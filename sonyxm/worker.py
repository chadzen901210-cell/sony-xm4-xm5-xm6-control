"""Background threads behind the app window.

Worker keeps one connection to the headphones' control channel open, applies
changes, and reports what the headphones say. HeadWorker reads the head
tracking sensor while head tracking is switched on. Both report through an
`emit` callback, so any front end (and the tests) can listen.
"""

from __future__ import annotations

import collections
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Union

from . import headtracker
from .bluetooth import BluetoothError, ChannelInUseError, DeviceNotFoundError, PairedDevice, SeveralDevicesError
from .connection import DEFAULT_CONNECT_TIMEOUT, ConnectionClosed, Log, ResponseTimeout, SessionError
from .connection import connect as default_connect
from .headphones import Change, Headphones, Notification
from .headtracker import HeadTrackerError, Orientation, Quaternion, SensorMissingError
from .models import Model, find_model
from .protocol import Battery, EqPreset, Equalizer, NcAsmState, PayloadError, ProtocolError

RECONNECT_DELAY = 5.0
# How long the worker listens for notifications before checking for new commands
LISTEN_SLICE = 0.2
# How often to look for the head tracking sensor again when it is missing
HEAD_RETRY_DELAY = 5.0


# --- Events -------------------------------------------------------------------


@dataclass(frozen=True)
class Connecting:
    pass


@dataclass(frozen=True)
class Connected:
    device: PairedDevice
    model: Model  # what these headphones can do, settled by the handshake


@dataclass(frozen=True)
class Disconnected:
    reason: str
    # "not_found" (not paired), "several" (several supported headphones paired, none
    # or more than one connected), "in_use" (another program holds the connection),
    # "unreachable" (could not connect), "lost"
    kind: str = "lost"


@dataclass(frozen=True)
class BatteryChanged:
    battery: Battery


@dataclass(frozen=True)
class NoiseControlChanged:
    state: NcAsmState


@dataclass(frozen=True)
class EqualizerChanged:
    equalizer: Equalizer


@dataclass(frozen=True)
class CommandFinished:
    error: str | None = None
    # When error is set: "timeout", "not_applied", "unexpected", or "unsupported"
    # (these headphones do not have that setting)
    code: str | None = None


@dataclass(frozen=True)
class HeadStatus:
    state: str  # "off", "starting", "running", "unavailable", "repairing"
    message: str | None = None
    code: str | None = None  # why it is unavailable: "missing", "error", "repair_failed", "repair_declined"


@dataclass(frozen=True)
class HeadPose:
    quaternion: Quaternion  # relative to "straight ahead"
    yaw: float
    pitch: float
    roll: float
    settled: bool
    rate: float  # samples per second


Event = Union[Connecting, Connected, Disconnected, BatteryChanged, NoiseControlChanged, EqualizerChanged,
              CommandFinished, HeadStatus, HeadPose]
Emit = Callable[[Event], None]


# --- Commands for the control channel worker ----------------------------------


@dataclass(frozen=True)
class _Refresh:
    pass


@dataclass(frozen=True)
class _SetNoiseControl:
    change: Change


@dataclass(frozen=True)
class _SetEqualizerPreset:
    preset: EqPreset


@dataclass(frozen=True)
class _SetEqualizerBands:
    clear_bass: int | None
    bands: tuple[int, ...]


_Command = Union[_Refresh, _SetNoiseControl, _SetEqualizerPreset, _SetEqualizerBands]


def _error_code(error: Exception) -> str:
    if isinstance(error, ResponseTimeout):
        return "timeout"
    if isinstance(error, PayloadError):
        return "unexpected"
    if isinstance(error, ValueError):
        return "unsupported"
    return "not_applied"


# --- Control channel ----------------------------------------------------------


class Worker(threading.Thread):
    """Owns the connection. Reconnects on its own when it is lost."""

    def __init__(
        self,
        emit: Emit,
        *,
        address: str | None = None,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        log: Log | None = None,
        connect: Callable = default_connect,
    ) -> None:
        super().__init__(name="sonyxm-worker", daemon=True)
        self._emit = emit
        self._address = address
        self._timeout = timeout
        self._log = log
        self._connect = connect
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._stopping = threading.Event()

    # Called from other threads

    def change(self, change: Change) -> None:
        self._commands.put(_SetNoiseControl(change))

    def set_equalizer_preset(self, preset: EqPreset) -> None:
        self._commands.put(_SetEqualizerPreset(preset))

    def set_equalizer_bands(self, clear_bass: int | None, bands: tuple[int, ...]) -> None:
        self._commands.put(_SetEqualizerBands(clear_bass, tuple(bands)))

    def refresh(self) -> None:
        self._commands.put(_Refresh())

    def stop(self) -> None:
        self._stopping.set()

    # Worker thread

    def run(self) -> None:
        while not self._stopping.is_set():
            self._emit(Connecting())
            try:
                conn, device = self._connect(self._address, timeout=self._timeout, log=self._log)
            except SeveralDevicesError as e:
                self._lost(str(e), "several")
                continue
            except DeviceNotFoundError as e:
                self._lost(str(e), "not_found")
                continue
            except ChannelInUseError as e:
                self._lost(str(e), "in_use")
                continue
            except (BluetoothError, OSError) as e:
                self._lost(str(e), "unreachable")
                continue
            try:
                with conn:
                    self._session(Headphones(conn, self._log, find_model(device.name)), device)
            except (SessionError, ProtocolError, OSError) as e:
                self._lost(str(e), "lost")

    def _lost(self, reason: str, kind: str) -> None:
        if not self._stopping.is_set():
            self._emit(Disconnected(reason, kind))
            self._stopping.wait(RECONNECT_DELAY)

    def _session(self, hp: Headphones, device: PairedDevice) -> None:
        hp.handshake()
        self._emit(Connected(device, hp.model))
        self._refresh(hp)
        while not self._stopping.is_set():
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                hp.listen(self._on_notification, duration=LISTEN_SLICE)
                continue
            self._run(hp, command)

    def _run(self, hp: Headphones, command: _Command) -> None:
        try:
            if isinstance(command, _Refresh):
                self._refresh(hp)
            elif isinstance(command, _SetNoiseControl):
                self._emit(NoiseControlChanged(hp.change_noise_control(command.change)))
            elif isinstance(command, _SetEqualizerPreset):
                self._emit(EqualizerChanged(hp.set_equalizer_preset(command.preset)))
            else:
                self._emit(EqualizerChanged(hp.set_equalizer_bands(command.clear_bass, command.bands)))
        except ConnectionClosed:
            raise
        except (SessionError, PayloadError, ValueError) as e:
            # The connection is still up: report it, then show the real state again
            self._emit(CommandFinished(str(e), _error_code(e)))
            try:
                if isinstance(command, (_SetEqualizerPreset, _SetEqualizerBands)):
                    self._emit(EqualizerChanged(hp.equalizer()))
                else:
                    self._emit(NoiseControlChanged(hp.noise_control()))
            except ConnectionClosed:
                raise
            except (SessionError, PayloadError):
                pass
            return
        self._emit(CommandFinished())

    def _refresh(self, hp: Headphones) -> None:
        self._optional("battery level", lambda: self._emit(BatteryChanged(hp.battery())))
        self._emit(NoiseControlChanged(hp.noise_control()))
        self._optional("equalizer", lambda: self._emit(EqualizerChanged(hp.equalizer())))

    def _optional(self, what: str, read: Callable[[], None]) -> None:
        """Read something the window can do without (models this project could not test may not answer)."""
        try:
            read()
        except ConnectionClosed:
            raise
        except (SessionError, PayloadError) as e:
            if self._log:
                self._log(f"{what} not available: {e}")

    def _on_notification(self, notification: Notification) -> None:
        if isinstance(notification, Battery):
            self._emit(BatteryChanged(notification))
        elif isinstance(notification, Equalizer):
            self._emit(EqualizerChanged(notification))
        else:
            self._emit(NoiseControlChanged(notification))


# --- Head tracking ------------------------------------------------------------


class HeadWorker(threading.Thread):
    """Reads the head tracking sensor while enabled; closes it (so the headphones stop sensing) when not."""

    def __init__(
        self,
        emit: Emit,
        *,
        log: Log | None = None,
        open_tracker: Callable = headtracker.open_head_tracker,
    ) -> None:
        super().__init__(name="sonyxm-head", daemon=True)
        self._emit = emit
        self._log = log
        self._open_tracker = open_tracker
        self._enabled = False
        self._recenter = False
        self._wake = threading.Event()
        self._stopping = threading.Event()

    # Called from other threads

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._wake.set()

    def recenter(self) -> None:
        self._recenter = True

    def retry(self) -> None:
        """Look for the sensor again now, e.g. after a repair."""
        self._wake.set()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    # Head tracking thread

    def run(self) -> None:
        self._emit(HeadStatus("off"))
        while not self._stopping.is_set():
            if not self._enabled:
                self._wait(None)
                continue
            self._emit(HeadStatus("starting"))
            try:
                tracker = self._open_tracker(self._log)
            except HeadTrackerError as e:
                self._emit(HeadStatus("unavailable", str(e), "missing" if isinstance(e, SensorMissingError) else "error"))
                self._wait(HEAD_RETRY_DELAY)
                continue
            try:
                with tracker:
                    self._stream(tracker)
            except HeadTrackerError as e:
                self._emit(HeadStatus("unavailable", str(e), "error"))
                self._wait(HEAD_RETRY_DELAY)
                continue
            if not self._stopping.is_set():
                self._emit(HeadStatus("off"))

    def _wait(self, timeout: float | None) -> None:
        self._wake.wait(timeout)
        self._wake.clear()

    def _stream(self, tracker: headtracker.HeadTracker) -> None:
        self._emit(HeadStatus("running"))
        orientation = Orientation()
        arrivals: collections.deque[float] = collections.deque(maxlen=26)
        while self._enabled and not self._stopping.is_set():
            if self._recenter:
                self._recenter = False
                orientation.recenter()
            sample = tracker.read(timeout=0.5)
            if sample is None:
                continue
            angles = orientation.update(sample)
            arrivals.append(time.monotonic())
            elapsed = arrivals[-1] - arrivals[0]
            rate = (len(arrivals) - 1) / elapsed if elapsed > 0 else 0.0
            self._emit(HeadPose(orientation.relative(), angles.yaw, angles.pitch, angles.roll,
                                orientation.settled, rate))

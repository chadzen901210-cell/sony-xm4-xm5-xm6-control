"""What you can do with connected headphones: battery, noise control and equalizer.

Shared by the command line (cli.py) and the app window (worker.py). Works with
protocol V1 (WH-1000XM4) and V2 (WH-1000XM5/XM6); the handshake tells which.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Callable, TypeVar, Union

from .connection import Connection, Log, ResponseTimeout, SessionError
from .models import Model, generic_model, resolve
from .protocol import (
    AMBIENT_LEVEL_MIN,
    BATTERY_REPLIES,
    EQ_BAND_SLOTS,
    V1,
    V2,
    Battery,
    Command,
    EqPreset,
    Equalizer,
    Frame,
    MessageType,
    NcAsmMode,
    NcAsmState,
    PayloadError,
    battery_request,
    eq_band_names,
    equalizer_bands_set,
    equalizer_preset_set,
    equalizer_request,
    ncasm_request,
    ncasm_set,
    parse_battery,
    parse_equalizer,
    parse_ncasm,
)

# How long to wait for a reply before trying the next sub-type or query kind.
# The headphones answered the battery query within 50 ms.
NCASM_QUERY_TIMEOUT = 2.0
# If the state read back right after a change does not match yet, read once
# more after this delay before reporting a failure.
READBACK_RETRY_DELAY = 0.5

Change = Callable[[NcAsmState], NcAsmState]
Notification = Union[Battery, NcAsmState, Equalizer]
_T = TypeVar("_T")


def describe_noise_control(state: NcAsmState) -> str:
    if state.mode is NcAsmMode.OFF:
        return "off"
    if state.mode is NcAsmMode.NOISE_CANCELLING:
        return "noise cancelling"
    if state.mode is NcAsmMode.WIND:
        return "wind noise reduction"
    text = f"ambient sound, level {state.ambient_level}"
    if state.focus_on_voice:
        text += ", focus on voice"
    if state.auto_ambient:
        text += ", automatic level"
    return text


def same_setting(wanted: NcAsmState, actual: NcAsmState) -> bool:
    """Level, voice focus and automatic level only matter in ambient mode."""
    if wanted.mode is not actual.mode:
        return False
    if wanted.mode is NcAsmMode.AMBIENT:
        return (wanted.ambient_level, wanted.focus_on_voice, wanted.auto_ambient) == (
            actual.ambient_level, actual.focus_on_voice, actual.auto_ambient)
    return True


def describe_equalizer_preset(preset: EqPreset | int) -> str:
    if isinstance(preset, EqPreset):
        return preset.name.replace("_", " ").title()  # e.g. "Bass Boost", "Custom 1"
    return f"preset 0x{preset:02x}"


def describe_equalizer(eq: Equalizer) -> str:
    name = describe_equalizer_preset(eq.preset)
    if not eq.bands:
        return name
    bands = ", ".join(f"{label} {value:+d}" for label, value in zip(eq_band_names(len(eq.bands)), eq.bands))
    if eq.clear_bass is None:
        return f"{name} ({bands})"
    return f"{name} (Clear Bass {eq.clear_bass:+d}; {bands})"


def parse_notification(frame: Frame) -> Notification | None:
    """Battery, NC/ambient or equalizer state from a frame the headphones sent on their own, else None."""
    if frame.type != MessageType.COMMAND_1 or not frame.payload:
        return None
    try:
        if frame.payload[0] == Command.NCASM_NOTIFY:
            return parse_ncasm(frame.payload)[1]
        if frame.payload[0] in (Command.BATTERY_NOTIFY, Command.BATTERY_NOTIFY_V1):
            return parse_battery(frame.payload)
        if frame.payload[0] == Command.EQ_NOTIFY:
            return parse_equalizer(frame.payload)
    except PayloadError:
        return None
    return None


class Headphones:
    def __init__(self, conn: Connection, log: Log | None = None, model: Model | None = None) -> None:
        self.conn = conn
        self._log = log
        self._expected = model
        # What we know about these headphones; settled by handshake()
        self.model: Model = model or generic_model(V2)
        self.subtype: int | None = None  # NC/ambient sub-type, once known
        self.battery_kind: int | None = None  # battery query kind, once known

    @property
    def version(self) -> str:
        if self.conn.version is None:
            raise SessionError("no handshake yet")
        return self.conn.version

    def handshake(self) -> str:
        version = self.conn.handshake()
        self.model = resolve(self._expected, version)
        if self._expected is not None and self.model is not self._expected:
            self._debug(f"{self._expected.name} answered with protocol {version}; using generic settings")
        return version

    def battery(self) -> Battery:
        if self.battery_kind is None and len(self.model.battery_kinds) == 1:
            self.battery_kind = self.model.battery_kinds[0]
        if self.battery_kind is not None:
            return self._query_battery(self.battery_kind)
        kind, battery = self._first_answer(
            self.model.battery_kinds, lambda k: self._query_battery(k, NCASM_QUERY_TIMEOUT), "battery level"
        )
        self.battery_kind = kind
        return battery

    def _query_battery(self, kind: int, timeout: float | None = None) -> Battery:
        reply = self.conn.request(battery_request(self.version, kind), BATTERY_REPLIES[self.version],
                                  timeout=timeout)
        assert reply is not None
        return parse_battery(reply.payload)

    def query_noise_control(self, subtype: int) -> NcAsmState:
        """Ask for the NC/ambient state using one sub-type (PROTOCOL.md §6.2)."""
        reply = self.conn.request(ncasm_request(subtype), {Command.NCASM_RET}, timeout=NCASM_QUERY_TIMEOUT)
        assert reply is not None
        answered, state = parse_ncasm(reply.payload)
        if answered != subtype:
            raise PayloadError(f"asked with sub-type 0x{subtype:02x}, answered with 0x{answered:02x}")
        return state

    def noise_control(self) -> NcAsmState:
        """Current NC/ambient state. Finds the right sub-type on first use and remembers it."""
        if self.subtype is not None:
            return self.query_noise_control(self.subtype)
        subtype, state = self._first_answer(self.model.ncasm_subtypes, self.query_noise_control,
                                            "noise control state")
        self.subtype = subtype
        return state

    def _first_answer(self, options: tuple[int, ...], query: Callable[[int], _T], what: str) -> tuple[int, _T]:
        """Try each option until the headphones answer one; the headphones ignore the others."""
        problems = []
        for option in options:
            try:
                return option, query(option)
            except (ResponseTimeout, PayloadError) as e:
                problems.append(f"0x{option:02x}: {e}")
        raise SessionError(f"the headphones did not report their {what} (" + "; ".join(problems) + ")")

    def change_noise_control(self, change: Change) -> NcAsmState:
        """Read the current state, write change(current), and return the state read back afterwards.

        Starting from the current state keeps the ambient level and voice focus
        the user already had (PROTOCOL.md §6.2). Raises ValueError for a mode
        or setting these headphones do not have.
        """
        current = self.noise_control()
        assert self.subtype is not None
        wanted = change(current)
        if wanted.mode is NcAsmMode.WIND and not self.model.wind:
            raise ValueError("wind noise reduction is only available on the WH-1000XM4")
        if wanted.auto_ambient is not None and current.auto_ambient is None:
            raise ValueError("automatic ambient level is only available on the WH-1000XM6")
        if wanted.ambient_level < AMBIENT_LEVEL_MIN:
            # Carried along from the current state; the XM5 would turn it into 1 anyway
            wanted = dataclasses.replace(wanted, ambient_level=AMBIENT_LEVEL_MIN)
        if same_setting(wanted, current):
            self._debug("already in the requested state; nothing to write")
            return current
        self.conn.request(ncasm_set(self.subtype, wanted))
        actual = self.query_noise_control(self.subtype)
        if not same_setting(wanted, actual):
            time.sleep(READBACK_RETRY_DELAY)
            actual = self.query_noise_control(self.subtype)
        if not same_setting(wanted, actual):
            raise SessionError(
                f"asked for {describe_noise_control(wanted)}, "
                f"but the headphones report {describe_noise_control(actual)}"
            )
        return actual

    def equalizer(self) -> Equalizer:
        reply = self.conn.request(equalizer_request(self.version), {Command.EQ_RET}, timeout=NCASM_QUERY_TIMEOUT)
        assert reply is not None
        return parse_equalizer(reply.payload)

    def set_equalizer_preset(self, preset: EqPreset) -> Equalizer:
        self.conn.request(equalizer_preset_set(preset, self.version))
        return self._read_back_equalizer(lambda eq: eq.preset == preset, describe_equalizer_preset(preset))

    def set_equalizer_bands(
        self, clear_bass: int | None, bands: tuple[int, ...], slot: EqPreset | None = None
    ) -> Equalizer:
        """Hand-set the bands: Clear Bass and 5 bands, or (XM6) no Clear Bass and 10 bands.

        On protocol V2 they are stored in `slot`. By default, like Sony's app:
        editing Custom 1 or 2 changes that slot; editing any other preset
        switches to Manual. V1 has no slots. Raises ValueError if the values
        do not fit these headphones.
        """
        current = self.equalizer()
        if len(bands) != len(current.bands):
            if not current.bands:
                raise ValueError("these headphones do not report equalizer bands")
            raise ValueError(f"these headphones have {len(current.bands)} equalizer bands, got {len(bands)} values")
        bands = tuple(bands)
        if self.version == V1:
            self.conn.request(equalizer_bands_set(clear_bass, bands, version=V1))
            wanted = Equalizer(EqPreset.MANUAL, clear_bass, bands)
            # V1 reports whichever preset it files hand-set bands under
            return self._read_back_equalizer(lambda eq: (eq.clear_bass, eq.bands) == (clear_bass, bands),
                                             describe_equalizer(wanted))
        if slot is None:
            slot = current.preset if current.preset in EQ_BAND_SLOTS else EqPreset.MANUAL
        self.conn.request(equalizer_bands_set(clear_bass, bands, slot))
        wanted = Equalizer(slot, clear_bass, bands)
        return self._read_back_equalizer(lambda eq: eq == wanted, describe_equalizer(wanted))

    def _read_back_equalizer(self, ok: Callable[[Equalizer], bool], wanted: str) -> Equalizer:
        actual = self.equalizer()
        if not ok(actual):
            time.sleep(READBACK_RETRY_DELAY)
            actual = self.equalizer()
        if not ok(actual):
            raise SessionError(f"asked for {wanted}, but the headphones report {describe_equalizer(actual)}")
        return actual

    def listen(self, on_notification: Callable[[Notification], None], duration: float | None = None) -> None:
        """Pass battery, NC/ambient and equalizer changes the headphones report on their own to on_notification."""

        def on_frame(frame: Frame) -> None:
            notification = parse_notification(frame)
            if notification is not None:
                on_notification(notification)

        self.conn.listen(on_frame, duration)

    def _debug(self, text: str) -> None:
        if self._log is not None:
            self._log(text)

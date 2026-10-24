"""Tests for sonyxm.worker: the control channel worker against simulated headphones, and the head tracking worker."""

import collections
import dataclasses
import math
import queue
import time
import unittest
from unittest import mock

from tests.fakes import FakeSocket, SimulatedXm5, device_ack, device_frame, simulated_xm6
from sonyxm import headphones, worker
from sonyxm.bluetooth import BluetoothError, DeviceNotFoundError, PairedDevice, SeveralDevicesError, connect_error
from sonyxm.connection import Connection
from sonyxm.headtracker import HeadTrackerError, Sample, SensorMissingError
from sonyxm.models import MODELS
from sonyxm.protocol import (
    Battery,
    ChargingStatus,
    Command,
    EqPreset,
    Equalizer,
    MessageType,
    NcAsmMode,
    NcAsmState,
)
from sonyxm.worker import (
    BatteryChanged,
    CommandFinished,
    Connected,
    Connecting,
    Disconnected,
    EqualizerChanged,
    HeadPose,
    HeadStatus,
    HeadWorker,
    NoiseControlChanged,
    Worker,
)

XM5 = PairedDevice(name="WH-1000XM5", address="12:34:56:78:9A:BC", connected=True, authenticated=True)
XM6 = PairedDevice(name="WH-1000XM6", address="12:34:56:78:9A:BE", connected=True, authenticated=True)
_, XM5_MODEL, XM6_MODEL = MODELS
NC = NcAsmMode.NOISE_CANCELLING
AMBIENT = NcAsmMode.AMBIENT
OFF = NcAsmMode.OFF


def well_behaved_with_noise(frame):
    """Answers NC queries (as an XM5 in noise cancelling) but ignores equalizer queries."""
    if frame.type == MessageType.COMMAND_1 and frame.payload[0] == Command.EQ_GET:
        return [device_ack(frame.seq)]
    return SimulatedXm5(NcAsmState(NC, False, 10))(frame)


def drain(events):
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except queue.Empty:
            return items


class DroppingSocket(FakeSocket):
    """A connection that can be cut, as when the headphones are switched off."""

    dropped = False

    def recv(self, size):
        if self.dropped:
            return b""
        return super().recv(size)


class EventsMixin:
    def setUp(self):
        self.events = queue.Queue()
        for target, name, value in (
            (worker, "RECONNECT_DELAY", 0.05),
            (worker, "LISTEN_SLICE", 0.05),
            (worker, "HEAD_RETRY_DELAY", 0.05),
            (headphones, "NCASM_QUERY_TIMEOUT", 0.2),
            (headphones, "READBACK_RETRY_DELAY", 0),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait_for(self, predicate, timeout=3.0):
        """Collect events until one matches (a type or a function); return them all."""
        if isinstance(predicate, type):
            event_type = predicate
            predicate = lambda e: isinstance(e, event_type)  # noqa: E731
        seen = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = self.events.get(timeout=0.05)
            except queue.Empty:
                continue
            seen.append(event)
            if predicate(event):
                return seen
        self.fail(f"no matching event within {timeout}s; got {seen}")

    def start_thread(self, thread):
        thread.start()

        def stop():
            thread.stop()
            thread.join(timeout=2)

        self.addCleanup(stop)
        return thread


class WorkerTest(EventsMixin, unittest.TestCase):
    def start(self, sockets, device=XM5):
        """Start a worker whose successive connections use the given sockets (or exceptions)."""
        sockets = list(sockets)

        def fake_connect(address, *, timeout, log):
            item = sockets.pop(0) if len(sockets) > 1 else sockets[0]
            if isinstance(item, Exception):
                raise item
            return Connection(item, log, request_timeout=1.0), device

        return self.start_thread(Worker(self.events.put, connect=fake_connect))

    def test_connects_and_reports_state(self):
        self.start([FakeSocket(SimulatedXm5(NcAsmState(AMBIENT, False, 10)))])
        self.assertEqual(self.wait_for(NoiseControlChanged), [
            Connecting(),
            Connected(XM5, XM5_MODEL),
            BatteryChanged(Battery(75, ChargingStatus.NOT_CHARGING)),
            NoiseControlChanged(NcAsmState(AMBIENT, False, 10)),
        ])

    def test_change(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, False, 10))
        w = self.start([FakeSocket(device)])
        self.wait_for(NoiseControlChanged)
        w.change(lambda current: dataclasses.replace(current, mode=NC))
        events = self.wait_for(CommandFinished)
        self.assertEqual(events[-2:], [NoiseControlChanged(NcAsmState(NC, False, 10)), CommandFinished()])
        self.assertEqual(device.writes, ["68 17 01 01 00 00 0a"])

    def test_button_press_on_headphones_is_reported(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        sock = FakeSocket(device)
        self.start([sock])
        self.wait_for(NoiseControlChanged)
        device.press_button(sock, NcAsmState(AMBIENT, True, 5))
        self.assertEqual(self.wait_for(NoiseControlChanged)[-1], NoiseControlChanged(NcAsmState(AMBIENT, True, 5)))

    def test_battery_notification_is_reported(self):
        sock = FakeSocket(SimulatedXm5(NcAsmState(NC, False, 10)))
        self.start([sock])
        self.wait_for(NoiseControlChanged)
        sock.incoming.append(device_frame(1, bytes.fromhex("25 00 32 01")))
        self.assertEqual(self.wait_for(BatteryChanged)[-1], BatteryChanged(Battery(50, ChargingStatus.CHARGING)))

    def test_refresh(self):
        w = self.start([FakeSocket(SimulatedXm5(NcAsmState(OFF, False, 3)))])
        self.wait_for(EqualizerChanged)
        w.refresh()
        self.assertEqual(self.wait_for(CommandFinished), [
            BatteryChanged(Battery(75, ChargingStatus.NOT_CHARGING)),
            NoiseControlChanged(NcAsmState(OFF, False, 3)),
            EqualizerChanged(Equalizer(EqPreset.OFF, 0, (0, 0, 0, 0, 0))),
            CommandFinished(),
        ])

    def test_equalizer(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        w = self.start([FakeSocket(device)])
        self.wait_for(EqualizerChanged)
        w.set_equalizer_preset(EqPreset.BRIGHT)
        events = self.wait_for(CommandFinished)
        self.assertEqual(events[-2:], [EqualizerChanged(Equalizer(EqPreset.BRIGHT, -1, (0, 5, 7, 7, 9))),
                                       CommandFinished()])
        w.set_equalizer_bands(3, (1, 2, 3, 4, 5))
        events = self.wait_for(CommandFinished)
        self.assertEqual(events[-2], EqualizerChanged(Equalizer(EqPreset.MANUAL, 3, (1, 2, 3, 4, 5))))
        self.assertEqual(device.writes, ["58 00 10 00", "58 00 a0 06 0d 0b 0c 0d 0e 0f"])

    def test_failed_equalizer_change_shows_the_real_state(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10), apply_writes=False)
        w = self.start([FakeSocket(device)])
        self.wait_for(EqualizerChanged)
        w.set_equalizer_preset(EqPreset.VOCAL)
        finished = self.wait_for(CommandFinished)[-1]
        self.assertEqual(finished.code, "not_applied")
        self.assertEqual(self.wait_for(EqualizerChanged)[-1].equalizer.preset, EqPreset.OFF)

    def test_xm6(self):
        device = simulated_xm6(NcAsmState(AMBIENT, False, 10, auto_ambient=True))
        self.start([FakeSocket(device)], device=XM6)
        events = self.wait_for(EqualizerChanged)
        self.assertEqual(events[:4], [
            Connecting(),
            Connected(XM6, XM6_MODEL),
            BatteryChanged(Battery(75, ChargingStatus.NOT_CHARGING)),
            NoiseControlChanged(NcAsmState(AMBIENT, False, 10, auto_ambient=True)),
        ])
        self.assertEqual(len(events[-1].equalizer.bands), 10)

    def test_unsupported_setting_is_reported_not_fatal(self):
        w = self.start([FakeSocket(SimulatedXm5(NcAsmState(NC, False, 10)))])
        self.wait_for(EqualizerChanged)
        w.change(lambda current: dataclasses.replace(current, mode=NcAsmMode.WIND))
        finished = self.wait_for(CommandFinished)[-1]
        self.assertEqual(finished.code, "unsupported")
        time.sleep(0.2)
        self.assertFalse(any(isinstance(e, Disconnected) for e in drain(self.events)))

    def test_battery_is_optional(self):
        # Headphones that never answer the battery query (an untested model) still connect
        def no_battery(frame):
            if frame.type == MessageType.COMMAND_1 and frame.payload[0] == Command.BATTERY_GET:
                return [device_ack(frame.seq)]
            return SimulatedXm5(NcAsmState(NC, False, 10))(frame)

        self.start([FakeSocket(no_battery)])
        events = self.wait_for(NoiseControlChanged)
        self.assertFalse(any(isinstance(e, (BatteryChanged, Disconnected)) for e in events))

    def test_equalizer_is_optional(self):
        # Headphones that never answer the equalizer query still connect fine
        self.start([FakeSocket(well_behaved_with_noise)])
        events = self.wait_for(NoiseControlChanged)
        self.assertEqual(events[-1], NoiseControlChanged(NcAsmState(NC, False, 10)))
        time.sleep(0.5)
        self.assertFalse(any(isinstance(e, Disconnected) for e in drain(self.events)))

    def test_failed_change_keeps_the_connection(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, False, 10), apply_writes=False)
        w = self.start([FakeSocket(device)])
        self.wait_for(NoiseControlChanged)
        w.change(lambda current: dataclasses.replace(current, mode=NC))
        events = self.wait_for(CommandFinished)
        self.assertIn("asked for noise cancelling", events[-1].error)
        self.assertEqual(self.wait_for(NoiseControlChanged)[-1], NoiseControlChanged(NcAsmState(AMBIENT, False, 10)))
        w.refresh()
        events = self.wait_for(CommandFinished)
        self.assertEqual(events[-1], CommandFinished())
        self.assertFalse(any(isinstance(e, Disconnected) for e in events))

    def test_retries_when_connecting_fails(self):
        self.start([BluetoothError("connection timed out"), FakeSocket(SimulatedXm5(NcAsmState(NC, False, 10)))])
        self.assertEqual(self.wait_for(Connected), [
            Connecting(), Disconnected("connection timed out", "unreachable"), Connecting(), Connected(XM5, XM5_MODEL),
        ])

    def test_not_paired(self):
        self.start([DeviceNotFoundError("no paired WH-1000XM5 found")])
        self.assertEqual(self.wait_for(Disconnected)[-1], Disconnected("no paired WH-1000XM5 found", "not_found"))

    def test_several_headphones_paired(self):
        self.start([SeveralDevicesError("several supported headphones are paired")])
        self.assertEqual(self.wait_for(Disconnected)[-1].kind, "several")

    def test_channel_in_use(self):
        error = connect_error(10048)
        self.start([error])
        self.assertEqual(self.wait_for(Disconnected)[-1], Disconnected(str(error), "in_use"))

    def test_reconnects_when_the_connection_drops(self):
        first = DroppingSocket(SimulatedXm5(NcAsmState(NC, False, 10)))
        second = FakeSocket(SimulatedXm5(NcAsmState(AMBIENT, False, 4)))
        self.start([first, second])
        self.wait_for(EqualizerChanged)  # the end of the first refresh
        first.dropped = True
        events = self.wait_for(NoiseControlChanged)
        self.assertEqual([type(e) for e in events[:3]], [Disconnected, Connecting, Connected])
        self.assertTrue(first.closed)
        self.assertEqual(events[-1], NoiseControlChanged(NcAsmState(AMBIENT, False, 4)))

    def test_stop_closes_the_connection(self):
        sock = FakeSocket(SimulatedXm5(NcAsmState(NC, False, 10)))
        w = self.start([sock])
        self.wait_for(NoiseControlChanged)
        w.stop()
        w.join(timeout=2)
        self.assertFalse(w.is_alive())
        self.assertTrue(sock.closed)


class FakeTracker:
    """Stands in for headtracker.HeadTracker: hands out queued samples."""

    def __init__(self):
        self.samples = collections.deque()
        self.closed = False

    def read(self, timeout=1.0):
        if self.samples:
            return self.samples.popleft()
        time.sleep(min(timeout, 0.01))
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


def yaw_sample(degrees, counter=0):
    return Sample((0.0, 0.0, math.radians(degrees)), None, counter)


class HeadWorkerTest(EventsMixin, unittest.TestCase):
    def start(self, trackers):
        """Start a head worker whose successive opens return these trackers (or raise these errors)."""
        trackers = list(trackers)
        self.opens = 0

        def open_tracker(log):
            self.opens += 1
            item = trackers.pop(0) if len(trackers) > 1 else trackers[0]
            if isinstance(item, Exception):
                raise item
            return item

        return self.start_thread(HeadWorker(self.events.put, open_tracker=open_tracker))

    def test_off_until_enabled(self):
        self.start([FakeTracker()])
        self.assertEqual(self.wait_for(HeadStatus), [HeadStatus("off")])
        time.sleep(0.1)
        self.assertEqual(self.opens, 0)

    def test_streams_poses_while_enabled(self):
        tracker = FakeTracker()
        tracker.samples.extend([yaw_sample(10), yaw_sample(25)])
        head = self.start([tracker])
        self.wait_for(HeadStatus)
        head.set_enabled(True)
        events = self.wait_for(lambda e: isinstance(e, HeadPose) and abs(e.yaw - 15) < 1e-6)
        self.assertEqual([e for e in events if isinstance(e, HeadStatus)],
                         [HeadStatus("starting"), HeadStatus("running")])
        first = next(e for e in events if isinstance(e, HeadPose))
        self.assertAlmostEqual(first.yaw, 0)  # the first sample is "straight ahead"
        self.assertFalse(first.settled)

    def test_turning_off_closes_the_sensor(self):
        tracker = FakeTracker()
        head = self.start([tracker])
        self.wait_for(HeadStatus)
        head.set_enabled(True)
        self.wait_for(lambda e: e == HeadStatus("running"))
        head.set_enabled(False)
        self.wait_for(lambda e: e == HeadStatus("off"))
        self.assertTrue(tracker.closed)

    def test_recenter(self):
        tracker = FakeTracker()
        tracker.samples.extend([yaw_sample(0), yaw_sample(30)])
        head = self.start([tracker])
        self.wait_for(HeadStatus)
        head.set_enabled(True)
        self.wait_for(lambda e: isinstance(e, HeadPose) and abs(e.yaw - 30) < 1e-6)
        head.recenter()
        tracker.samples.append(yaw_sample(40))
        self.wait_for(lambda e: isinstance(e, HeadPose) and abs(e.yaw - 10) < 1e-6)

    def test_missing_sensor_is_reported_and_retried(self):
        tracker = FakeTracker()
        head = self.start([SensorMissingError("Windows has not set up the sensor"), tracker])
        self.wait_for(HeadStatus)
        head.set_enabled(True)
        self.wait_for(lambda e: e == HeadStatus("unavailable", "Windows has not set up the sensor", "missing"))
        head.retry()
        self.wait_for(lambda e: e == HeadStatus("running"))
        self.assertEqual(self.opens, 2)

    def test_read_failure_is_reported(self):
        class Broken(FakeTracker):
            def read(self, timeout=1.0):
                raise HeadTrackerError("reading the head tracker failed")

        head = self.start([Broken(), FakeTracker()])
        self.wait_for(HeadStatus)
        head.set_enabled(True)
        self.wait_for(lambda e: e == HeadStatus("unavailable", "reading the head tracker failed", "error"))
        self.wait_for(lambda e: e == HeadStatus("running"))


if __name__ == "__main__":
    unittest.main()

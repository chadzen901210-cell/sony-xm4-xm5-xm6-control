"""Tests for sonyxm.headphones."""

import dataclasses
import unittest
from unittest import mock

from tests.fakes import CMD, FakeSocket, SimulatedXm5, device_frame, simulated_xm4, simulated_xm6
from sonyxm import headphones
from sonyxm.connection import Connection
from sonyxm.headphones import Headphones, parse_notification
from sonyxm.models import MODELS, find_model
from sonyxm.protocol import (
    V1,
    V2,
    Battery,
    ChargingStatus,
    Command,
    EqPreset,
    Equalizer,
    Frame,
    MessageType,
    NcAsmMode,
    NcAsmState,
)

NC = NcAsmMode.NOISE_CANCELLING
AMBIENT = NcAsmMode.AMBIENT
XM4, XM5, XM6 = MODELS


class SubtypeTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(headphones, "NCASM_QUERY_TIMEOUT", 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def nc_queries(self, sock):
        return [f.payload.hex(" ") for f in sock.host_commands() if f.payload[0] == Command.NCASM_GET]

    def test_found_subtype_is_remembered(self):
        sock = FakeSocket(SimulatedXm5(NcAsmState(NC, False, 10), subtype=0x15))
        hp = Headphones(Connection(sock), model=XM5)
        hp.handshake()
        self.assertIsNone(hp.subtype)
        self.assertEqual(hp.noise_control(), NcAsmState(NC, False, 10))
        self.assertEqual(hp.subtype, 0x15)
        hp.noise_control()
        # 0x17 is tried once, then only the sub-type that answered
        self.assertEqual(self.nc_queries(sock), ["66 17", "66 15", "66 15"])

    def test_xm5_needs_one_query(self):
        sock = FakeSocket(SimulatedXm5(NcAsmState(AMBIENT, True, 20)))
        hp = Headphones(Connection(sock), model=XM5)
        hp.handshake()
        self.assertEqual(hp.noise_control(), NcAsmState(AMBIENT, True, 20))
        self.assertEqual(self.nc_queries(sock), ["66 17"])


def connected(device, model):
    sock = FakeSocket(device)
    hp = Headphones(Connection(sock), model=model)
    hp.handshake()
    return hp, sock


class Xm4Test(unittest.TestCase):
    """Protocol V1, as Gadgetbridge documents it. Not tested on real WH-1000XM4 headphones."""

    def setUp(self):
        patcher = mock.patch.object(headphones, "READBACK_RETRY_DELAY", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_handshake_and_reads(self):
        hp, sock = connected(simulated_xm4(NcAsmState(AMBIENT, True, 12)), XM4)
        self.assertEqual(hp.version, V1)
        self.assertIs(hp.model, XM4)
        self.assertEqual(hp.battery(), Battery(75, ChargingStatus.NOT_CHARGING))
        self.assertEqual(hp.noise_control(), NcAsmState(AMBIENT, True, 12))
        self.assertEqual(hp.equalizer(), Equalizer(EqPreset.OFF, 0, (0, 0, 0, 0, 0)))
        self.assertEqual([f.payload.hex(" ") for f in sock.host_commands()], ["00 00", "10 00", "66 02", "56 01"])

    def test_wind_noise_reduction(self):
        device = simulated_xm4(NcAsmState(NC, False, 12))
        hp, _ = connected(device, XM4)
        state = hp.change_noise_control(lambda current: dataclasses.replace(current, mode=NcAsmMode.WIND))
        self.assertEqual(state.mode, NcAsmMode.WIND)
        self.assertEqual(device.writes, ["68 02 11 02 01 01 00 00"])

    def test_equalizer_bands(self):
        device = simulated_xm4(NcAsmState(NC, False, 12), equalizer=Equalizer(EqPreset.BRIGHT, -1, (0, 5, 7, 7, 9)))
        hp, _ = connected(device, XM4)
        eq = hp.set_equalizer_bands(2, (1, 2, 3, 4, 5))
        self.assertEqual(eq, Equalizer(EqPreset.MANUAL, 2, (1, 2, 3, 4, 5)))
        self.assertEqual(device.writes, ["58 01 ff 06 0c 0b 0c 0d 0e 0f"])
        hp.set_equalizer_preset(EqPreset.VOCAL)
        self.assertEqual(device.writes[-1], "58 01 14 00")


class Xm6Test(unittest.TestCase):
    """What mos9527's client sends to the WH-1000XM6. Not tested on real XM6 headphones."""

    def setUp(self):
        for name, value in (("NCASM_QUERY_TIMEOUT", 0.1), ("READBACK_RETRY_DELAY", 0)):
            patcher = mock.patch.object(headphones, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_battery_with_threshold_is_found_and_remembered(self):
        hp, sock = connected(simulated_xm6(NcAsmState(NC, False, 10)), XM6)
        self.assertEqual(hp.battery(), Battery(75, ChargingStatus.NOT_CHARGING))
        hp.battery()
        queries = [f.payload.hex(" ") for f in sock.host_commands() if f.payload[0] == Command.BATTERY_GET]
        self.assertEqual(queries, ["22 00", "22 08", "22 08"])

    def test_auto_ambient(self):
        device = simulated_xm6(NcAsmState(AMBIENT, False, 10, auto_ambient=False))
        hp, _ = connected(device, XM6)
        state = hp.change_noise_control(lambda current: dataclasses.replace(current, auto_ambient=True))
        self.assertTrue(state.auto_ambient)
        self.assertEqual(device.writes, ["68 19 01 01 01 00 0a 01 00"])

    def test_ten_bands(self):
        device = simulated_xm6(NcAsmState(NC, False, 10))
        hp, _ = connected(device, XM6)
        bands = (0, 1, 2, 3, 4, 5, 6, -6, -1, 0)
        self.assertEqual(hp.set_equalizer_bands(None, bands), Equalizer(EqPreset.MANUAL, None, bands))
        self.assertEqual(device.writes, ["58 00 a0 0a 06 07 08 09 0a 0b 0c 00 05 06"])
        with self.assertRaisesRegex(ValueError, "10 equalizer bands"):
            hp.set_equalizer_bands(0, (0, 0, 0, 0, 0))


class ModelTest(unittest.TestCase):
    def test_xm5_has_no_wind_or_auto_ambient(self):
        hp, _ = connected(SimulatedXm5(NcAsmState(NC, False, 10)), XM5)
        with self.assertRaisesRegex(ValueError, "WH-1000XM4"):
            hp.change_noise_control(lambda current: dataclasses.replace(current, mode=NcAsmMode.WIND))
        with self.assertRaisesRegex(ValueError, "WH-1000XM6"):
            hp.change_noise_control(lambda current: dataclasses.replace(current, auto_ambient=True))

    def test_the_handshake_has_the_last_word(self):
        # Named like an XM4 but answering with protocol V2: use the generic V2 settings
        hp, _ = connected(SimulatedXm5(NcAsmState(NC, False, 10)), XM4)
        self.assertEqual(hp.model.protocol, V2)
        self.assertEqual(hp.model.name, "WH-1000XM4")
        self.assertEqual(hp.noise_control(), NcAsmState(NC, False, 10))

    def test_find_model(self):
        self.assertIs(find_model("WH-1000XM5"), XM5)
        self.assertIs(find_model("Living room wh-1000xm6"), XM6)
        self.assertIsNone(find_model("WF-1000XM5"))


class NotificationTest(unittest.TestCase):
    def test_parse(self):
        cases = [
            (Frame(CMD, 0, bytes.fromhex("25 00 32 01")), Battery(50, ChargingStatus.CHARGING)),
            (Frame(CMD, 0, bytes.fromhex("13 00 32 00")), Battery(50, ChargingStatus.NOT_CHARGING)),  # V1
            (Frame(CMD, 0, bytes.fromhex("69 02 01 02 01 01 00 00")), NcAsmState(NcAsmMode.WIND, False, 0)),
            (Frame(CMD, 0, bytes.fromhex("59 00 30 0a 06 06 06 06 06 06 06 06 06 07")),
             Equalizer(EqPreset.HEAVY, None, (0, 0, 0, 0, 0, 0, 0, 0, 0, 1))),
            (Frame(CMD, 0, bytes.fromhex("69 17 01 01 01 00 05")), NcAsmState(AMBIENT, False, 5)),
            (Frame(CMD, 0, bytes.fromhex("67 17 01 01 01 00 05")), None),  # a reply, not a notification
            (Frame(CMD, 0, bytes.fromhex("15 02 00")), None),  # codec notification: not ours
            (Frame(CMD, 0, bytes.fromhex("69 17 01 01")), None),  # malformed
            (Frame(CMD, 0, b""), None),
            (Frame(MessageType.ACK, 0), None),
            (Frame(MessageType.COMMAND_2, 0, bytes.fromhex("25 00 32 01")), None),
        ]
        for frame, expected in cases:
            with self.subTest(frame=str(frame)):
                self.assertEqual(parse_notification(frame), expected)

    def test_listen_passes_on_parsed_notifications(self):
        sock = FakeSocket()
        sock.incoming.extend([
            device_frame(0, bytes.fromhex("69 17 01 01 00 00 14")),
            device_frame(1, bytes.fromhex("15 02 00")),
            device_frame(0, bytes.fromhex("25 00 50 00")),
        ])
        received = []
        Headphones(Connection(sock)).listen(received.append, duration=0.1)
        self.assertEqual(received, [NcAsmState(NC, False, 20), Battery(80, ChargingStatus.NOT_CHARGING)])
        self.assertEqual(len(sock.host_acks()), 3)  # every frame is still acknowledged


if __name__ == "__main__":
    unittest.main()

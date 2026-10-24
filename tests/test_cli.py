"""Tests for sonyxm.cli, with the Bluetooth connection replaced by a fake."""

import contextlib
import io
import unittest
from unittest import mock

from tests.fakes import (
    CMD,
    FakeSocket,
    SimulatedXm5,
    device_ack,
    device_frame,
    simulated_xm4,
    simulated_xm6,
    well_behaved,
)
from sonyxm import cli, headphones
from sonyxm.bluetooth import BluetoothError, PairedDevice
from sonyxm.connection import Connection
from sonyxm.protocol import (
    Battery,
    ChargingStatus,
    Command,
    EqPreset,
    Equalizer,
    Frame,
    MessageType,
    NcAsmMode,
    NcAsmState,
    battery_request,
)

XM5 = PairedDevice(name="WH-1000XM5", address="12:34:56:78:9A:BC", connected=True, authenticated=True)


def run(argv, sock=None, connect_error=None, request_timeout=5.0, device=None):
    """Run the CLI with a fake connection. Returns (exit code, stdout, stderr)."""

    def fake_connect(address=None, *, timeout, log):
        if connect_error is not None:
            raise connect_error
        return Connection(sock, log, request_timeout=request_timeout), device or XM5

    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "connect", fake_connect), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def battery_headphones(payload: bytes):
    def respond(frame):
        if frame.type == CMD and frame.payload[0] == Command.BATTERY_GET:
            return [device_ack(frame.seq), device_frame(0, payload)]
        return well_behaved(frame)

    return respond


class BatteryCommandTest(unittest.TestCase):
    def test_battery(self):
        sock = FakeSocket(battery_headphones(bytes.fromhex("23 00 4b 00")))
        code, out, err = run(["battery"], sock)
        self.assertEqual((code, out, err), (0, "Battery: 75%\n", ""))
        # Handshake first, then exactly one battery request, and the socket is closed
        commands = sock.host_commands()
        self.assertEqual([f.payload[0] for f in commands], [Command.INIT_REQUEST, Command.BATTERY_GET])
        self.assertEqual(commands[1], Frame(CMD, 1, battery_request()))
        self.assertTrue(sock.closed)

    def test_charging(self):
        sock = FakeSocket(battery_headphones(bytes.fromhex("23 00 3c 01")))
        self.assertEqual(run(["battery"], sock)[1], "Battery: 60% (charging)\n")

    def test_notification_is_accepted_as_answer(self):
        sock = FakeSocket(battery_headphones(bytes.fromhex("25 00 64 03")))
        self.assertEqual(run(["battery"], sock)[1], "Battery: 100% (fully charged)\n")

    def test_debug_goes_to_stderr(self):
        sock = FakeSocket(battery_headphones(bytes.fromhex("23 00 4b 00")))
        code, out, err = run(["battery", "--debug"], sock)
        self.assertEqual(out, "Battery: 75%\n")
        self.assertIn(">> 3e 0c 01 00 00 00 02 22 00 31 3c", err)

    def test_no_answer(self):
        def ack_only(frame):
            if frame.type == CMD and frame.payload[0] == Command.BATTERY_GET:
                return [device_ack(frame.seq)]
            return well_behaved(frame)

        sock = FakeSocket(ack_only)
        code, out, err = run(["battery"], sock, request_timeout=0.1)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("error: no reply", err)
        self.assertTrue(sock.closed)

    def test_bluetooth_error(self):
        code, out, err = run(["battery"], connect_error=BluetoothError("Bluetooth is turned off"))
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "error: Bluetooth is turned off\n")

    def test_socket_error(self):
        code, _, err = run(["battery"], connect_error=ConnectionResetError(10054, "reset"))
        self.assertEqual(code, 1)
        self.assertIn("error: connection problem", err)


def nc_headphones(answers, notifications=()):
    """Headphones that answer NC/ambient queries per sub-type.

    answers maps sub-type -> reply payload hex (missing = ACK only, no reply).
    notifications are sent once, right after the host acknowledges an NC reply.
    """
    state = {"nc_replied": False, "notified": False}

    def respond(frame):
        if frame.type == CMD and frame.payload[0] == Command.NCASM_GET:
            chunks = [device_ack(frame.seq)]
            reply = answers.get(frame.payload[1])
            if reply is not None:
                chunks.append(device_frame(0, bytes.fromhex(reply)))
                state["nc_replied"] = True
            return chunks
        if frame.type == MessageType.ACK and state["nc_replied"] and not state["notified"]:
            state["notified"] = True
            return [device_frame(1, bytes.fromhex(n)) for n in notifications]
        return well_behaved(frame)

    respond.state = state
    return respond


class InterruptingSocket(FakeSocket):
    """Acts like the user pressing Ctrl+C once the notifications have all been read."""

    def recv(self, size):
        if not self.incoming and self.respond.state["notified"]:
            raise KeyboardInterrupt
        return super().recv(size)


class StatusCommandTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(headphones, "NCASM_QUERY_TIMEOUT", 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def nc_queries(self, sock):
        commands = sock.host_commands()
        # status must never change anything
        self.assertNotIn(Command.NCASM_SET, [f.payload[0] for f in commands])
        return [f.payload.hex(" ") for f in commands if f.payload[0] == Command.NCASM_GET]

    def test_noise_cancelling(self):
        sock = FakeSocket(nc_headphones({0x17: "67 17 01 01 00 00 0a"}))
        self.assertEqual(run(["status"], sock), (0, "Noise control: noise cancelling\n", ""))
        self.assertEqual(self.nc_queries(sock), ["66 17"])
        self.assertTrue(sock.closed)

    def test_real_xm5_reply(self):
        # Captured from a WH-1000XM5 with `sonyxm status --probe --debug`
        sock = FakeSocket(nc_headphones({0x17: "67 17 01 01 01 00 14"}))
        self.assertEqual(run(["status"], sock)[1], "Noise control: ambient sound, level 20\n")

    def test_ambient(self):
        sock = FakeSocket(nc_headphones({0x17: "67 17 01 01 01 00 0d"}))
        self.assertEqual(run(["status"], sock)[1], "Noise control: ambient sound, level 13\n")

    def test_ambient_focus_on_voice(self):
        sock = FakeSocket(nc_headphones({0x17: "67 17 01 01 01 01 14"}))
        self.assertEqual(run(["status"], sock)[1], "Noise control: ambient sound, level 20, focus on voice\n")

    def test_off(self):
        sock = FakeSocket(nc_headphones({0x17: "67 17 01 00 00 00 14"}))
        self.assertEqual(run(["status"], sock)[1], "Noise control: off\n")

    def test_falls_back_to_other_subtype(self):
        sock = FakeSocket(nc_headphones({0x15: "67 15 01 01 01 00 05"}))
        self.assertEqual(run(["status"], sock), (0, "Noise control: ambient sound, level 5\n", ""))
        self.assertEqual(self.nc_queries(sock), ["66 17", "66 15"])

    def test_no_subtype_answers(self):
        sock = FakeSocket(nc_headphones({}))
        code, out, err = run(["status"], sock)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("0x17: no reply", err)
        self.assertIn("0x15: no reply", err)

    def test_probe_asks_every_subtype(self):
        sock = FakeSocket(nc_headphones({0x15: "67 15 01 01 00 00 0a", 0x17: "67 17 01 01 00 00 0a"}))
        code, out, _ = run(["status", "--probe"], sock)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Model: WH-1000XM5, protocol v2\n"
                              "sub-type 0x17: Noise control: noise cancelling\n"
                              "sub-type 0x15: Noise control: noise cancelling\n")
        self.assertEqual(self.nc_queries(sock), ["66 17", "66 15"])

    def test_probe_reports_failures(self):
        sock = FakeSocket(nc_headphones({0x17: "67 15 01 01 00 00 0a"}))
        code, out, _ = run(["status", "--probe"], sock)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(lines[1], "sub-type 0x17: asked with sub-type 0x17, answered with 0x15")
        self.assertTrue(lines[2].startswith("sub-type 0x15: no reply"))

    def test_watch_prints_notifications(self):
        sock = InterruptingSocket(nc_headphones(
            {0x17: "67 17 01 01 00 00 0a"},
            notifications=["69 17 01 01 01 00 0a", "25 00 50 00", "69 17 01 00 00 00 0a"],
        ))
        code, out, err = run(["status", "--watch"], sock)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "Noise control: noise cancelling")
        self.assertEqual(lines[1], "Watching for changes; press Ctrl+C to stop.")
        self.assertRegex(lines[2], r"^\[\d\d:\d\d:\d\d\] Noise control: ambient sound, level 10$")
        self.assertRegex(lines[3], r"^\[\d\d:\d\d:\d\d\] Battery: 80%$")
        self.assertRegex(lines[4], r"^\[\d\d:\d\d:\d\d\] Noise control: off$")
        # Every notification (device seq 1) was acknowledged with seq 0
        self.assertEqual(sock.host_acks()[-3:], [Frame(MessageType.ACK, 0)] * 3)
        self.assertTrue(sock.closed)

    def test_watch_with_probe_shows_subtype(self):
        # Only the last probed sub-type answers, so the notification arrives once watching has begun
        sock = InterruptingSocket(nc_headphones(
            {0x15: "67 15 01 01 00 00 0a"}, notifications=["69 17 01 01 01 00 0a"],
        ))
        out = run(["status", "--watch", "--probe"], sock)[1]
        self.assertTrue(out.splitlines()[-1].endswith("Noise control: ambient sound, level 10 (sub-type 0x17)"))


NC = NcAsmMode.NOISE_CANCELLING
AMBIENT = NcAsmMode.AMBIENT
OFF = NcAsmMode.OFF


class ChangeCommandTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("NCASM_QUERY_TIMEOUT", 0.1), ("READBACK_RETRY_DELAY", 0)):
            patcher = mock.patch.object(headphones, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, argv, device):
        sock = FakeSocket(device)
        result = run(argv, sock)
        self.assertTrue(sock.closed)
        return result

    def test_nc_on_keeps_level_and_voice(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, True, 10))
        self.assertEqual(self.run_with(["nc", "on"], device), (0, "Noise control: noise cancelling\n", ""))
        self.assertEqual(device.writes, ["68 17 01 01 00 01 0a"])
        self.assertEqual(device.state, NcAsmState(NC, True, 10))

    def test_nc_off(self):
        device = SimulatedXm5(NcAsmState(NC, False, 20))
        self.assertEqual(self.run_with(["nc", "off"], device), (0, "Noise control: off\n", ""))
        self.assertEqual(device.writes, ["68 17 01 00 00 00 14"])

    def test_nc_off_from_ambient(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, False, 7))
        self.assertEqual(self.run_with(["nc", "off"], device)[1], "Noise control: off\n")
        self.assertEqual(device.writes, ["68 17 01 00 00 00 07"])

    def test_ambient_level(self):
        device = SimulatedXm5(NcAsmState(NC, False, 20))
        self.assertEqual(self.run_with(["ambient", "5"], device), (0, "Noise control: ambient sound, level 5\n", ""))
        self.assertEqual(device.writes, ["68 17 01 01 01 00 05"])

    def test_ambient_without_level_keeps_current_level(self):
        device = SimulatedXm5(NcAsmState(OFF, False, 12))
        self.assertEqual(self.run_with(["ambient"], device)[1], "Noise control: ambient sound, level 12\n")
        self.assertEqual(device.writes, ["68 17 01 01 01 00 0c"])

    def test_ambient_voice_flags(self):
        device = SimulatedXm5(NcAsmState(NC, False, 20))
        out = self.run_with(["ambient", "15", "--voice"], device)[1]
        self.assertEqual(out, "Noise control: ambient sound, level 15, focus on voice\n")
        self.assertEqual(device.writes, ["68 17 01 01 01 01 0f"])

        out = self.run_with(["ambient", "--no-voice"], device)[1]
        self.assertEqual(out, "Noise control: ambient sound, level 15\n")
        self.assertEqual(device.writes[-1], "68 17 01 01 01 00 0f")

    def test_ambient_extremes(self):
        for level in (1, 20):
            device = SimulatedXm5(NcAsmState(NC, False, 10))
            with self.subTest(level=level):
                self.assertEqual(self.run_with(["ambient", str(level)], device)[0], 0)
                self.assertEqual(device.state, NcAsmState(AMBIENT, False, level))

    def test_nothing_written_when_already_set(self):
        for argv, state in (
            (["nc", "on"], NcAsmState(NC, False, 10)),
            (["nc", "off"], NcAsmState(OFF, True, 3)),
            (["ambient", "8"], NcAsmState(AMBIENT, False, 8)),
        ):
            device = SimulatedXm5(state)
            with self.subTest(argv=argv):
                self.assertEqual(self.run_with(argv, device)[0], 0)
                self.assertEqual(device.writes, [])

    def test_uses_the_subtype_the_headphones_answer(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, False, 10), subtype=0x15)
        self.assertEqual(self.run_with(["nc", "on"], device)[0], 0)
        self.assertEqual(device.writes, ["68 15 01 01 00 00 0a"])

    def test_change_not_applied(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, False, 10), apply_writes=False)
        code, out, err = self.run_with(["nc", "on"], device)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("asked for noise cancelling, but the headphones report ambient sound, level 10", err)

    def test_level_refused(self):
        # What the real XM5 did with level 0, here with a device that refuses level 1
        device = SimulatedXm5(NcAsmState(NC, False, 10), min_level=2)
        code, _, err = self.run_with(["ambient", "1"], device)
        self.assertEqual(code, 1)
        self.assertIn("asked for ambient sound, level 1, but the headphones report ambient sound, level 2", err)

    def test_nc_never_writes_level_zero(self):
        device = SimulatedXm5(NcAsmState(AMBIENT, False, 0))
        self.assertEqual(self.run_with(["nc", "on"], device)[0], 0)
        self.assertEqual(device.writes, ["68 17 01 01 00 00 01"])

    def test_bad_arguments(self):
        for argv in (
            ["ambient", "0"], ["ambient", "21"], ["ambient", "-1"], ["ambient", "abc"], ["nc"], ["nc", "maybe"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                cli.main(argv)
            self.assertEqual(raised.exception.code, 2)


class EqCommandTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("NCASM_QUERY_TIMEOUT", 0.1), ("READBACK_RETRY_DELAY", 0)):
            patcher = mock.patch.object(headphones, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_with(self, argv, device):
        sock = FakeSocket(device)
        result = run(argv, sock)
        self.assertTrue(sock.closed)
        return result

    def test_show(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        self.assertEqual(
            self.run_with(["eq"], device),
            (0, "Equalizer: Off (Clear Bass +0; 400 Hz +0, 1 kHz +0, 2.5 kHz +0, 6.3 kHz +0, 16 kHz +0)\n", ""),
        )
        self.assertEqual(device.writes, [])

    def test_preset(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        code, out, _ = self.run_with(["eq", "preset", "bright"], device)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Equalizer: Bright (Clear Bass -1; 400 Hz +0, 1 kHz +5, 2.5 kHz +7, 6.3 kHz +7, "
                              "16 kHz +9)\n")
        self.assertEqual(device.writes, ["58 00 10 00"])

    def test_bands(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        code, out, _ = self.run_with(["eq", "bands", "1", "2", "3", "4", "5", "--clear-bass", "-2"], device)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Equalizer: Manual (Clear Bass -2; 400 Hz +1"))
        self.assertEqual(device.writes, ["58 00 a0 06 08 0b 0c 0d 0e 0f"])

    def test_bands_keep_the_current_custom_slot(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10), equalizer=Equalizer(EqPreset.CUSTOM_1, 0, (0, 0, 0, 0, 0)))
        code, out, _ = self.run_with(["eq", "bands", "1", "2", "3", "4", "5"], device)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Equalizer: Custom 1 (Clear Bass +0; 400 Hz +1"))
        self.assertEqual(device.writes, ["58 00 a1 06 0a 0b 0c 0d 0e 0f"])

    def test_bands_into_a_chosen_slot(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        code, out, _ = self.run_with(["eq", "bands", "0", "0", "0", "0", "1", "--slot", "custom_2"], device)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Equalizer: Custom 2 ("))
        self.assertEqual(device.writes, ["58 00 a2 06 0a 0a 0a 0a 0a 0b"])

    def test_options_before_the_action_are_kept(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        _, _, err = self.run_with(["eq", "--debug", "preset", "bright"], device)
        self.assertIn(">> 3e 0c", err)

    def test_not_applied(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10), apply_writes=False)
        code, out, err = self.run_with(["eq", "preset", "bass_boost"], device)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("asked for Bass Boost, but the headphones report Off", err)

    def test_bad_arguments(self):
        for argv in (["eq", "bands", "1", "2", "3", "4"], ["eq", "bands", "1", "2", "3", "4", "11"],
                     ["eq", "preset", "loud"], ["eq", "bands", "1", "2", "3", "4", "5", "--clear-bass", "x"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                cli.main(argv)
            self.assertEqual(raised.exception.code, 2)


XM4 = PairedDevice(name="WH-1000XM4", address="12:34:56:78:9A:BD", connected=True, authenticated=True)
XM6 = PairedDevice(name="WH-1000XM6", address="12:34:56:78:9A:BE", connected=True, authenticated=True)


class OtherModelsTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("NCASM_QUERY_TIMEOUT", 0.1), ("READBACK_RETRY_DELAY", 0)):
            patcher = mock.patch.object(headphones, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_connect_names_the_model(self):
        _, out, _ = run(["connect"], FakeSocket(simulated_xm6(NcAsmState(NC, False, 10))), device=XM6)
        self.assertIn("Model: WH-1000XM6, protocol v2 (not yet tested on these headphones by this project)", out)

    def test_xm4_wind(self):
        device = simulated_xm4(NcAsmState(NC, False, 10))
        code, out, _ = run(["nc", "wind"], FakeSocket(device), device=XM4)
        self.assertEqual((code, out), (0, "Noise control: wind noise reduction\n"))
        self.assertEqual(device.writes, ["68 02 11 02 01 01 00 00"])

    def test_wind_needs_an_xm4(self):
        device = SimulatedXm5(NcAsmState(NC, False, 10))
        code, out, err = run(["nc", "wind"], FakeSocket(device))
        self.assertEqual((code, out), (1, ""))
        self.assertIn("only available on the WH-1000XM4", err)
        self.assertEqual(device.writes, [])

    def test_xm6_auto_ambient(self):
        device = simulated_xm6(NcAsmState(AMBIENT, False, 10, auto_ambient=False))
        code, out, _ = run(["ambient", "--auto"], FakeSocket(device), device=XM6)
        self.assertEqual((code, out), (0, "Noise control: ambient sound, level 10, automatic level\n"))
        self.assertEqual(device.writes, ["68 19 01 01 01 00 0a 01 00"])

    def test_xm6_battery(self):
        device = simulated_xm6(NcAsmState(NC, False, 10))
        self.assertEqual(run(["battery"], FakeSocket(device), device=XM6)[1], "Battery: 75%\n")

    def test_xm6_ten_bands(self):
        device = simulated_xm6(NcAsmState(NC, False, 10))
        code, out, _ = run(["eq", "bands", "0", "1", "2", "3", "4", "5", "6", "-6", "-1", "0"], FakeSocket(device),
                           device=XM6)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("Equalizer: Manual (31 Hz +0, 63 Hz +1"))
        self.assertEqual(device.writes, ["58 00 a0 0a 06 07 08 09 0a 0b 0c 00 05 06"])

    def test_xm6_rejects_out_of_range_and_clear_bass(self):
        device = simulated_xm6(NcAsmState(NC, False, 10))
        code, _, err = run(["eq", "bands", *["7"] + ["0"] * 9], FakeSocket(device), device=XM6)
        self.assertEqual(code, 1)
        self.assertIn("from -6 to 6", err)
        code, _, err = run(["eq", "bands", *["0"] * 10, "--clear-bass", "1"], FakeSocket(device), device=XM6)
        self.assertEqual(code, 1)
        self.assertIn("no Clear Bass", err)
        self.assertEqual(device.writes, [])

    def test_band_count_is_checked_before_connecting(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as raised:
            cli.main(["eq", "bands", "1", "2", "3"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("give 5 values", err.getvalue())


class FormatNcAsmTest(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(cli.format_ncasm(NcAsmState(NcAsmMode.OFF, True, 3)), "Noise control: off")
        self.assertEqual(
            cli.format_ncasm(NcAsmState(NcAsmMode.NOISE_CANCELLING, True, 3)), "Noise control: noise cancelling"
        )
        self.assertEqual(
            cli.format_ncasm(NcAsmState(NcAsmMode.AMBIENT, False, 0)), "Noise control: ambient sound, level 0"
        )
        self.assertEqual(
            headphones.describe_noise_control(NcAsmState(NcAsmMode.AMBIENT, True, 4)),
            "ambient sound, level 4, focus on voice",
        )


class FormatBatteryTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(cli.format_battery(Battery(5, ChargingStatus.NOT_CHARGING)), "Battery: 5%")
        self.assertEqual(cli.format_battery(Battery(5, ChargingStatus.CHARGING)), "Battery: 5% (charging)")
        self.assertEqual(cli.format_battery(Battery(100, ChargingStatus.CHARGED)), "Battery: 100% (fully charged)")
        self.assertEqual(cli.format_battery(Battery(5, ChargingStatus.UNKNOWN)), "Battery: 5% (charging status 2)")
        self.assertEqual(cli.format_battery(Battery(5, 9)), "Battery: 5% (charging status 9)")


if __name__ == "__main__":
    unittest.main()

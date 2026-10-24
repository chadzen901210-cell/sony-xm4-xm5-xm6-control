"""Tests for the parts of sonyxm.tray that do not need Windows: tooltip, menu, labels and state."""

import unittest

from sonyxm.bluetooth import PairedDevice
from sonyxm.models import MODELS
from sonyxm.protocol import Battery, ChargingStatus, NcAsmMode, NcAsmState
from sonyxm.tray import DEFAULT_LABELS, Tray, TrayState, clean_labels, menu, tooltip
from sonyxm.worker import BatteryChanged, Connected, Disconnected, NoiseControlChanged

XM4_MODEL, XM5_MODEL, _ = MODELS
XM5 = PairedDevice(name="WH-1000XM5", address="12:34:56:78:9A:BC", connected=True, authenticated=True)
XM4 = PairedDevice(name="WH-1000XM4", address="12:34:56:78:9A:BD", connected=True, authenticated=True)


def texts(items):
    return [(item.text, item.enabled, item.checked) for item in items if item.id]


class TooltipTest(unittest.TestCase):
    def test_not_connected(self):
        self.assertEqual(tooltip(TrayState(), DEFAULT_LABELS), "XM Control\nNot connected")

    def test_connected(self):
        state = TrayState("WH-1000XM5", battery=80, charging=True, mode=NcAsmMode.AMBIENT)
        self.assertEqual(tooltip(state, DEFAULT_LABELS), "XM Control\nWH-1000XM5 · 80% ⚡\nAmbient sound")

    def test_fits_windows_limit(self):
        state = TrayState("x" * 200, battery=80)
        self.assertLessEqual(len(tooltip(state, DEFAULT_LABELS)), 127)


class MenuTest(unittest.TestCase):
    def test_not_connected(self):
        self.assertEqual(texts(menu(TrayState(), DEFAULT_LABELS)), [
            ("Not connected", False, False),
            ("Noise cancelling", False, False),
            ("Ambient sound", False, False),
            ("Off", False, False),
            ("Open XM Control", True, False),
            ("Quit", True, False),
        ])

    def test_connected_xm5(self):
        state = TrayState("WH-1000XM5", battery=55, mode=NcAsmMode.NOISE_CANCELLING)
        self.assertEqual(texts(menu(state, DEFAULT_LABELS))[:4], [
            ("WH-1000XM5 · 55%", False, False),
            ("Noise cancelling", True, True),
            ("Ambient sound", True, False),
            ("Off", True, False),
        ])

    def test_xm4_has_wind(self):
        state = TrayState("WH-1000XM4", wind=True, mode=NcAsmMode.WIND)
        self.assertIn(("Wind noise reduction", True, True), texts(menu(state, DEFAULT_LABELS)))


class LabelsTest(unittest.TestCase):
    def test_clean(self):
        labels = dict(DEFAULT_LABELS, quit="Save & quit")
        self.assertEqual(clean_labels(labels)["quit"], "Save && quit")

    def test_rejects(self):
        for bad in (None, [], {}, dict(DEFAULT_LABELS, extra="x"), dict(DEFAULT_LABELS, open=""),
                    dict(DEFAULT_LABELS, open="x" * 65), dict(DEFAULT_LABELS, open=1)):
            with self.subTest(bad=bad):
                self.assertIsNone(clean_labels(bad))


class StateTest(unittest.TestCase):
    def test_follows_events(self):
        # Not started: updates only change the state (no window to notify)
        tray = Tray(icon_path="icon.ico", on_open=lambda: None, on_mode=lambda mode: None, on_quit=lambda: None)
        tray.update(Connected(XM4, XM4_MODEL))
        tray.update(BatteryChanged(Battery(40, ChargingStatus.CHARGING)))
        tray.update(NoiseControlChanged(NcAsmState(NcAsmMode.WIND, False, 0)))
        state, labels = tray.snapshot()
        self.assertEqual(state, TrayState("WH-1000XM4", wind=True, battery=40, charging=True, mode=NcAsmMode.WIND))
        self.assertEqual(labels, DEFAULT_LABELS)
        tray.update(Disconnected("gone"))
        self.assertEqual(tray.snapshot()[0], TrayState())
        tray.update(Connected(XM5, XM5_MODEL))
        self.assertFalse(tray.snapshot()[0].wind)


if __name__ == "__main__":
    unittest.main()

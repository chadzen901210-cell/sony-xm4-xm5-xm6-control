"""Tests for sonyxm.webui: event messages, the event hub, and the local web server (on a real 127.0.0.1 port)."""

import http.client
import json
import os
import tempfile
import queue
import threading
import time
import unittest
from unittest import mock

from sonyxm import webui
from sonyxm.bluetooth import PairedDevice
from sonyxm.models import MODELS
from sonyxm.tray import DEFAULT_LABELS
from sonyxm.headtracker import HeadTrackerError
from sonyxm.protocol import Battery, ChargingStatus, EqPreset, Equalizer, NcAsmMode, NcAsmState
from sonyxm.webui import App, AppServer, EventHub, event_to_json
from sonyxm.worker import (
    BatteryChanged,
    CommandFinished,
    Connected,
    Connecting,
    Disconnected,
    EqualizerChanged,
    HeadPose,
    HeadStatus,
    NoiseControlChanged,
)

XM5 = PairedDevice(name="WH-1000XM5", address="12:34:56:78:9A:BC", connected=True, authenticated=True)
XM4 = PairedDevice(name="WH-1000XM4", address="12:34:56:78:9A:BD", connected=True, authenticated=True)
XM4_MODEL, XM5_MODEL, _ = MODELS


class EventJsonTest(unittest.TestCase):
    def test_messages(self):
        cases = [
            (Connecting(), {"type": "connection", "state": "connecting"}),
            (Connected(XM5, XM5_MODEL),
             {"type": "connection", "state": "connected", "name": "WH-1000XM5", "model": "WH-1000XM5", "tested": True,
              "wind": False, "headTracking": True, "eqPresets": [p.name.lower() for p in XM5_MODEL.eq_presets]}),
            (Connected(XM4, XM4_MODEL),
             {"type": "connection", "state": "connected", "name": "WH-1000XM4", "model": "WH-1000XM4",
              "tested": False, "wind": True, "headTracking": False,
              "eqPresets": [p.name.lower() for p in XM4_MODEL.eq_presets]}),
            (Disconnected("timed out", "unreachable"),
             {"type": "connection", "state": "disconnected", "reason": "timed out", "kind": "unreachable",
              "retryIn": 5.0}),
            (BatteryChanged(Battery(80, ChargingStatus.CHARGING)), {"type": "battery", "level": 80,
                                                                    "charging": "charging"}),
            (BatteryChanged(Battery(80, 7)), {"type": "battery", "level": 80, "charging": "unknown"}),
            (NoiseControlChanged(NcAsmState(NcAsmMode.AMBIENT, True, 12)),
             {"type": "noise", "mode": "ambient", "level": 12, "voice": True, "autoAmbient": None}),
            (NoiseControlChanged(NcAsmState(NcAsmMode.WIND, False, 0)),
             {"type": "noise", "mode": "wind", "level": 0, "voice": False, "autoAmbient": None}),
            (NoiseControlChanged(NcAsmState(NcAsmMode.AMBIENT, False, 5, auto_ambient=True)),
             {"type": "noise", "mode": "ambient", "level": 5, "voice": False, "autoAmbient": True}),
            (EqualizerChanged(Equalizer(EqPreset.BRIGHT, -1, (0, 5, 7, 7, 9))),
             {"type": "eq", "preset": "bright", "clearBass": -1, "bands": [0, 5, 7, 7, 9],
              "labels": ["400 Hz", "1 kHz", "2.5 kHz", "6.3 kHz", "16 kHz"], "stepMax": 10}),
            (EqualizerChanged(Equalizer(0x40, None, ())),
             {"type": "eq", "preset": None, "clearBass": None, "bands": [], "labels": [], "stepMax": 10}),
            (EqualizerChanged(Equalizer(EqPreset.HEAVY, None, (1, 2, 3, 4, 5, 6, -6, 0, 0, 0))),
             {"type": "eq", "preset": "heavy", "clearBass": None, "bands": [1, 2, 3, 4, 5, 6, -6, 0, 0, 0],
              "labels": ["31 Hz", "63 Hz", "125 Hz", "250 Hz", "500 Hz", "1 kHz", "2 kHz", "4 kHz", "8 kHz",
                         "16 kHz"], "stepMax": 6}),
            (CommandFinished(), {"type": "done", "error": None, "code": None}),
            (CommandFinished("too slow", "timeout"), {"type": "done", "error": "too slow", "code": "timeout"}),
            (HeadStatus("unavailable", "no sensor", "missing"),
             {"type": "headStatus", "state": "unavailable", "message": "no sensor", "code": "missing"}),
            (HeadPose((1.0, 0.0, 0.0, 0.0), 12.345, -3.21, 0.04, True, 24.94),
             {"type": "head", "q": [1.0, 0.0, 0.0, 0.0], "yaw": 12.3, "pitch": -3.2, "roll": 0.0, "settled": True,
              "rate": 24.9}),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(event_to_json(event), expected)


class EventHubTest(unittest.TestCase):
    def test_new_windows_get_the_latest_state(self):
        hub = EventHub()
        hub.publish(Connecting())
        hub.publish(Connected(XM5, XM5_MODEL))
        hub.publish(NoiseControlChanged(NcAsmState(NcAsmMode.OFF, False, 3)))
        hub.publish(CommandFinished("oops"))  # one-off: not replayed
        subscriber = hub.subscribe()
        replay = [subscriber.get_nowait() for _ in range(subscriber.qsize())]
        self.assertEqual([m["type"] for m in replay], ["connection", "noise"])
        self.assertEqual(replay[0]["state"], "connected")

    def test_live_events_and_unsubscribe(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        self.assertEqual(hub.window_count, 1)
        hub.publish(CommandFinished())
        self.assertEqual(subscriber.get_nowait(), {"type": "done", "error": None, "code": None})
        hub.unsubscribe(subscriber)
        self.assertEqual(hub.window_count, 0)

    def test_stalled_window_does_not_block(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        for _ in range(1000):
            hub.publish(HeadPose((1.0, 0.0, 0.0, 0.0), 0, 0, 0, True, 25))
        self.assertEqual(subscriber.qsize(), 256)

    def test_close_wakes_windows(self):
        hub = EventHub()
        subscriber = hub.subscribe()
        hub.close()
        self.assertIsNone(subscriber.get_nowait())


class StubWorker:
    def __init__(self):
        self.changes = []
        self.refreshes = 0
        self.eq = []

    def change(self, change):
        self.changes.append(change)

    def set_equalizer_preset(self, preset):
        self.eq.append(("preset", preset))

    def set_equalizer_bands(self, clear_bass, bands):
        self.eq.append(("bands", clear_bass, bands))

    def refresh(self):
        self.refreshes += 1


class StubHead:
    def __init__(self):
        self.enabled = []
        self.recenters = 0
        self.retries = 0

    def set_enabled(self, enabled):
        self.enabled.append(enabled)

    def recenter(self):
        self.recenters += 1

    def retry(self):
        self.retries += 1


class StubTray:
    def __init__(self):
        self.labels = []

    def set_labels(self, labels):
        self.labels.append(labels)


class ServerTest(unittest.TestCase):
    TOKEN = "test-token"

    def setUp(self):
        self.hub = EventHub()
        self.worker = StubWorker()
        self.head = StubHead()
        self.app = App(self.hub, self.worker, self.head)
        self.server = AppServer(self.app, self.TOKEN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.closing = True
        self.hub.close()
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, body=None, token=TOKEN, host=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if token is not None:
            headers["X-XM5-Token"] = token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        content = response.read()
        conn.close()
        return response, content

    # Pages

    def test_index(self):
        response, content = self.request("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"<title>XM Control</title>", content)
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))

    def test_static_files(self):
        for path, kind in (("/app.js", "text/javascript"), ("/i18n.js", "text/javascript"),
                           ("/head3d.js", "text/javascript"), ("/app.css", "text/css"), ("/icon.svg", "image/svg+xml")):
            with self.subTest(path=path):
                response, content = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertTrue(response.getheader("Content-Type").startswith(kind))
                self.assertTrue(content)

    def test_every_language_has_every_text(self):
        # Each language must translate every key English has (a missing one falls back to English)
        import re

        source = self.request("GET", "/i18n.js")[1].decode()
        blocks = re.split(r"\n  (?:\"[\w-]+\"|\w+): \{\n    name:", source)[1:]
        keys = [set(re.findall(r'"([\w.]+)":', block)) for block in blocks]
        self.assertEqual(len(keys), 10)
        for index, language_keys in enumerate(keys[1:], start=1):
            with self.subTest(language=index):
                self.assertEqual(language_keys, keys[0])

    def test_unknown_paths(self):
        for path in ("/nope", "/../webui.py", "/web/app.js", "/%2e%2e/webui.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 404)

    def test_wrong_host_is_refused(self):
        # A page served from evil.example that resolves to 127.0.0.1 (DNS rebinding)
        self.assertEqual(self.request("GET", "/", host=f"evil.example:{self.server.port}")[0].status, 403)
        response, _ = self.request("POST", "/api/refresh", {}, host="evil.example")
        self.assertEqual(response.status, 403)
        self.assertEqual(self.worker.refreshes, 0)

    # API

    def test_token_required(self):
        for token in (None, "", "wrong"):
            with self.subTest(token=token):
                self.assertEqual(self.request("POST", "/api/refresh", {}, token=token)[0].status, 403)
        self.assertEqual(self.worker.refreshes, 0)

    def test_noise_change(self):
        response, content = self.request("POST", "/api/noise", {"mode": "ambient", "level": 7})
        self.assertEqual(response.status, 202, content)
        (change,) = self.worker.changes
        current = NcAsmState(NcAsmMode.NOISE_CANCELLING, True, 12)
        self.assertEqual(change(current), NcAsmState(NcAsmMode.AMBIENT, True, 7))

    def test_noise_change_keeps_unmentioned_settings(self):
        self.request("POST", "/api/noise", {"mode": "off"})
        self.request("POST", "/api/noise", {"mode": "ambient", "voice": False})
        current = NcAsmState(NcAsmMode.AMBIENT, True, 12)
        self.assertEqual(self.worker.changes[0](current), NcAsmState(NcAsmMode.OFF, True, 12))
        self.assertEqual(self.worker.changes[1](current), NcAsmState(NcAsmMode.AMBIENT, False, 12))

    def test_noise_change_validation(self):
        bad = [
            {},
            {"mode": "loud"},
            {"mode": "ambient", "level": 0},
            {"mode": "ambient", "level": 21},
            {"mode": "ambient", "level": "5"},
            {"mode": "ambient", "level": True},
            {"mode": "ambient", "level": 5.5},
            {"mode": "ambient", "voice": "yes"},
            {"mode": "ambient", "autoAmbient": "yes"},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/noise", body)[0].status, 400)
        self.assertEqual(self.worker.changes, [])

    def test_noise_wind_and_auto_ambient(self):
        self.assertEqual(self.request("POST", "/api/noise", {"mode": "wind"})[0].status, 202)
        self.assertEqual(self.request("POST", "/api/noise", {"mode": "ambient", "autoAmbient": True})[0].status, 202)
        current = NcAsmState(NcAsmMode.AMBIENT, False, 8, auto_ambient=False)
        self.assertEqual(self.worker.changes[0](current).mode, NcAsmMode.WIND)
        self.assertEqual(self.worker.changes[1](current), NcAsmState(NcAsmMode.AMBIENT, False, 8, auto_ambient=True))
        # Unmentioned, it stays as the headphones have it
        self.request("POST", "/api/noise", {"mode": "ambient", "level": 3})
        self.assertEqual(self.worker.changes[2](current).auto_ambient, False)

    def test_equalizer(self):
        self.assertEqual(self.request("POST", "/api/eq", {"preset": "bass_boost"})[0].status, 202)
        self.assertEqual(self.request("POST", "/api/eq", {"clearBass": -3, "bands": [1, 2, 3, 4, 5]})[0].status, 202)
        ten = [0, 1, 2, 3, 4, 5, 6, -6, -1, 0]
        self.assertEqual(self.request("POST", "/api/eq", {"clearBass": None, "bands": ten})[0].status, 202)
        self.assertEqual(self.request("POST", "/api/eq", {"preset": "heavy"})[0].status, 202)
        self.assertEqual(self.worker.eq, [("preset", EqPreset.BASS_BOOST), ("bands", -3, (1, 2, 3, 4, 5)),
                                          ("bands", None, tuple(ten)), ("preset", EqPreset.HEAVY)])

    def test_equalizer_validation(self):
        bad = [
            {"preset": "loud"},
            {"clearBass": 0, "bands": [0, 0, 0, 0]},
            {"clearBass": 0, "bands": [0, 0, 0, 0, 11]},
            {"clearBass": 0, "bands": 5},
            {"clearBass": "0", "bands": [0, 0, 0, 0, 0]},
            {"bands": [0, 0, 0, 0, 0]},
            {"clearBass": 0, "bands": [0] * 10},  # no Clear Bass with 10 bands
            {"clearBass": None, "bands": [0] * 9 + [7]},  # 10 bands go from -6 to +6
            {"clearBass": None, "bands": [0] * 6},
            {},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/eq", body)[0].status, 400)
        self.assertEqual(self.worker.eq, [])

    def test_tray_labels(self):
        tray = StubTray()
        self.app.tray = tray
        labels = dict(DEFAULT_LABELS, open="XM Control 開啟", quit="結束 & 離開")
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(webui, "_labels_file", lambda: os.path.join(folder, "labels.json")):
            self.assertEqual(self.request("POST", "/api/labels", labels)[0].status, 202)
            self.assertEqual(tray.labels[-1]["quit"], "結束 && 離開")  # "&" would underline in a menu
            with mock.patch.object(webui, "_labels_file", lambda: os.path.join(folder, "labels.json")):
                self.assertEqual(webui._saved_labels(), tray.labels[-1])
            for bad in ({}, dict(labels, extra="x"), dict(labels, open=""), dict(labels, open="x" * 65),
                        dict(labels, quit=3)):
                with self.subTest(bad=bad):
                    self.assertEqual(self.request("POST", "/api/labels", bad)[0].status, 400)
        self.assertEqual(len(tray.labels), 1)

    def test_bad_bodies(self):
        self.assertEqual(self.request("POST", "/api/refresh", raw=b"not json")[0].status, 400)
        self.assertEqual(self.request("POST", "/api/refresh", raw=b"[1, 2]")[0].status, 400)
        self.assertEqual(self.request("POST", "/api/refresh", raw=b"{" + b" " * 5000 + b"}")[0].status, 400)
        self.assertEqual(self.request("POST", "/api/unknown", {})[0].status, 404)

    def test_other_commands(self):
        self.assertEqual(self.request("POST", "/api/refresh", {})[0].status, 202)
        self.assertEqual(self.request("POST", "/api/head", {"enabled": True})[0].status, 202)
        self.assertEqual(self.request("POST", "/api/head", {"enabled": "yes"})[0].status, 400)
        self.assertEqual(self.request("POST", "/api/head/recenter", {})[0].status, 202)
        self.assertEqual((self.worker.refreshes, self.head.enabled, self.head.recenters), (1, [True], 1))

    def test_events_need_the_token(self):
        self.assertEqual(self.request("GET", "/api/events", token=None)[0].status, 403)
        self.assertEqual(self.request("GET", "/api/events?t=wrong", token=None)[0].status, 403)

    def test_event_stream(self):
        self.hub.publish(Connected(XM5, XM5_MODEL))
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        conn.request("GET", f"/api/events?t={self.TOKEN}", headers={"Host": f"127.0.0.1:{self.server.port}"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        first = json.loads(response.fp.readline().decode().removeprefix("data: "))
        self.assertEqual(first["state"], "connected")
        response.fp.readline()  # blank line ends the event
        deadline = time.monotonic() + 2
        while self.hub.window_count == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.hub.publish(CommandFinished("busy"))
        second = json.loads(response.fp.readline().decode().removeprefix("data: "))
        self.assertEqual(second, {"type": "done", "error": "busy", "code": None})
        conn.close()


class RepairTest(unittest.TestCase):
    def setUp(self):
        self.hub = EventHub()
        self.subscriber = self.hub.subscribe()
        self.head = StubHead()
        self.app = App(self.hub, StubWorker(), self.head)

    def statuses(self):
        deadline = time.monotonic() + 2
        while self.head.retries == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        messages = []
        while True:
            try:
                messages.append(self.subscriber.get_nowait())
            except queue.Empty:
                return messages

    def test_success(self):
        with mock.patch.object(webui.headtracker, "run_elevated", return_value=0) as run:
            self.app.repair_head_tracking()
            messages = self.statuses()
        run.assert_called_once_with(["head", "--repair"], wait=True)
        self.assertEqual([m["state"] for m in messages], ["repairing"])
        self.assertEqual(self.head.retries, 1)

    def test_failure(self):
        with mock.patch.object(webui.headtracker, "run_elevated", return_value=1):
            self.app.repair_head_tracking()
            messages = self.statuses()
        self.assertEqual([m["state"] for m in messages], ["repairing", "unavailable"])
        self.assertIn("did not finish", messages[-1]["message"])
        self.assertEqual(messages[-1]["code"], "repair_failed")

    def test_declined(self):
        with mock.patch.object(webui.headtracker, "run_elevated",
                               side_effect=HeadTrackerError("administrator rights were not granted")):
            self.app.repair_head_tracking()
            messages = self.statuses()
        self.assertIn("administrator rights were not granted", messages[-1]["message"])
        self.assertEqual(messages[-1]["code"], "repair_declined")


class WaitForWindowsTest(unittest.TestCase):
    def setUp(self):
        for name, value in (("IDLE_EXIT_DELAY", 0.2), ("FIRST_WINDOW_TIMEOUT", 0.3)):
            patcher = mock.patch.object(webui, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_gives_up_when_no_window_opens(self):
        start = time.monotonic()
        webui.wait_for_windows(EventHub())
        self.assertLess(time.monotonic() - start, 1.5)

    def test_returns_after_the_last_window_closes(self):
        hub = EventHub()
        window = hub.subscribe()
        done = threading.Event()
        threading.Thread(target=lambda: (webui.wait_for_windows(hub), done.set()), daemon=True).start()
        time.sleep(0.5)
        self.assertFalse(done.is_set())  # a window is open
        hub.unsubscribe(window)
        self.assertTrue(done.wait(2))


class BackgroundTest(unittest.TestCase):
    def test_head_tracking_stops_when_the_last_window_closes(self):
        hub = EventHub()
        head = StubHead()
        stop = threading.Event()
        thread = threading.Thread(target=webui.run_in_background, args=(hub, head, stop), daemon=True)
        window = hub.subscribe()
        thread.start()
        time.sleep(0.4)
        self.assertEqual(head.enabled, [])
        hub.unsubscribe(window)
        deadline = time.monotonic() + 2
        while not head.enabled and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(head.enabled, [False])
        stop.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()

"""The app window: a small local web server and an Edge "app mode" window.

The server only listens on 127.0.0.1, and every API call must carry a random
token that only the window this program opened knows, so web pages in a
browser cannot control the headphones. Closing the window leaves the program
running in the notification area (tray.py); its menu quits it. Without a tray
icon, closing the window quits the program.
"""

from __future__ import annotations

import ctypes
import dataclasses
import http.server
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from importlib import resources
from typing import Any

from . import headtracker
from .connection import DEFAULT_CONNECT_TIMEOUT, Log
from .headtracker import HeadTrackerError
from .tray import DEFAULT_LABELS, Tray, clean_labels, focus_window
from .protocol import (
    AMBIENT_LEVEL_MAX,
    AMBIENT_LEVEL_MIN,
    EQ_BAND_NAMES,
    EQ_BAND_NAMES_10,
    EQ_STEP_MAX,
    EQ_STEP_MAX_10,
    EQ_STEP_MIN,
    ChargingStatus,
    EqPreset,
    NcAsmMode,
    NcAsmState,
    eq_band_names,
    eq_step_max,
)
from .worker import (
    RECONNECT_DELAY,
    BatteryChanged,
    CommandFinished,
    Connected,
    Connecting,
    Disconnected,
    EqualizerChanged,
    Event,
    HeadPose,
    HeadStatus,
    HeadWorker,
    NoiseControlChanged,
    Worker,
)

APP_NAME = "XM Control"
WINDOW_SIZE = (440, 900)
# Quit once no window has been open for this long (a reload reconnects well within it)
IDLE_EXIT_DELAY = 3.0
# Give up if the window never connects
FIRST_WINDOW_TIMEOUT = 60.0
# How long a newly opened window may take to appear before another one is opened
WINDOW_START_TIME = 4.0
# The project's PayPal donation page, opened by the window's "Support" link
DONATE_URL = "https://www.paypal.com/ncp/payment/RBKTRJ9542BKC"
STATIC_FILES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "i18n.js": "text/javascript; charset=utf-8",
    "head3d.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_CHARGING = {ChargingStatus.CHARGING: "charging", ChargingStatus.CHARGED: "charged",
             ChargingStatus.NOT_CHARGING: "no"}


def event_to_json(event: Event) -> dict[str, Any] | None:
    if isinstance(event, Connecting):
        return {"type": "connection", "state": "connecting"}
    if isinstance(event, Connected):
        model = event.model
        return {"type": "connection", "state": "connected", "name": event.device.name, "model": model.name,
                "tested": model.tested, "wind": model.wind, "headTracking": model.head_tracking,
                "eqPresets": [p.name.lower() for p in model.eq_presets]}
    if isinstance(event, Disconnected):
        return {"type": "connection", "state": "disconnected", "reason": event.reason, "kind": event.kind,
                "retryIn": RECONNECT_DELAY}
    if isinstance(event, BatteryChanged):
        return {"type": "battery", "level": event.battery.level,
                "charging": _CHARGING.get(event.battery.charging, "unknown")}
    if isinstance(event, NoiseControlChanged):
        s = event.state
        return {"type": "noise", "mode": s.mode.value, "level": s.ambient_level, "voice": s.focus_on_voice,
                "autoAmbient": s.auto_ambient}
    if isinstance(event, EqualizerChanged):
        eq = event.equalizer
        preset = eq.preset.name.lower() if isinstance(eq.preset, EqPreset) else None
        return {"type": "eq", "preset": preset, "clearBass": eq.clear_bass, "bands": list(eq.bands),
                "labels": list(eq_band_names(len(eq.bands))) if eq.bands else [],
                "stepMax": eq_step_max(len(eq.bands))}
    if isinstance(event, CommandFinished):
        return {"type": "done", "error": event.error, "code": event.code}
    if isinstance(event, HeadStatus):
        return {"type": "headStatus", "state": event.state, "message": event.message, "code": event.code}
    if isinstance(event, HeadPose):
        return {"type": "head", "q": [round(v, 5) for v in event.quaternion], "yaw": round(event.yaw, 1),
                "pitch": round(event.pitch, 1), "roll": round(event.roll, 1), "settled": event.settled,
                "rate": round(event.rate, 1)}
    return None


class EventHub:
    """Sends every event to every open window, and the latest state to windows that open later."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[dict[str, Any] | None]] = []
        self._latest: dict[str, dict[str, Any]] = {}

    def publish(self, event: Event | dict[str, Any]) -> None:
        message = event if isinstance(event, dict) else event_to_json(event)
        if message is None:
            return
        with self._lock:
            if message["type"] != "done":
                self._latest[message["type"]] = message
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                pass  # a stalled window misses some head poses rather than piling them up

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        subscriber: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
        with self._lock:
            for message in self._latest.values():
                subscriber.put_nowait(message)
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    @property
    def window_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass


class App:
    """What the window can ask for."""

    def __init__(self, hub: EventHub, worker: Worker, head: HeadWorker, tray: Tray | None = None) -> None:
        self.hub = hub
        self.worker = worker
        self.head = head
        self.tray = tray
        self._repairing = False

    def set_tray_labels(self, labels: dict[str, str]) -> None:
        """Menu texts in the window's language; kept for the next start too."""
        if self.tray is None:
            return
        self.tray.set_labels(labels)
        try:
            with open(_labels_file(), "w", encoding="utf-8") as f:
                json.dump(labels, f, ensure_ascii=False)
        except OSError:
            pass

    def set_noise_control(
        self, mode: NcAsmMode, level: int | None, voice: bool | None, auto_ambient: bool | None = None
    ) -> None:
        def change(current: NcAsmState) -> NcAsmState:
            return dataclasses.replace(
                current,
                mode=mode,
                ambient_level=current.ambient_level if level is None else level,
                focus_on_voice=current.focus_on_voice if voice is None else voice,
                auto_ambient=current.auto_ambient if auto_ambient is None else auto_ambient,
            )

        self.worker.change(change)

    def repair_head_tracking(self) -> None:
        if self._repairing:
            return
        self._repairing = True
        self.hub.publish(HeadStatus("repairing", "Approve the Windows prompt to let the repair run."))

        def repair() -> None:
            failure = None
            try:
                if headtracker.run_elevated(["head", "--repair"], wait=True) != 0:
                    failure = HeadStatus("unavailable", "The repair did not finish. Turn the headphones off and "
                                                        "on, then try again.", "repair_failed")
            except HeadTrackerError as e:
                failure = HeadStatus("unavailable", f"Repair not started: {e}.", "repair_declined")
            finally:
                self._repairing = False
            if failure:
                self.hub.publish(failure)
            self.head.retry()

        threading.Thread(target=repair, name="sonyxm-repair", daemon=True).start()


class AppServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: App, token: str, log: Log | None = None) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.app = app
        self.token = token
        self.log = log
        self.closing = False

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


class Handler(http.server.BaseHTTPRequestHandler):
    server: AppServer
    server_version = "sonyxm"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        if self.server.log:
            self.server.log("http: " + format % args)

    # Routing

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send_json(403, {"error": "forbidden"})
        path, _, query = self.path.partition("?")
        if path == "/":
            path = "/index.html"
        if path == "/api/events":
            if not secrets.compare_digest(_query_value(query, "t"), self.server.token):
                return self._send_json(403, {"error": "forbidden"})
            return self._events()
        name = path.lstrip("/")
        if name in STATIC_FILES:
            return self._static(name)
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._host_ok() or not secrets.compare_digest(self.headers.get("X-XM5-Token", ""), self.server.token):
            return self._send_json(403, {"error": "forbidden"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= 4096:
            return self._send_json(400, {"error": "bad request"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "not JSON"})
        if not isinstance(body, dict):
            return self._send_json(400, {"error": "expected an object"})

        app = self.server.app
        path = self.path.partition("?")[0]
        if path == "/api/noise":
            error = self._noise(app, body)
            if error:
                return self._send_json(400, {"error": error})
        elif path == "/api/eq":
            error = self._equalizer(app, body)
            if error:
                return self._send_json(400, {"error": error})
        elif path == "/api/refresh":
            app.worker.refresh()
        elif path == "/api/head":
            if not isinstance(body.get("enabled"), bool):
                return self._send_json(400, {"error": "enabled must be true or false"})
            app.head.set_enabled(body["enabled"])
        elif path == "/api/head/recenter":
            app.head.recenter()
        elif path == "/api/head/repair":
            app.repair_head_tracking()
        elif path == "/api/donate":
            # Always this one address: the window cannot make the app open anything else
            webbrowser.open(DONATE_URL)
        elif path == "/api/labels":
            labels = clean_labels(body)
            if labels is None:
                return self._send_json(400, {"error": f"expected the texts {', '.join(DEFAULT_LABELS)}"})
            app.set_tray_labels(labels)
        else:
            return self._send_json(404, {"error": "not found"})
        self._send_json(202, {"ok": True})

    @staticmethod
    def _noise(app: App, body: dict[str, Any]) -> str | None:
        try:
            mode = NcAsmMode(body.get("mode"))
        except ValueError:
            return "mode must be noise_cancelling, ambient, wind or off"
        level = body.get("level")
        if level is not None and (type(level) is not int or not AMBIENT_LEVEL_MIN <= level <= AMBIENT_LEVEL_MAX):
            return f"level must be a whole number from {AMBIENT_LEVEL_MIN} to {AMBIENT_LEVEL_MAX}"
        voice = body.get("voice")
        if voice is not None and not isinstance(voice, bool):
            return "voice must be true or false"
        auto_ambient = body.get("autoAmbient")
        if auto_ambient is not None and not isinstance(auto_ambient, bool):
            return "autoAmbient must be true or false"
        app.set_noise_control(mode, level, voice, auto_ambient)
        return None

    @staticmethod
    def _equalizer(app: App, body: dict[str, Any]) -> str | None:
        """{"preset": "bright"}, {"clearBass": 0, "bands": [5 values]} or {"clearBass": null, "bands": [10 values]}."""
        if "preset" in body:
            try:
                preset = EqPreset[str(body["preset"]).upper()]
            except KeyError:
                return "unknown preset"
            app.worker.set_equalizer_preset(preset)
            return None
        bands = body.get("bands")
        clear_bass = body.get("clearBass")
        if not isinstance(bands, list) or len(bands) not in (len(EQ_BAND_NAMES), len(EQ_BAND_NAMES_10)):
            return f"bands must be a list of {len(EQ_BAND_NAMES)} or {len(EQ_BAND_NAMES_10)} values"
        step_max = eq_step_max(len(bands))
        values = [clear_bass, *bands] if len(bands) == len(EQ_BAND_NAMES) else bands
        if (len(bands) == len(EQ_BAND_NAMES_10) and clear_bass is not None) or any(
            type(v) is not int or not -step_max <= v <= step_max for v in values
        ):
            return (f"clearBass and {len(EQ_BAND_NAMES)} bands must be whole numbers from {EQ_STEP_MIN} to "
                    f"{EQ_STEP_MAX}, or clearBass null and {len(EQ_BAND_NAMES_10)} bands from {-EQ_STEP_MAX_10} "
                    f"to {EQ_STEP_MAX_10}")
        app.worker.set_equalizer_bands(clear_bass, tuple(bands))
        return None

    # Responses

    def _host_ok(self) -> bool:
        # Rejects requests that reach us through a hostile DNS name (DNS rebinding)
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _static(self, name: str) -> None:
        data = resources.files("sonyxm").joinpath("web", name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_FILES[name])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if name.endswith(".html"):
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _events(self) -> None:
        """Server-Sent Events: one JSON message per event, for as long as the window is open."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        hub = self.server.app.hub
        subscriber = hub.subscribe()
        try:
            while not self.server.closing:
                try:
                    message = subscriber.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                if message is None:
                    break
                self.wfile.write(b"data: " + json.dumps(message).encode() + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass  # the window went away
        finally:
            hub.unsubscribe(subscriber)


def _query_value(query: str, key: str) -> str:
    from urllib.parse import parse_qs

    values = parse_qs(query).get(key)
    return values[0] if values else ""


# --- Window -------------------------------------------------------------------


def data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "XMControl")
    os.makedirs(path, exist_ok=True)
    return path


def find_edge() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as key:
                path = winreg.QueryValue(key, None)
                if path and os.path.isfile(path):
                    return path
        except OSError:
            pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.isfile(path):
                return path
    return None


def open_window(url: str) -> subprocess.Popen | None:
    """A standalone app window (Edge app mode with its own profile); a browser tab if Edge is missing."""
    edge = find_edge()
    if edge is None:
        webbrowser.open(url)
        return None
    width, height = WINDOW_SIZE
    return subprocess.Popen([
        edge, f"--app={url}", f"--user-data-dir={os.path.join(data_dir(), 'window')}",
        f"--window-size={width},{height}", "--no-first-run", "--no-default-browser-check",
        "--disable-sync", "--disable-extensions", "--disable-default-apps",
    ])


class _SingleInstance:
    """One app per user: a second start just opens another window onto the first."""

    def __init__(self) -> None:
        self.first = True
        self._mutex = None
        self._file = os.path.join(data_dir(), "instance.json")
        if sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            self._mutex = kernel32.CreateMutexW(None, False, "Local\\XMControl")
            self.first = ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

    def running_url(self) -> str | None:
        try:
            with open(self._file, encoding="utf-8") as f:
                return json.load(f)["url"]
        except (OSError, ValueError, KeyError):
            return None

    def publish(self, url: str) -> None:
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump({"url": url, "pid": os.getpid()}, f)

    def release(self) -> None:
        try:
            os.remove(self._file)
        except OSError:
            pass


def wait_for_windows(hub: EventHub, stop: threading.Event | None = None) -> None:
    """Return once every window has been closed (or none ever opened)."""
    stop = stop or threading.Event()
    deadline = time.monotonic() + FIRST_WINDOW_TIMEOUT
    while hub.window_count == 0:
        if time.monotonic() > deadline or stop.wait(0.2):
            return
    idle_since = None
    while not stop.wait(0.25):
        if hub.window_count:
            idle_since = None
        elif idle_since is None:
            idle_since = time.monotonic()
        elif time.monotonic() - idle_since > IDLE_EXIT_DELAY:
            return


def run_in_background(hub: EventHub, head: HeadWorker, stop: threading.Event) -> None:
    """Keep going until `stop` (the tray menu's Quit). Head tracking only runs while a window shows it."""
    windows = hub.window_count
    while not stop.wait(0.25):
        count = hub.window_count
        if windows and not count:
            head.set_enabled(False)  # nobody sees it; save the headphones' battery
        windows = count


def _labels_file() -> str:
    return os.path.join(data_dir(), "tray-labels.json")


def _saved_labels() -> dict[str, str] | None:
    try:
        with open(_labels_file(), encoding="utf-8") as f:
            return clean_labels({k: v.replace("&&", "&") for k, v in json.load(f).items()})
    except (OSError, ValueError, AttributeError):
        return None


def run(address: str | None = None, timeout: float = DEFAULT_CONNECT_TIMEOUT, log: Log | None = None) -> int:
    instance = _SingleInstance()
    if not instance.first:
        url = instance.running_url()
        if url:
            if not focus_window():
                open_window(url)
            return 0

    hub = EventHub()
    stop = threading.Event()
    app: App

    opened_at = [-WINDOW_START_TIME]

    def show_window() -> None:
        if focus_window():
            return
        # A double click asks twice; the window started by the first click may not be up yet
        if time.monotonic() - opened_at[0] < WINDOW_START_TIME and not hub.window_count:
            return
        opened_at[0] = time.monotonic()
        open_window(server.url)

    tray = Tray(
        icon_path=str(resources.files("sonyxm").joinpath("web", "icon.ico")),
        on_open=show_window,
        on_mode=lambda mode: app.set_noise_control(mode, None, None),
        on_quit=stop.set,
        labels=_saved_labels(),
        log=log,
    )

    def publish(event: Event) -> None:
        hub.publish(event)
        tray.update(event)

    worker = Worker(publish, address=address, timeout=timeout, log=log)
    head = HeadWorker(publish, log=log)
    app = App(hub, worker, head, tray)
    server = AppServer(app, secrets.token_urlsafe(24), log)
    threading.Thread(target=server.serve_forever, name="sonyxm-http", daemon=True).start()
    worker.start()
    head.start()
    instance.publish(server.url)
    if log:
        log(f"window address: http://127.0.0.1:{server.port}/")
    in_tray = tray.start()
    if not in_tray:
        app.tray = None
    open_window(server.url)
    try:
        if in_tray:
            run_in_background(hub, head, stop)
        else:
            wait_for_windows(hub, stop)
    except KeyboardInterrupt:
        pass
    finally:
        if hub.window_count:
            hub.publish({"type": "quit"})  # open windows close themselves
            time.sleep(0.3)
        server.closing = True
        hub.close()
        head.stop()
        worker.stop()
        tray.stop()
        server.shutdown()
        instance.release()
        head.join(timeout=2)
        worker.join(timeout=2)
    return 0

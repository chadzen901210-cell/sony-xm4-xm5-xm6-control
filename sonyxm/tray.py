"""The notification area ("system tray") icon that keeps the app running after its window closes.

Shows the headphones and battery level in its tooltip; its menu switches the
noise control mode, reopens the window, or quits. Win32 through ctypes, on a
thread of its own with its own message loop.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from .connection import Log
from .protocol import ChargingStatus, NcAsmMode
from .worker import BatteryChanged, Connected, Connecting, Disconnected, Event, NoiseControlChanged

APP_NAME = "XM Control"
# Menu texts until the window sends them in the user's language (/api/labels)
DEFAULT_LABELS = {
    "open": "Open XM Control",
    "quit": "Quit",
    "noise_cancelling": "Noise cancelling",
    "ambient": "Ambient sound",
    "wind": "Wind noise reduction",
    "off": "Off",
    "notConnected": "Not connected",
}
LABEL_MAX_LENGTH = 64

_ID_OPEN = 1
_ID_QUIT = 2
_ID_HEADER = 3
_MODE_IDS = {10: NcAsmMode.NOISE_CANCELLING, 11: NcAsmMode.AMBIENT, 12: NcAsmMode.WIND, 13: NcAsmMode.OFF}


@dataclass
class TrayState:
    name: str | None = None  # headphones, while connected
    wind: bool = False
    battery: int | None = None
    charging: bool = False
    mode: NcAsmMode | None = None


@dataclass(frozen=True)
class MenuItem:
    id: int  # 0 for a separator
    text: str = ""
    enabled: bool = True
    checked: bool = False


def tooltip(state: TrayState, labels: dict[str, str]) -> str:
    if state.name is None:
        return f"{APP_NAME}\n{labels['notConnected']}"
    line = state.name
    if state.battery is not None:
        line += f" · {state.battery}%" + (" ⚡" if state.charging else "")
    if state.mode is not None:
        line += f"\n{labels[state.mode.value]}"
    return f"{APP_NAME}\n{line}"[:127]


def menu(state: TrayState, labels: dict[str, str]) -> list[MenuItem]:
    header = labels["notConnected"] if state.name is None else state.name + (
        f" · {state.battery}%" if state.battery is not None else "")
    items = [MenuItem(_ID_HEADER, header, enabled=False), MenuItem(0)]
    for item_id, mode in _MODE_IDS.items():
        if mode is NcAsmMode.WIND and not state.wind:
            continue
        items.append(MenuItem(item_id, labels[mode.value], enabled=state.name is not None,
                              checked=state.name is not None and state.mode is mode))
    items += [MenuItem(0), MenuItem(_ID_OPEN, labels["open"]), MenuItem(_ID_QUIT, labels["quit"])]
    return items


def clean_labels(raw: object) -> dict[str, str] | None:
    """The labels from the window, or None if they are not what we expect."""
    if not isinstance(raw, dict) or set(raw) != set(DEFAULT_LABELS):
        return None
    if not all(isinstance(v, str) and 0 < len(v) <= LABEL_MAX_LENGTH for v in raw.values()):
        return None
    return {k: v.replace("&", "&&") for k, v in raw.items()}  # "&" would underline in a menu


# --- Win32 --------------------------------------------------------------------

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_NULL = 0x0000
_WM_LBUTTONUP = 0x0202
_WM_RBUTTONUP = 0x0205
_WM_CONTEXTMENU = 0x007B
_WM_APP = 0x8000
_WM_TRAY = _WM_APP + 1  # mouse events on the icon
_WM_REFRESH = _WM_APP + 2  # state changed: update the tooltip
_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP = 0x1, 0x2, 0x4
_MF_STRING, _MF_GRAYED, _MF_CHECKED, _MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
_TPM_RIGHTBUTTON, _TPM_RETURNCMD, _TPM_NONOTIFY = 0x2, 0x100, 0x80
_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x10
_SM_CXSMICON, _SM_CYSMICON = 49, 50
_IDI_APPLICATION = 32512
_SW_RESTORE = 9


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", _GUID), ("hBalloonIcon", wintypes.HICON),
    ]


def _api() -> tuple[ctypes.WinDLL, ctypes.WinDLL, ctypes.WinDLL]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    H, U, I, B = wintypes.HWND, wintypes.UINT, ctypes.c_int, wintypes.BOOL  # noqa: E741
    for name, restype, argtypes in (
        ("RegisterClassExW", wintypes.ATOM, [ctypes.POINTER(_WNDCLASSEXW)]),
        ("UnregisterClassW", B, [wintypes.LPCWSTR, wintypes.HINSTANCE]),
        ("CreateWindowExW", H, [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, I, I, I, I,
                                H, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]),
        ("DefWindowProcW", LRESULT, [H, U, wintypes.WPARAM, wintypes.LPARAM]),
        ("DestroyWindow", B, [H]),
        ("GetMessageW", I, [ctypes.POINTER(wintypes.MSG), H, U, U]),
        ("TranslateMessage", B, [ctypes.POINTER(wintypes.MSG)]),
        ("DispatchMessageW", LRESULT, [ctypes.POINTER(wintypes.MSG)]),
        ("PostMessageW", B, [H, U, wintypes.WPARAM, wintypes.LPARAM]),
        ("PostQuitMessage", None, [I]),
        ("RegisterWindowMessageW", U, [wintypes.LPCWSTR]),
        ("CreatePopupMenu", wintypes.HMENU, []),
        ("AppendMenuW", B, [wintypes.HMENU, U, ctypes.c_size_t, wintypes.LPCWSTR]),
        ("SetMenuDefaultItem", B, [wintypes.HMENU, U, U]),
        ("TrackPopupMenu", I, [wintypes.HMENU, U, I, I, I, H, wintypes.LPVOID]),
        ("DestroyMenu", B, [wintypes.HMENU]),
        ("GetCursorPos", B, [ctypes.POINTER(wintypes.POINT)]),
        ("SetForegroundWindow", B, [H]),
        ("LoadImageW", wintypes.HANDLE, [wintypes.HINSTANCE, wintypes.LPCWSTR, U, I, I, U]),
        ("LoadIconW", wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPVOID]),
        ("DestroyIcon", B, [wintypes.HICON]),
        ("GetSystemMetrics", I, [I]),
        ("FindWindowW", H, [wintypes.LPCWSTR, wintypes.LPCWSTR]),
        ("IsIconic", B, [H]),
        ("IsWindowVisible", B, [H]),
        ("ShowWindow", B, [H, I]),
    ):
        function = getattr(user32, name)
        function.restype, function.argtypes = restype, argtypes
    shell32.Shell_NotifyIconW.restype = B
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    return user32, shell32, kernel32


# Window class of Edge (Chromium) windows, the app window included
_EDGE_WINDOW_CLASS = "Chrome_WidgetWin_1"


def focus_window(title: str = APP_NAME) -> bool:
    """Bring the app window (an Edge window titled `title`) to the front if one is open."""
    if sys.platform != "win32":
        return False
    user32, _, _ = _api()
    hwnd = user32.FindWindowW(_EDGE_WINDOW_CLASS, title)
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, _SW_RESTORE)
    user32.SetForegroundWindow(hwnd)
    return True


class Tray:
    def __init__(
        self,
        *,
        icon_path: str,
        on_open: Callable[[], None],
        on_mode: Callable[[NcAsmMode], None],
        on_quit: Callable[[], None],
        labels: dict[str, str] | None = None,
        log: Log | None = None,
    ) -> None:
        self._icon_path = icon_path
        self._on_open = on_open
        self._on_mode = on_mode
        self._on_quit = on_quit
        self._log = log
        self._lock = threading.Lock()
        self._state = TrayState()
        self._labels = dict(labels or DEFAULT_LABELS)
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        # Set on the tray thread; the window procedure already runs while the window is created
        self._taskbar_created = -1
        self._icon = None
        self._owns_icon = False

    # Called from other threads

    def start(self, timeout: float = 5.0) -> bool:
        """Show the icon. False if it could not be created (then the app should quit with its window)."""
        if sys.platform != "win32":
            return False
        self._thread = threading.Thread(target=self._run, name="sonyxm-tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        return self._hwnd is not None

    def stop(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            self._user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def update(self, event: Event) -> None:
        """Follow the worker's events (called for every event, from the worker threads)."""
        with self._lock:
            s = self._state
            if isinstance(event, Connected):
                s.name, s.wind = event.device.name, event.model.wind
            elif isinstance(event, (Connecting, Disconnected)):
                self._state = TrayState()
            elif isinstance(event, BatteryChanged):
                s.battery = event.battery.level
                s.charging = event.battery.charging == ChargingStatus.CHARGING
            elif isinstance(event, NoiseControlChanged):
                s.mode = event.state.mode
            else:
                return
        self._post(_WM_REFRESH)

    def set_labels(self, labels: dict[str, str]) -> None:
        with self._lock:
            self._labels = dict(labels)
        self._post(_WM_REFRESH)

    def snapshot(self) -> tuple[TrayState, dict[str, str]]:
        with self._lock:
            return TrayState(**vars(self._state)), dict(self._labels)

    def _post(self, message: int) -> None:
        hwnd = self._hwnd
        if hwnd:
            self._user32.PostMessageW(hwnd, message, 0, 0)

    # Tray thread

    def _run(self) -> None:
        try:
            self._user32, self._shell32, kernel32 = _api()
            instance = kernel32.GetModuleHandleW(None)
            self._wndproc = WNDPROC(self._window_proc)  # keep a reference while the window lives
            class_name = "XMControlTray"
            wc = _WNDCLASSEXW(cbSize=ctypes.sizeof(_WNDCLASSEXW), lpfnWndProc=self._wndproc,
                              hInstance=instance, lpszClassName=class_name)
            if not self._user32.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "RegisterClassExW failed")
            # A hidden top-level window: it only receives the icon's messages. Its title
            # differs from the app window's, so focus_window() never mistakes it for that.
            hwnd = self._user32.CreateWindowExW(0, class_name, f"{APP_NAME} tray", 0, 0, 0, 0, 0, None, None,
                                                instance, None)
            if not hwnd:
                raise OSError(ctypes.get_last_error(), "CreateWindowExW failed")
            self._taskbar_created = self._user32.RegisterWindowMessageW("TaskbarCreated")
            self._icon = self._load_icon()
            self._hwnd = hwnd
            if not self._notify(_NIM_ADD):
                raise OSError(ctypes.get_last_error(), "Shell_NotifyIconW failed")
        except OSError as e:
            self._debug(f"tray icon not available: {e}")
            self._hwnd = None
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))
        self._user32.UnregisterClassW(class_name, instance)

    def _load_icon(self) -> int:
        size_x = self._user32.GetSystemMetrics(_SM_CXSMICON)
        size_y = self._user32.GetSystemMetrics(_SM_CYSMICON)
        icon = self._user32.LoadImageW(None, self._icon_path, _IMAGE_ICON, size_x, size_y, _LR_LOADFROMFILE)
        self._owns_icon = bool(icon)
        if not icon:
            self._debug(f"could not load {self._icon_path}; using the default icon")
            icon = self._user32.LoadIconW(None, ctypes.c_void_p(_IDI_APPLICATION))
        return icon

    def _notify(self, action: int) -> bool:
        state, labels = self.snapshot()
        data = _NOTIFYICONDATAW(cbSize=ctypes.sizeof(_NOTIFYICONDATAW), hWnd=self._hwnd, uID=1,
                                uFlags=_NIF_MESSAGE | _NIF_ICON | _NIF_TIP, uCallbackMessage=_WM_TRAY,
                                hIcon=self._icon, szTip=tooltip(state, labels))
        return bool(self._shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        try:
            if message == _WM_TRAY:
                if lparam == _WM_LBUTTONUP:
                    self._on_open()
                elif lparam in (_WM_RBUTTONUP, _WM_CONTEXTMENU):
                    self._show_menu(hwnd)
                return 0
            if message == _WM_REFRESH:
                self._notify(_NIM_MODIFY)
                return 0
            if message == self._taskbar_created:  # Explorer restarted: the icon is gone
                self._notify(_NIM_ADD)
                return 0
            if message == _WM_CLOSE:
                self._notify(_NIM_DELETE)
                if self._owns_icon:
                    self._user32.DestroyIcon(self._icon)
                self._user32.DestroyWindow(hwnd)
                return 0
            if message == _WM_DESTROY:
                self._hwnd = None
                self._user32.PostQuitMessage(0)
                return 0
        except Exception as e:  # never let an error escape into Windows' message loop
            self._debug(f"tray error: {e!r}")
        return self._user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _show_menu(self, hwnd: int) -> None:
        state, labels = self.snapshot()
        handle = self._user32.CreatePopupMenu()
        try:
            for item in menu(state, labels):
                if item.id == 0:
                    self._user32.AppendMenuW(handle, _MF_SEPARATOR, 0, None)
                    continue
                flags = _MF_STRING | (0 if item.enabled else _MF_GRAYED) | (_MF_CHECKED if item.checked else 0)
                self._user32.AppendMenuW(handle, flags, item.id, item.text)
            self._user32.SetMenuDefaultItem(handle, _ID_OPEN, 0)
            point = wintypes.POINT()
            self._user32.GetCursorPos(ctypes.byref(point))
            # Without this the menu does not close when the user clicks elsewhere
            self._user32.SetForegroundWindow(hwnd)
            choice = self._user32.TrackPopupMenu(handle, _TPM_RIGHTBUTTON | _TPM_RETURNCMD | _TPM_NONOTIFY,
                                                 point.x, point.y, 0, hwnd, None)
            self._user32.PostMessageW(hwnd, _WM_NULL, 0, 0)
        finally:
            self._user32.DestroyMenu(handle)
        if choice == _ID_OPEN:
            self._on_open()
        elif choice == _ID_QUIT:
            self._on_quit()
        elif choice in _MODE_IDS:
            self._on_mode(_MODE_IDS[choice])

    def _debug(self, text: str) -> None:
        if self._log is not None:
            self._log(text)

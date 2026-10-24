"""Windows Bluetooth: list paired devices and open an RFCOMM socket by service UUID.

Uses only ctypes and the socket module. Python's socket.connect() on Windows
takes an RFCOMM channel number, not a service UUID, so we call Winsock's
connect() ourselves with a SOCKADDR_BTH that carries the UUID and let Windows
do the SDP lookup (PROTOCOL.md §1).
"""

from __future__ import annotations

import ctypes
import re
import select
import socket
import sys
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from .models import MODELS, SERVICE_V2, find_model

_AF_BTH = 32
_ERROR_NO_MORE_ITEMS = 259
_WSAEWOULDBLOCK = 10035
_BLUETOOTH_MAX_NAME_SIZE = 248

# Winsock errors worth explaining in plain words.
_WSA_MESSAGES = {
    10047: "this PC has no usable Bluetooth stack (WSAEAFNOSUPPORT)",
    10049: "the Bluetooth address is not valid (WSAEADDRNOTAVAIL)",
    10050: "Bluetooth is turned off or the adapter is unavailable (WSAENETDOWN)",
    10060: "the headphones did not answer: are they on and in range? (WSAETIMEDOUT)",
    10061: "the headphones refused the connection (WSAECONNREFUSED)",
    10064: "the headphones are off or out of range (WSAEHOSTDOWN)",
    10065: "the headphones are unreachable: are they on and in range? (WSAEHOSTUNREACH)",
    10108: "the headphones do not offer the Sony control service (WSASERVICE_NOT_FOUND)",
}


class BluetoothError(Exception):
    """A Bluetooth problem, with a message meant for the user."""


class DeviceNotFoundError(BluetoothError):
    pass


class SeveralDevicesError(DeviceNotFoundError):
    """Several supported headphones are paired and it is not clear which one to use."""


class ChannelInUseError(BluetoothError):
    """Another program on this PC already holds the headphones' control connection."""


class ServiceNotFoundError(BluetoothError):
    """The headphones do not offer the control service we asked for (they may offer the other one)."""


_WSAEADDRINUSE = 10048
_WSASERVICE_NOT_FOUND = 10108


def connect_error(code: int) -> BluetoothError:
    if code == _WSAEADDRINUSE:
        return ChannelInUseError(
            "another program on this PC is already connected to the headphones' controls "
            "(for example an open XM Control window); close it and try again (WSAEADDRINUSE)"
        )
    if code == _WSASERVICE_NOT_FOUND:
        return ServiceNotFoundError(f"connection failed: {_describe_wsa_error(code)}")
    return BluetoothError(f"connection failed: {_describe_wsa_error(code)}")


@dataclass(frozen=True)
class PairedDevice:
    name: str
    address: str  # "AA:BB:CC:DD:EE:FF"
    connected: bool
    authenticated: bool


def format_address(value: int) -> str:
    return ":".join(f"{(value >> shift) & 0xFF:02X}" for shift in range(40, -1, -8))


def parse_address(text: str) -> int:
    """Accept AA:BB:CC:DD:EE:FF, AA-BB-..., or 12 plain hex digits."""
    digits = re.sub(r"[:\-]", "", text.strip())
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", digits):
        raise BluetoothError(f"not a Bluetooth address: {text!r} (expected AA:BB:CC:DD:EE:FF)")
    return int(digits, 16)


def pick_headphones(devices: list[PairedDevice], address: str | None = None) -> PairedDevice:
    """Choose the headphones to talk to: by address if given, otherwise any supported model by name.

    With several supported headphones paired, the one connected to this PC wins.
    """
    if address is not None:
        wanted = parse_address(address)
        for device in devices:
            if parse_address(device.address) == wanted:
                return device
        raise DeviceNotFoundError(f"{format_address(wanted)} is not a paired Bluetooth device")

    matches = [d for d in devices if find_model(d.name) is not None]
    if not matches:
        supported = ", ".join(m.name for m in MODELS)
        names = ", ".join(f"{d.name} ({d.address})" for d in devices) or "none"
        raise DeviceNotFoundError(
            f"no paired {supported} found. Pair it in Windows Settings first. Paired devices: {names}"
        )
    if len(matches) > 1:
        connected = [d for d in matches if d.connected]
        if len(connected) == 1:
            return connected[0]
        listing = ", ".join(f"{d.name} ({d.address})" for d in matches)
        raise SeveralDevicesError(
            f"several supported headphones are paired ({listing}); turn on only the one you want, "
            f"or choose one with --address"
        )
    return matches[0]


# --- Paired device list (BluetoothAPIs) --------------------------------------


class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [(name, wintypes.WORD) for name in (
        "wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds",
    )]


class _BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("fReturnAuthenticated", wintypes.BOOL),
        ("fReturnRemembered", wintypes.BOOL),
        ("fReturnUnknown", wintypes.BOOL),
        ("fReturnConnected", wintypes.BOOL),
        ("fIssueInquiry", wintypes.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),
        ("hRadio", wintypes.HANDLE),
    ]


class _BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Address", ctypes.c_ulonglong),
        ("ulClassofDevice", wintypes.ULONG),
        ("fConnected", wintypes.BOOL),
        ("fRemembered", wintypes.BOOL),
        ("fAuthenticated", wintypes.BOOL),
        ("stLastSeen", _SYSTEMTIME),
        ("stLastUsed", _SYSTEMTIME),
        ("szName", wintypes.WCHAR * _BLUETOOTH_MAX_NAME_SIZE),
    ]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise BluetoothError("this tool only supports Windows")


def _load_bluetooth_apis() -> ctypes.WinDLL:
    for name in ("BluetoothApis.dll", "bthprops.cpl"):
        try:
            dll = ctypes.WinDLL(name, use_last_error=True)
        except OSError:
            continue
        dll.BluetoothFindFirstDevice.restype = wintypes.HANDLE
        dll.BluetoothFindFirstDevice.argtypes = [
            ctypes.POINTER(_BLUETOOTH_DEVICE_SEARCH_PARAMS), ctypes.POINTER(_BLUETOOTH_DEVICE_INFO),
        ]
        dll.BluetoothFindNextDevice.restype = wintypes.BOOL
        dll.BluetoothFindNextDevice.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BLUETOOTH_DEVICE_INFO)]
        dll.BluetoothFindDeviceClose.restype = wintypes.BOOL
        dll.BluetoothFindDeviceClose.argtypes = [wintypes.HANDLE]
        return dll
    raise BluetoothError("could not load the Windows Bluetooth API (BluetoothApis.dll)")


def list_paired_devices() -> list[PairedDevice]:
    """Paired (remembered) classic Bluetooth devices. Does not scan, so it is quick."""
    return [
        PairedDevice(
            name=info.szName,
            address=format_address(info.Address),
            connected=bool(info.fConnected),
            authenticated=bool(info.fAuthenticated),
        )
        for info in _paired_device_infos()
    ]


def _device_info(address: int) -> _BLUETOOTH_DEVICE_INFO:
    """The raw Windows record of a paired device, as some Bluetooth APIs need it."""
    for info in _paired_device_infos():
        if info.Address == address:
            return info
    raise DeviceNotFoundError(f"{format_address(address)} is not a paired Bluetooth device")


def _paired_device_infos() -> list[_BLUETOOTH_DEVICE_INFO]:
    _require_windows()
    api = _load_bluetooth_apis()

    params = _BLUETOOTH_DEVICE_SEARCH_PARAMS()
    params.dwSize = ctypes.sizeof(params)
    params.fReturnAuthenticated = True
    params.fReturnRemembered = True
    params.fReturnConnected = True
    params.fReturnUnknown = False
    params.fIssueInquiry = False
    params.hRadio = None

    info = _BLUETOOTH_DEVICE_INFO()
    info.dwSize = ctypes.sizeof(info)
    handle = api.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_NO_MORE_ITEMS:
            return []
        raise BluetoothError(f"could not list Bluetooth devices: {ctypes.FormatError(error)} ({error})")

    devices = []
    try:
        while True:
            devices.append(info)
            info = _BLUETOOTH_DEVICE_INFO()
            info.dwSize = ctypes.sizeof(info)
            if not api.BluetoothFindNextDevice(handle, ctypes.byref(info)):
                break
    finally:
        api.BluetoothFindDeviceClose(handle)
    return devices


# --- RFCOMM connection (Winsock) ---------------------------------------------


class _SOCKADDR_BTH(ctypes.Structure):
    _pack_ = 1  # ws2bth.h wraps this struct in pshpack1.h
    _fields_ = [
        ("addressFamily", ctypes.c_ushort),
        ("btAddr", ctypes.c_ulonglong),
        ("serviceClassId", ctypes.c_ubyte * 16),  # GUID, little-endian layout
        ("port", ctypes.c_ulong),
    ]


def _sockaddr_bth(address: int, service: uuid.UUID) -> _SOCKADDR_BTH:
    sa = _SOCKADDR_BTH()
    sa.addressFamily = _AF_BTH
    sa.btAddr = address
    sa.serviceClassId[:] = service.bytes_le
    sa.port = 0  # 0 = look up the channel for serviceClassId via SDP
    return sa


def _describe_wsa_error(code: int) -> str:
    text = _WSA_MESSAGES.get(code)
    if text is None:
        text = f"{ctypes.FormatError(code).strip()} (Winsock error {code})"
    return text


def open_rfcomm(
    address: str,
    timeout: float = 15.0,
    log: Callable[[str], None] | None = None,
    service: uuid.UUID = SERVICE_V2,
) -> socket.socket:
    """Connect to a Sony control service (models.SERVICE_V1 or SERVICE_V2) on the given device.

    Returns a socket with no timeout set; the caller sets its own read timeouts.
    """
    _require_windows()
    bt_addr = parse_address(address)
    if log:
        log(f"connecting to {format_address(bt_addr)}, service {service}, timeout {timeout:g}s")

    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    except OSError as e:
        raise BluetoothError(f"cannot create a Bluetooth socket: {_describe_wsa_error(e.winerror or 0)}") from e

    try:
        ws2_32 = ctypes.WinDLL("ws2_32", use_last_error=True)
        ws2_32.connect.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        ws2_32.connect.restype = ctypes.c_int

        # Non-blocking connect so the timeout is ours, not Windows' default.
        sock.setblocking(False)
        sa = _sockaddr_bth(bt_addr, service)
        if ws2_32.connect(sock.fileno(), ctypes.byref(sa), ctypes.sizeof(sa)) != 0:
            error = ctypes.get_last_error()
            if error != _WSAEWOULDBLOCK:
                raise connect_error(error)
            _, writable, failed = select.select([], [sock], [sock], timeout)
            if not writable and not failed:
                raise BluetoothError(
                    f"connection timed out after {timeout:g}s: are the headphones on and in range?"
                )
            error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise connect_error(error)
    except BaseException:
        sock.close()
        raise

    sock.setblocking(True)
    if log:
        log("connected")
    return sock

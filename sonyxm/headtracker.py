"""Head tracking through the headphones' Android Head Tracker HID sensor (Windows, ctypes only).

The XM5 exposes head orientation as a HID sensor (usage page 0x20, usage
0xE1) following Android's "Head tracker HID protocol". Reading it does not
use the Sony control channel at all. See PROTOCOL.md §10 for sources.

Windows often pairs the headphones without creating the sensor's device node;
repair() fixes that, but needs administrator rights.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

# --- Protocol constants (Android head tracker HID protocol) -------------------

SENSOR_PAGE = 0x20
OTHER_CUSTOM = 0xE1
SENSOR_DESCRIPTION = 0x0308
REPORT_INTERVAL = 0x030E
REPORTING_NO_EVENTS = 0x0840
REPORTING_ALL_EVENTS = 0x0841
POWER_FULL = 0x0851
POWER_OFF = 0x0855
ROTATION_VECTOR = 0x0544  # Custom Value 1: orientation, radians
ANGULAR_VELOCITY = 0x0545  # Custom Value 2: rad/s
RESET_COUNTER = 0x0546  # Custom Value 3: changes when the reference frame resets
MARKER = b"#AndroidHeadTracker#"
SONY_VENDOR_ID = 0x054C
# Fastest interval the protocol asks for; the XM5 is reported to offer 40 ms only.
TARGET_INTERVAL = 0.010

Log = Callable[[str], None]


class HeadTrackerError(Exception):
    """Head tracking is unavailable; the message says why and what to do."""


class SensorMissingError(HeadTrackerError):
    """Windows has not set up the sensor; repair() fixes that."""


@dataclass(frozen=True)
class Sample:
    rotation: tuple[float, float, float]  # rotation vector, radians
    angular_velocity: tuple[float, float, float] | None  # rad/s
    reset_counter: int | None


@dataclass(frozen=True)
class Angles:
    yaw: float  # degrees, positive = turned left
    pitch: float  # degrees, positive = looking up
    roll: float  # degrees, positive = tilted right


# --- Orientation math (pure) --------------------------------------------------

Quaternion = tuple[float, float, float, float]  # (w, x, y, z)
IDENTITY: Quaternion = (1.0, 0.0, 0.0, 0.0)


def quaternion_from_rotation_vector(rx: float, ry: float, rz: float) -> Quaternion:
    angle = math.sqrt(rx * rx + ry * ry + rz * rz)
    if angle < 1e-12:
        return IDENTITY
    s = math.sin(angle / 2) / angle
    return (math.cos(angle / 2), rx * s, ry * s, rz * s)


def quaternion_multiply(a: Quaternion, b: Quaternion) -> Quaternion:
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def quaternion_conjugate(q: Quaternion) -> Quaternion:
    return (q[0], -q[1], -q[2], -q[3])


def angles_from_quaternion(q: Quaternion) -> Angles:
    """Yaw about Z (up), then pitch about X (right ear), then roll about Y (nose)."""
    w, x, y, z = q
    r01 = 2 * (x * y - w * z)
    r11 = 1 - 2 * (x * x + z * z)
    r20 = 2 * (x * z - w * y)
    r21 = 2 * (y * z + w * x)
    r22 = 1 - 2 * (x * x + y * y)
    pitch = math.asin(max(-1.0, min(1.0, r21)))
    yaw = math.atan2(-r01, r11)
    roll = math.atan2(-r20, r22)
    return Angles(math.degrees(yaw), math.degrees(pitch), math.degrees(roll))


# The headphones' orientation filter drifts for about a second after it starts
# (PROTOCOL.md §10), so "straight ahead" is taken again after this many samples
# (about 1.5 s at the XM5's 25 samples/s).
SETTLE_SAMPLES = 38


class Orientation:
    """Turns samples into angles relative to a "straight ahead" pose set by recenter()."""

    def __init__(self, settle_samples: int = SETTLE_SAMPLES) -> None:
        self.reference: Quaternion | None = None
        self.current: Quaternion = IDENTITY
        self.settle_samples = settle_samples
        self._reset_counter: int | None = None
        self._since_reset = 0

    @property
    def settled(self) -> bool:
        return self._since_reset >= self.settle_samples

    def update(self, sample: Sample) -> Angles:
        self.current = quaternion_from_rotation_vector(*sample.rotation)
        if sample.reset_counter != self._reset_counter:
            # First sample, or the headphones restarted their orientation filter
            self._reset_counter = sample.reset_counter
            self.reference = self.current
            self._since_reset = 0
        self._since_reset += 1
        if self._since_reset == self.settle_samples:
            self.reference = self.current
        return self.angles()

    def recenter(self) -> None:
        self.reference = self.current

    def relative(self) -> Quaternion:
        reference = self.reference or IDENTITY
        return quaternion_multiply(quaternion_conjugate(reference), self.current)

    def angles(self) -> Angles:
        return angles_from_quaternion(self.relative())


# --- HID value decoding (pure) ------------------------------------------------


def unit_exponent(code: int) -> int:
    """HID unit exponents are a 4-bit two's complement nibble (0xD = -3)."""
    code &= 0xF
    return code - 16 if code >= 8 else code


def unpack_values(data: bytes, bit_size: int, count: int, signed: bool) -> list[int]:
    """Little-endian, LSB-first packed fields, as HidP_GetUsageValueArray returns them."""
    packed = int.from_bytes(data, "little")
    mask = (1 << bit_size) - 1
    values = []
    for i in range(count):
        v = (packed >> (i * bit_size)) & mask
        if signed and v & (1 << (bit_size - 1)):
            v -= 1 << bit_size
        values.append(v)
    return values


def to_physical(logical: int, lmin: int, lmax: int, pmin: int, pmax: int, exponent: int) -> float:
    """HID spec 6.2.2.7. With no physical range given, physical equals logical."""
    if pmin == 0 and pmax == 0:
        pmin, pmax = lmin, lmax
    if lmax == lmin:
        return float(logical) * 10.0 ** exponent
    return ((logical - lmin) * (pmax - pmin) / (lmax - lmin) + pmin) * 10.0 ** exponent


# --- Windows API --------------------------------------------------------------

_HIDP_STATUS_SUCCESS = 0x00110000
_HidP_Input, _HidP_Feature = 0, 2
_DIGCF_PRESENT, _DIGCF_ALLCLASSES, _DIGCF_DEVICEINTERFACE = 0x02, 0x04, 0x10
_GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
_FILE_SHARE_READ, _FILE_SHARE_WRITE = 0x1, 0x2
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_ERROR_IO_PENDING = 997
_ERROR_OPERATION_ABORTED = 995
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0, 0x102
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_SPDRP_HARDWAREID = 0x1
_CM_PROB_FAILED_START = 10
_INSTALLFLAG_FORCE, _INSTALLFLAG_NONINTERACTIVE = 0x1, 0x4
_BLUETOOTH_SERVICE_DISABLE, _BLUETOOTH_SERVICE_ENABLE = 0, 1
_ERROR_INVALID_PARAMETER = 87
_E_INVALIDARG = 0x80070057
_HEAD_TRACKER_HARDWARE_ID = "UP:0020_U:00E1"  # hardware ID part of the sensor collection


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_uuid(cls, text: str) -> _GUID:
        import uuid

        g = cls()
        ctypes.memmove(ctypes.byref(g), uuid.UUID(text).bytes_le, 16)
        return g


class _SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("InterfaceClassGuid", _GUID), ("Flags", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


class _SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", _GUID), ("DevInst", wintypes.DWORD),
                ("Reserved", ctypes.c_void_p)]


class _HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Size", wintypes.ULONG), ("VendorID", wintypes.USHORT), ("ProductID", wintypes.USHORT),
                ("VersionNumber", wintypes.USHORT)]


class _HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                ("InputReportByteLength", wintypes.USHORT), ("OutputReportByteLength", wintypes.USHORT),
                ("FeatureReportByteLength", wintypes.USHORT), ("Reserved", wintypes.USHORT * 17),
                ("NumberLinkCollectionNodes", wintypes.USHORT), ("NumberInputButtonCaps", wintypes.USHORT),
                ("NumberInputValueCaps", wintypes.USHORT), ("NumberInputDataIndices", wintypes.USHORT),
                ("NumberOutputButtonCaps", wintypes.USHORT), ("NumberOutputValueCaps", wintypes.USHORT),
                ("NumberOutputDataIndices", wintypes.USHORT), ("NumberFeatureButtonCaps", wintypes.USHORT),
                ("NumberFeatureValueCaps", wintypes.USHORT), ("NumberFeatureDataIndices", wintypes.USHORT)]


class _UsageRange(ctypes.Structure):
    # Range and NotRange share this layout; for NotRange, UsageMin is the usage.
    _fields_ = [("UsageMin", wintypes.USHORT), ("UsageMax", wintypes.USHORT),
                ("StringMin", wintypes.USHORT), ("StringMax", wintypes.USHORT),
                ("DesignatorMin", wintypes.USHORT), ("DesignatorMax", wintypes.USHORT),
                ("DataIndexMin", wintypes.USHORT), ("DataIndexMax", wintypes.USHORT)]


class _HIDP_VALUE_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", wintypes.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wintypes.USHORT), ("LinkCollection", wintypes.USHORT),
                ("LinkUsage", wintypes.USHORT), ("LinkUsagePage", wintypes.USHORT),
                ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("HasNull", ctypes.c_ubyte), ("Reserved", ctypes.c_ubyte), ("BitSize", wintypes.USHORT),
                ("ReportCount", wintypes.USHORT), ("Reserved2", wintypes.USHORT * 5),
                ("UnitsExp", wintypes.ULONG), ("Units", wintypes.ULONG),
                ("LogicalMin", wintypes.LONG), ("LogicalMax", wintypes.LONG),
                ("PhysicalMin", wintypes.LONG), ("PhysicalMax", wintypes.LONG),
                ("Range", _UsageRange)]


class _HIDP_BUTTON_CAPS(ctypes.Structure):
    _fields_ = [("UsagePage", wintypes.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wintypes.USHORT), ("LinkCollection", wintypes.USHORT),
                ("LinkUsage", wintypes.USHORT), ("LinkUsagePage", wintypes.USHORT),
                ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte),
                ("Reserved", wintypes.ULONG * 10), ("Range", _UsageRange)]


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", ctypes.c_void_p)]


def _usage(caps) -> tuple[int, int]:
    """(first, last) usage of a value/button capability."""
    if caps.IsRange:
        return caps.Range.UsageMin, caps.Range.UsageMax
    return caps.Range.UsageMin, caps.Range.UsageMin


class _Api:
    """Lazily bound Windows functions with explicit 64-bit-safe signatures."""

    _instance: _Api | None = None

    @classmethod
    def get(cls) -> _Api:
        if sys.platform != "win32":
            raise HeadTrackerError("head tracking is only supported on Windows")
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        P, H, B, U, L, US = ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL, wintypes.ULONG, wintypes.LONG, wintypes.USHORT
        self.kernel32 = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.setupapi = s = ctypes.WinDLL("setupapi", use_last_error=True)
        self.hid = h = ctypes.WinDLL("hid", use_last_error=True)
        self.cfgmgr32 = c = ctypes.WinDLL("cfgmgr32", use_last_error=True)

        def bind(dll, name, restype, *argtypes):
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = restype, list(argtypes)

        bind(k, "CreateFileW", H, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, P, wintypes.DWORD,
             wintypes.DWORD, H)
        bind(k, "CloseHandle", B, H)
        bind(k, "CreateEventW", H, P, B, B, wintypes.LPCWSTR)
        bind(k, "ReadFile", B, H, P, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), P)
        bind(k, "WaitForSingleObject", wintypes.DWORD, H, wintypes.DWORD)
        bind(k, "GetOverlappedResult", B, H, P, ctypes.POINTER(wintypes.DWORD), B)
        bind(k, "CancelIoEx", B, H, P)
        bind(k, "ResetEvent", B, H)

        bind(s, "SetupDiGetClassDevsW", H, P, wintypes.LPCWSTR, P, wintypes.DWORD)
        bind(s, "SetupDiEnumDeviceInterfaces", B, H, P, P, wintypes.DWORD, P)
        bind(s, "SetupDiGetDeviceInterfaceDetailW", B, H, P, P, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), P)
        bind(s, "SetupDiEnumDeviceInfo", B, H, wintypes.DWORD, P)
        bind(s, "SetupDiGetDeviceRegistryPropertyW", B, H, P, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
             P, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        bind(s, "SetupDiGetDeviceInstanceIdW", B, H, P, wintypes.LPWSTR, wintypes.DWORD,
             ctypes.POINTER(wintypes.DWORD))
        bind(s, "SetupDiDestroyDeviceInfoList", B, H)

        bind(h, "HidD_GetHidGuid", None, P)
        bind(h, "HidD_GetAttributes", ctypes.c_ubyte, H, P)
        bind(h, "HidD_GetPreparsedData", ctypes.c_ubyte, H, ctypes.POINTER(P))
        bind(h, "HidD_FreePreparsedData", ctypes.c_ubyte, P)
        bind(h, "HidD_GetFeature", ctypes.c_ubyte, H, P, U)
        bind(h, "HidD_SetFeature", ctypes.c_ubyte, H, P, U)
        bind(h, "HidD_GetProductString", ctypes.c_ubyte, H, P, U)
        bind(h, "HidP_GetCaps", L, P, P)
        bind(h, "HidP_GetValueCaps", L, ctypes.c_int, P, ctypes.POINTER(US), P)
        bind(h, "HidP_GetButtonCaps", L, ctypes.c_int, P, ctypes.POINTER(US), P)
        bind(h, "HidP_GetUsageValueArray", L, ctypes.c_int, US, US, US, P, US, P, P, U)
        bind(h, "HidP_GetUsageValue", L, ctypes.c_int, US, US, US, ctypes.POINTER(U), P, P, U)
        bind(h, "HidP_SetUsages", L, ctypes.c_int, US, US, ctypes.POINTER(US), ctypes.POINTER(U), P, P, U)
        bind(h, "HidP_SetScaledUsageValue", L, ctypes.c_int, US, US, US, L, P, P, U)

        bind(c, "CM_Get_DevNode_Status", wintypes.DWORD, ctypes.POINTER(U), ctypes.POINTER(U), wintypes.DWORD,
             U)
        bind(c, "CM_Get_Parent", wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, U)
        bind(c, "CM_Get_Device_IDW", wintypes.DWORD, wintypes.DWORD, wintypes.LPWSTR, U, U)

    @staticmethod
    def error() -> str:
        code = ctypes.get_last_error()
        return f"{ctypes.FormatError(code).strip()} (Win32 error {code})"


@dataclass(frozen=True)
class TrackerInfo:
    path: str
    product: str


def _hid_interface_paths(api: _Api) -> list[str]:
    guid = _GUID()
    api.hid.HidD_GetHidGuid(ctypes.byref(guid))
    devs = api.setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, _DIGCF_PRESENT | _DIGCF_DEVICEINTERFACE)
    if devs in (None, _INVALID_HANDLE):
        raise HeadTrackerError(f"could not list HID devices: {api.error()}")
    paths = []
    try:
        index = 0
        while True:
            iface = _SP_DEVICE_INTERFACE_DATA()
            iface.cbSize = ctypes.sizeof(iface)
            if not api.setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), index, ctypes.byref(iface)):
                break
            index += 1
            needed = wintypes.DWORD()
            api.setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), None, 0, ctypes.byref(needed), None)
            buffer = ctypes.create_string_buffer(needed.value)
            # SP_DEVICE_INTERFACE_DETAIL_DATA_W.cbSize is the fixed part: 8 on 64-bit, 6 on 32-bit
            ctypes.c_ulong.from_buffer(buffer).value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if api.setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(iface), buffer, needed, None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buffer) + 4))
    finally:
        api.setupapi.SetupDiDestroyDeviceInfoList(devs)
    return paths


def _open(api: _Api, path: str, overlapped: bool = False) -> int | None:
    flags = _FILE_FLAG_OVERLAPPED if overlapped else 0
    share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
    for access in (_GENERIC_READ | _GENERIC_WRITE, _GENERIC_READ, 0):
        handle = api.kernel32.CreateFileW(path, access, share, None, _OPEN_EXISTING, flags, None)
        if handle not in (None, _INVALID_HANDLE):
            return handle
    return None


def _caps(api: _Api, handle: int) -> tuple[ctypes.c_void_p, _HIDP_CAPS] | None:
    ppd = ctypes.c_void_p()
    if not api.hid.HidD_GetPreparsedData(handle, ctypes.byref(ppd)):
        return None
    caps = _HIDP_CAPS()
    if api.hid.HidP_GetCaps(ppd, ctypes.byref(caps)) != _HIDP_STATUS_SUCCESS:
        api.hid.HidD_FreePreparsedData(ppd)
        return None
    return ppd, caps


def find_head_trackers() -> list[TrackerInfo]:
    """Sony HID collections that are head tracker sensors. Read-only."""
    api = _Api.get()
    found = []
    for path in _hid_interface_paths(api):
        if "vid&0002054c" not in path.lower() and "vid_054c" not in path.lower():
            continue
        handle = _open(api, path)
        if handle is None:
            continue
        try:
            result = _caps(api, handle)
            if result is None:
                continue
            ppd, caps = result
            api.hid.HidD_FreePreparsedData(ppd)
            if (caps.UsagePage, caps.Usage) != (SENSOR_PAGE, OTHER_CUSTOM):
                continue
            name = ctypes.create_unicode_buffer(256)
            product = name.value if api.hid.HidD_GetProductString(handle, name, ctypes.sizeof(name)) else ""
            found.append(TrackerInfo(path, product))
        finally:
            api.kernel32.CloseHandle(handle)
    return found


class HeadTracker:
    """An open head tracker sensor. Use start(), then read() in a loop, then close()."""

    def __init__(self, path: str, log: Log | None = None) -> None:
        self._api = api = _Api.get()
        self._log = log
        self.path = path
        handle = _open(api, path, overlapped=True)
        if handle is None:
            raise HeadTrackerError(f"could not open the head tracker: {api.error()}")
        self._handle = handle
        result = _caps(api, handle)
        if result is None:
            self.close()
            raise HeadTrackerError("could not read the head tracker's HID description")
        self._ppd, self.caps = result
        self._input_caps = self._value_caps(_HidP_Input, self.caps.NumberInputValueCaps)
        self._feature_caps = self._value_caps(_HidP_Feature, self.caps.NumberFeatureValueCaps)
        self._feature_buttons = self._button_caps(_HidP_Feature, self.caps.NumberFeatureButtonCaps)
        self._event = api.kernel32.CreateEventW(None, True, False, None)
        self._buffer = ctypes.create_string_buffer(self.caps.InputReportByteLength)
        self._overlapped = _OVERLAPPED()
        self._pending = False
        self.description = self._read_description()
        self._debug(f"head tracker: {self.description or '(no description)'}; "
                    f"input report {self.caps.InputReportByteLength} bytes")
        if not self.description.encode("ascii", "replace").startswith(MARKER):
            self.close()
            raise HeadTrackerError(f"not an Android head tracker (description {self.description!r})")

    # Setup

    def _value_caps(self, report_type: int, count: int) -> list[_HIDP_VALUE_CAPS]:
        if not count:
            return []
        array = (_HIDP_VALUE_CAPS * count)()
        n = wintypes.USHORT(count)
        if self._api.hid.HidP_GetValueCaps(report_type, array, ctypes.byref(n), self._ppd) != _HIDP_STATUS_SUCCESS:
            return []
        return list(array[: n.value])

    def _button_caps(self, report_type: int, count: int) -> list[_HIDP_BUTTON_CAPS]:
        if not count:
            return []
        array = (_HIDP_BUTTON_CAPS * count)()
        n = wintypes.USHORT(count)
        if self._api.hid.HidP_GetButtonCaps(report_type, array, ctypes.byref(n), self._ppd) != _HIDP_STATUS_SUCCESS:
            return []
        return list(array[: n.value])

    def _get_feature(self, report_id: int) -> ctypes.Array | None:
        report = ctypes.create_string_buffer(self.caps.FeatureReportByteLength)
        report[0] = report_id
        if not self._api.hid.HidD_GetFeature(self._handle, report, len(report)):
            self._debug(f"reading feature report {report_id} failed: {self._api.error()}")
            return None
        self._debug(f"feature report {report_id}: {report.raw.hex(' ')}")
        return report

    def _read_description(self) -> str:
        for report_id in sorted({c.ReportID for c in self._feature_caps} | {b.ReportID for b in self._feature_buttons}):
            report = self._get_feature(report_id)
            if report is None:
                continue
            start = report.raw.find(MARKER)
            if start >= 0:
                text = report.raw[start:].split(b"\0")[0].rstrip(b"\xff")
                return text.decode("ascii", "replace")
        return ""

    def start(self) -> None:
        """Ask for All Events at Full Power with the fastest allowed interval."""
        self._configure(REPORTING_ALL_EVENTS, POWER_FULL, set_interval=True)

    def _configure(self, reporting: int, power: int, set_interval: bool) -> None:
        api, hid = self._api, self._api.hid
        reports: dict[int, ctypes.Array] = {}

        def report_for(report_id: int) -> ctypes.Array:
            if report_id not in reports:
                current = self._get_feature(report_id)
                if current is None:
                    current = ctypes.create_string_buffer(self.caps.FeatureReportByteLength)
                    current[0] = report_id
                reports[report_id] = current
            return reports[report_id]

        if set_interval:
            for c in self._feature_caps:
                if c.UsagePage != SENSOR_PAGE or _usage(c)[0] != REPORT_INTERVAL:
                    continue
                exponent = unit_exponent(c.UnitsExp)
                low, high = sorted((c.PhysicalMin, c.PhysicalMax))
                target = round(TARGET_INTERVAL / 10.0 ** exponent)
                target = max(low, min(high, target))
                report = report_for(c.ReportID)
                status = hid.HidP_SetScaledUsageValue(_HidP_Feature, c.UsagePage, c.LinkCollection, REPORT_INTERVAL,
                                                      target, self._ppd, report, len(report))
                self._debug(f"report interval -> {target * 10.0 ** exponent * 1000:g} ms "
                            f"(device allows {low * 10.0 ** exponent * 1000:g}-{high * 10.0 ** exponent * 1000:g} ms)"
                            f" status 0x{status & 0xFFFFFFFF:08x}")

        for wanted, label in ((power, "power state"), (reporting, "reporting state")):
            for b in self._feature_buttons:
                first, last = _usage(b)
                if b.UsagePage != SENSOR_PAGE or not first <= wanted <= last:
                    continue
                report = report_for(b.ReportID)
                usage = wintypes.USHORT(wanted)
                count = wintypes.ULONG(1)
                status = hid.HidP_SetUsages(_HidP_Feature, b.UsagePage, b.LinkCollection, ctypes.byref(usage),
                                            ctypes.byref(count), self._ppd, report, len(report))
                if status != _HIDP_STATUS_SUCCESS:
                    raise HeadTrackerError(f"could not encode the {label} (status 0x{status & 0xFFFFFFFF:08x})")
                break
            else:
                raise HeadTrackerError(f"the head tracker does not offer a writable {label}")

        for report_id, report in reports.items():
            self._debug(f"set feature report {report_id}: {report.raw.hex(' ')}")
            if not hid.HidD_SetFeature(self._handle, report, len(report)):
                raise HeadTrackerError(f"the head tracker refused its settings: {api.error()}")

    # Reading

    def read(self, timeout: float = 1.0) -> Sample | None:
        """Wait up to `timeout` seconds for one sample. None on timeout."""
        k = self._api.kernel32
        if not self._pending:
            k.ResetEvent(self._event)
            self._overlapped = _OVERLAPPED()
            self._overlapped.hEvent = self._event
            if not k.ReadFile(self._handle, self._buffer, len(self._buffer), None, ctypes.byref(self._overlapped)):
                if ctypes.get_last_error() != _ERROR_IO_PENDING:
                    raise HeadTrackerError(f"reading the head tracker failed: {self._api.error()}")
            self._pending = True
        if k.WaitForSingleObject(self._event, max(0, int(timeout * 1000))) == _WAIT_TIMEOUT:
            return None
        self._pending = False
        done = wintypes.DWORD()
        if not k.GetOverlappedResult(self._handle, ctypes.byref(self._overlapped), ctypes.byref(done), False):
            raise HeadTrackerError(f"reading the head tracker failed: {self._api.error()}")
        report = self._buffer.raw[: done.value]
        return self._parse(report)

    def _parse(self, report: bytes) -> Sample | None:
        rotation = velocity = None
        counter = None
        buffer = ctypes.create_string_buffer(report, len(self._buffer))
        for c in self._input_caps:
            usage = _usage(c)[0]
            if c.UsagePage != SENSOR_PAGE or usage not in (ROTATION_VECTOR, ANGULAR_VELOCITY, RESET_COUNTER):
                continue
            if usage == RESET_COUNTER:
                value = wintypes.ULONG()
                if self._api.hid.HidP_GetUsageValue(_HidP_Input, c.UsagePage, c.LinkCollection, usage,
                                                    ctypes.byref(value), self._ppd, buffer,
                                                    len(buffer)) == _HIDP_STATUS_SUCCESS:
                    counter = value.value & 0xFF
                continue
            size = (c.BitSize * c.ReportCount + 7) // 8
            packed = ctypes.create_string_buffer(size)
            status = self._api.hid.HidP_GetUsageValueArray(_HidP_Input, c.UsagePage, c.LinkCollection, usage, packed,
                                                           size, self._ppd, buffer, len(buffer))
            if status != _HIDP_STATUS_SUCCESS:
                continue
            raw = unpack_values(packed.raw, c.BitSize, c.ReportCount, c.LogicalMin < 0)
            exponent = unit_exponent(c.UnitsExp)
            values = tuple(to_physical(v, c.LogicalMin, c.LogicalMax, c.PhysicalMin, c.PhysicalMax, exponent)
                           for v in raw)
            if len(values) >= 3:
                if usage == ROTATION_VECTOR:
                    rotation = values[:3]
                else:
                    velocity = values[:3]
        if rotation is None:
            self._debug(f"report without orientation: {report.hex(' ')}")
            return None
        return Sample(rotation, velocity, counter)

    def close(self) -> None:
        api = self._api
        if getattr(self, "_handle", None) is None:
            return
        try:
            if getattr(self, "_ppd", None):
                # Let the headphones stop sensing when nobody is listening
                try:
                    self._configure(REPORTING_NO_EVENTS, POWER_OFF, set_interval=False)
                except HeadTrackerError:
                    pass
            if getattr(self, "_pending", False):
                api.kernel32.CancelIoEx(self._handle, ctypes.byref(self._overlapped))
                done = wintypes.DWORD()
                api.kernel32.GetOverlappedResult(self._handle, ctypes.byref(self._overlapped), ctypes.byref(done), True)
        finally:
            if getattr(self, "_ppd", None):
                api.hid.HidD_FreePreparsedData(self._ppd)
                self._ppd = None
            if getattr(self, "_event", None):
                api.kernel32.CloseHandle(self._event)
                self._event = None
            api.kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> HeadTracker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _debug(self, text: str) -> None:
        if self._log is not None:
            self._log(text)


def open_head_tracker(log: Log | None = None) -> HeadTracker:
    trackers = find_head_trackers()
    if not trackers:
        raise SensorMissingError(
            "Windows has not set up the headphones' head tracking sensor. "
            "Run the repair once (it asks for administrator rights), then try again."
        )
    tracker = HeadTracker(trackers[0].path, log)
    tracker.start()
    return tracker


# --- Repair (needs administrator rights) --------------------------------------


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", ctypes.c_void_p),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", ctypes.c_void_p),
                ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]


def run_elevated(arguments: list[str], wait: bool = True) -> int:
    """Run this program again as administrator (Windows asks the user first).

    Returns the exit code when waiting, else 0. Raises HeadTrackerError if the
    user declines or Windows refuses.
    """
    import subprocess

    if getattr(sys, "frozen", False):  # packaged .exe: the exe is the program
        executable, parameters = sys.executable, list(arguments)
    else:
        executable, parameters = sys.executable, ["-m", "sonyxm", *arguments]
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = subprocess.list2cmdline(parameters)
    info.lpDirectory = os.getcwd()
    info.nShow = 1  # SW_SHOWNORMAL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        if ctypes.get_last_error() == 1223:  # ERROR_CANCELLED
            raise HeadTrackerError("administrator rights were not granted")
        raise HeadTrackerError(f"could not start the repair: {_Api.error()}")
    if not wait or not info.hProcess:
        if info.hProcess:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(info.hProcess))
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    kernel32.CloseHandle(info.hProcess)
    return code.value


def _device_ids(api: _Api) -> list[tuple[int, str, list[str], int]]:
    """(devinst, instance ID, hardware IDs, problem code) of every present device."""
    devs = api.setupapi.SetupDiGetClassDevsW(None, None, None, _DIGCF_ALLCLASSES | _DIGCF_PRESENT)
    if devs in (None, _INVALID_HANDLE):
        raise HeadTrackerError(f"could not list devices: {api.error()}")
    result = []
    try:
        index = 0
        while True:
            info = _SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not api.setupapi.SetupDiEnumDeviceInfo(devs, index, ctypes.byref(info)):
                break
            index += 1
            instance = ctypes.create_unicode_buffer(512)
            if not api.setupapi.SetupDiGetDeviceInstanceIdW(devs, ctypes.byref(info), instance, 512, None):
                continue
            needed = wintypes.DWORD()
            api.setupapi.SetupDiGetDeviceRegistryPropertyW(devs, ctypes.byref(info), _SPDRP_HARDWAREID, None, None,
                                                           0, ctypes.byref(needed))
            hardware_ids: list[str] = []
            if needed.value:
                data = ctypes.create_string_buffer(needed.value)
                if api.setupapi.SetupDiGetDeviceRegistryPropertyW(devs, ctypes.byref(info), _SPDRP_HARDWAREID, None,
                                                                  data, needed, None):
                    hardware_ids = [s for s in data.raw.decode("utf-16-le", "replace").split("\0") if s]
            status, problem = wintypes.ULONG(), wintypes.ULONG()
            api.cfgmgr32.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem), info.DevInst, 0)
            result.append((info.DevInst, instance.value, hardware_ids, problem.value))
    finally:
        api.setupapi.SetupDiDestroyDeviceInfoList(devs)
    return result


def _parent_ids(api: _Api, devinst: int, depth: int = 6) -> list[str]:
    ids = []
    node = wintypes.DWORD(devinst)
    for _ in range(depth):
        parent = wintypes.DWORD()
        if api.cfgmgr32.CM_Get_Parent(ctypes.byref(parent), node, 0) != 0:
            break
        node = parent
        text = ctypes.create_unicode_buffer(512)
        if api.cfgmgr32.CM_Get_Device_IDW(node, text, 512, 0) == 0:
            ids.append(text.value)
    return ids


def _failed_tracker_nodes(api: _Api, address_hex: str) -> list[str]:
    """Hardware IDs of this headset's head tracker nodes that Windows failed to start (Code 10)."""
    failed = []
    for devinst, _instance, hardware_ids, problem in _device_ids(api):
        if problem != _CM_PROB_FAILED_START:
            continue
        if not any(_HEAD_TRACKER_HARDWARE_ID in h.upper() for h in hardware_ids):
            continue
        if any(address_hex in p.upper() for p in _parent_ids(api, devinst)):
            failed.append(hardware_ids[0])
    return failed


def repair(address: str, log: Log) -> None:
    """Make Windows (re)create the head tracker sensor for the headset at `address`.

    1. Re-enable the headset's standard Bluetooth HID service (cycling it when
       Windows' record of it is stale), so the sensor node is created.
    2. If Windows creates the node but fails to start it (Code 10), bind it to
       Windows' own generic HID driver (input.inf) so it can be read.
    Nothing is sent to the headphones themselves.
    """
    from . import bluetooth

    if not is_admin():
        raise HeadTrackerError("the repair needs administrator rights")
    api = _Api.get()
    apis = bluetooth._load_bluetooth_apis()
    bt_addr = bluetooth.parse_address(address)
    address_hex = f"{bt_addr:012X}"

    class _FIND_RADIO_PARAMS(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD)]

    apis.BluetoothFindFirstRadio.restype = ctypes.c_void_p
    apis.BluetoothFindFirstRadio.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    apis.BluetoothFindRadioClose.argtypes = [ctypes.c_void_p]
    apis.BluetoothSetServiceState.restype = wintypes.DWORD
    apis.BluetoothSetServiceState.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]

    params = _FIND_RADIO_PARAMS(ctypes.sizeof(_FIND_RADIO_PARAMS))
    radio = ctypes.c_void_p()
    find = apis.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(radio))
    if not find:
        raise HeadTrackerError("no Bluetooth radio found")
    apis.BluetoothFindRadioClose(find)
    try:
        info = bluetooth._device_info(bt_addr)
        hid_service = _GUID.from_uuid("00001124-0000-1000-8000-00805F9B34FB")
        log(f"Re-enabling the Bluetooth HID service of {info.szName} ({address})")
        result = apis.BluetoothSetServiceState(radio, ctypes.byref(info), ctypes.byref(hid_service),
                                               _BLUETOOTH_SERVICE_ENABLE)
        log(f"  enable: {result}")
        if result in (_ERROR_INVALID_PARAMETER, _E_INVALIDARG) and not find_head_trackers():
            log("  Windows thinks it is enabled but the sensor is missing; turning it off and on again")
            result = apis.BluetoothSetServiceState(radio, ctypes.byref(info), ctypes.byref(hid_service),
                                                   _BLUETOOTH_SERVICE_DISABLE)
            log(f"  disable: {result}")
            time.sleep(1.5)
            result = apis.BluetoothSetServiceState(radio, ctypes.byref(info), ctypes.byref(hid_service),
                                                   _BLUETOOTH_SERVICE_ENABLE)
            log(f"  enable: {result}")
    finally:
        ctypes.windll.kernel32.CloseHandle(radio)

    log("Waiting for Windows to set up the sensor…")
    rebound = False
    for _ in range(15):
        time.sleep(1)
        if find_head_trackers():
            log("Head tracking sensor is ready.")
            return
        failed = _failed_tracker_nodes(api, address_hex)
        if failed and not rebound:
            _bind_generic_hid(failed[0], log)
            rebound = True
    if not find_head_trackers():
        raise HeadTrackerError(
            "the sensor did not appear. Turn the headphones off and on, wait until they reconnect, "
            "and run the repair again."
        )


def _bind_generic_hid(hardware_id: str, log: Log) -> None:
    newdev = ctypes.WinDLL("newdev", use_last_error=True)
    newdev.UpdateDriverForPlugAndPlayDevicesW.restype = wintypes.BOOL
    newdev.UpdateDriverForPlugAndPlayDevicesW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                                          wintypes.DWORD, ctypes.POINTER(wintypes.BOOL)]
    inf = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "INF", "input.inf")
    log(f"The sensor failed to start (Code 10); using Windows' generic HID driver for {hardware_id}")
    reboot = wintypes.BOOL()
    if not newdev.UpdateDriverForPlugAndPlayDevicesW(None, hardware_id, inf,
                                                     _INSTALLFLAG_FORCE | _INSTALLFLAG_NONINTERACTIVE,
                                                     ctypes.byref(reboot)):
        raise HeadTrackerError(f"could not switch the sensor's driver: {_Api.error()}")
    log("  done" + (" (Windows asks for a restart)" if reboot.value else ""))

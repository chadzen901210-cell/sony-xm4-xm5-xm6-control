"""Sony MDR protocol: frame codec plus the few commands this tool uses on the WH-1000XM4/XM5/XM6.

Every constant here is documented, with its source, in PROTOCOL.md.
This module does no I/O, so it can be tested without headphones.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable

# --- Framing (PROTOCOL.md §2) ------------------------------------------------

HEADER = 0x3E
TRAILER = 0x3C
ESCAPE = 0x3D
_ESCAPE_MASK = 0xEF  # 3C/3D/3E are sent as 3D followed by 2C/2D/2E
_SPECIAL = frozenset((HEADER, TRAILER, ESCAPE))
_UNESCAPE = {b & _ESCAPE_MASK: b for b in _SPECIAL}

# Largest packet the reference clients expect; escaping can at most double it.
MAX_FRAME_SIZE = 2048
_MAX_BUFFER = MAX_FRAME_SIZE * 2


class MessageType(enum.IntEnum):
    """First byte after the header (PROTOCOL.md §3)."""

    ACK = 0x01
    COMMAND_1 = 0x0C  # "DATA_MDR": everything this tool sends
    COMMAND_2 = 0x0E  # "DATA_MDR_NO2": never sent by this tool


class ProtocolError(Exception):
    """Base class for all protocol errors."""


class FrameError(ProtocolError):
    """Malformed frame: bad markers, escaping, length or checksum."""


class PayloadError(ProtocolError):
    """The frame was valid but its payload is not what we expected."""


class ForbiddenCommandError(ProtocolError):
    """Refused to encode a frame that is not on the whitelist (PROTOCOL.md §7)."""


@dataclass(frozen=True)
class Frame:
    type: int
    seq: int
    payload: bytes = b""

    def __str__(self) -> str:
        try:
            name = MessageType(self.type).name
        except ValueError:
            name = f"type 0x{self.type:02x}"
        text = f"{name} seq={self.seq}"
        if self.payload:
            text += f" payload={to_hex(self.payload)}"
        return text


def to_hex(data: bytes) -> str:
    return data.hex(" ")


def checksum(data: bytes) -> int:
    """Low 8 bits of the byte sum."""
    return sum(data) & 0xFF


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in _SPECIAL:
            out += bytes((ESCAPE, b & _ESCAPE_MASK))
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    """Strict inverse of escape(): only 3D 2C / 3D 2D / 3D 2E are accepted."""
    out = bytearray()
    it = iter(data)
    for b in it:
        if b != ESCAPE:
            out.append(b)
            continue
        nxt = next(it, None)
        if nxt is None:
            raise FrameError("escape byte at end of frame")
        if nxt not in _UNESCAPE:
            raise FrameError(f"invalid escape sequence 3d {nxt:02x}")
        out.append(_UNESCAPE[nxt])
    return bytes(out)


def _pack(msg_type: int, seq: int, payload: bytes) -> bytes:
    """Frame raw bytes without any whitelist check. Only encode_frame() may send these."""
    body = bytes((msg_type, seq)) + len(payload).to_bytes(4, "big") + payload
    return bytes((HEADER,)) + escape(body + bytes((checksum(body),))) + bytes((TRAILER,))


def encode_frame(frame: Frame, version: str | None) -> bytes:
    """Encode a frame for sending to headphones that speak `version` (None before the handshake).

    Raises ForbiddenCommandError for anything off that version's whitelist.
    """
    check_allowed(frame, version)
    return _pack(frame.type, frame.seq, frame.payload)


def decode_frame(raw: bytes) -> Frame:
    """Decode one complete frame, header and trailer included."""
    if len(raw) < 2 or raw[0] != HEADER or raw[-1] != TRAILER:
        raise FrameError("frame must start with 3e and end with 3c")
    inner = raw[1:-1]
    if HEADER in inner or TRAILER in inner:
        raise FrameError("unescaped marker byte inside frame")
    body = unescape(inner)
    # type(1) + seq(1) + length(4) + checksum(1)
    if len(body) < 7:
        raise FrameError(f"frame too short ({len(body)} bytes after unescaping)")
    expected = checksum(body[:-1])
    if body[-1] != expected:
        raise FrameError(f"checksum mismatch: got {body[-1]:02x}, expected {expected:02x}")
    length = int.from_bytes(body[2:6], "big")
    payload = body[6:-1]
    if length != len(payload):
        raise FrameError(f"length field says {length} bytes, payload has {len(payload)}")
    return Frame(body[0], body[1], payload)


class FrameParser:
    """Split a byte stream into frames. Feed it whatever recv() returns.

    Bytes that cannot be decoded are dropped and passed to on_error, so a
    corrupted frame never blocks the ones after it.
    """

    def __init__(self, on_error: Callable[[bytes, FrameError], None] | None = None) -> None:
        self._buf = bytearray()
        self._on_error = on_error

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        frames: list[Frame] = []
        while self._buf:
            start = self._buf.find(HEADER)
            if start < 0:
                self._drop(len(self._buf), "bytes outside any frame")
                break
            if start > 0:
                self._drop(start, "bytes before frame header")

            end = self._buf.find(TRAILER, 1)
            next_header = self._buf.find(HEADER, 1)
            if next_header > 0 and (end < 0 or next_header < end):
                self._drop(next_header, "frame cut off by a new header")
                continue
            if end < 0:
                if len(self._buf) > _MAX_BUFFER:
                    self._drop(len(self._buf), "no trailer within the maximum frame size")
                break  # wait for more data

            raw = bytes(self._buf[: end + 1])
            del self._buf[: end + 1]
            try:
                frames.append(decode_frame(raw))
            except FrameError as e:
                self._report(raw, e)
        return frames

    def _drop(self, count: int, reason: str) -> None:
        raw = bytes(self._buf[:count])
        del self._buf[:count]
        self._report(raw, FrameError(reason))

    def _report(self, raw: bytes, error: FrameError) -> None:
        if self._on_error is not None:
            self._on_error(raw, error)


def ack_for(frame: Frame) -> Frame:
    """The ACK we must send back for a frame received from the headphones (PROTOCOL.md §4)."""
    if frame.seq not in (0, 1):
        raise ProtocolError(f"cannot acknowledge sequence number {frame.seq}")
    return Frame(MessageType.ACK, 1 - frame.seq)


# --- Commands (PROTOCOL.md §5-6, §11) -----------------------------------------

# Protocol versions, as the init reply tells them apart (PROTOCOL.md §5)
V1 = "v1"  # WH-1000XM4
V2 = "v2"  # WH-1000XM5, WH-1000XM6


class Command(enum.IntEnum):
    """First payload byte of a COMMAND_1 frame. Names ending in _V1 exist only in protocol V1."""

    INIT_REQUEST = 0x00
    INIT_REPLY = 0x01
    BATTERY_GET_V1 = 0x10
    BATTERY_RET_V1 = 0x11
    BATTERY_NOTIFY_V1 = 0x13
    BATTERY_GET = 0x22  # V2 only: in V1, 0x22 means "power off"
    BATTERY_RET = 0x23
    BATTERY_NOTIFY = 0x25
    EQ_GET = 0x56
    EQ_RET = 0x57
    EQ_SET = 0x58
    EQ_NOTIFY = 0x59
    NCASM_GET = 0x66
    NCASM_RET = 0x67
    NCASM_SET = 0x68
    NCASM_NOTIFY = 0x69


BATTERY_SINGLE = 0x00
# V2 battery reply with an extra "threshold" byte (mos9527 BATTERY_WITH_THRESHOLD);
# tried when headphones do not answer BATTERY_SINGLE (PROTOCOL.md §11.3)
BATTERY_WITH_THRESHOLD = 0x08
BATTERY_KINDS = (BATTERY_SINGLE, BATTERY_WITH_THRESHOLD)
BATTERY_REPLIES = {
    V1: frozenset((Command.BATTERY_RET_V1, Command.BATTERY_NOTIFY_V1)),
    V2: frozenset((Command.BATTERY_RET, Command.BATTERY_NOTIFY)),
}

# NC/ambient sub-types (PROTOCOL.md §6.2, §11). Each model tries its own list
# in order (models.py); the XM5 answers 0x17.
NCASM_SUBTYPE_V1 = 0x02  # V1 layout, with wind noise reduction (WH-1000XM4)
NCASM_SUBTYPE_AUTO = 0x19  # V2 with auto ambient sound (WH-1000XM6, mos9527 "..._SEAMLESS_NA")
NCASM_SUBTYPES = (0x17, 0x15, NCASM_SUBTYPE_AUTO)  # every V2 sub-type
# Levels we write. The XM5 turns a written 0 into 1 (PROTOCOL.md §6.2), so 0 is
# not offered; replies are still parsed leniently from 0.
AMBIENT_LEVEL_MIN = 1
AMBIENT_LEVEL_MAX = 20
AUTO_AMBIENT_SENSITIVITIES = (0, 1, 2)  # standard, high, low
_VALUE_CHANGED = 0x01  # 0x00 means "slider still moving"
_V1_ON_WRITE = 0x11  # V1 switches on with 0x11 and reports 0x01 (Gadgetbridge V1)
_V1_ON_READ = (0x01, 0x11)
_V1_WIND_CAPABLE = 0x02  # V1 setting type: 0 ambient, 1 wind noise reduction, 2 noise cancelling

# Equalizer (PROTOCOL.md §6.3, §11)
_EQ_TYPE = {V1: 0x01, V2: 0x00}  # "PRESET_EQ" inquired type
_EQ_V1_BANDS_PRESET = 0xFF  # V1 writes hand-set bands with this preset byte (Gadgetbridge V1)
EQ_BAND_NAMES = ("400 Hz", "1 kHz", "2.5 kHz", "6.3 kHz", "16 kHz")  # plus Clear Bass; XM4, XM5
EQ_BAND_NAMES_10 = ("31 Hz", "63 Hz", "125 Hz", "250 Hz", "500 Hz", "1 kHz", "2 kHz", "4 kHz", "8 kHz",
                    "16 kHz")  # no Clear Bass; XM6
EQ_STEP_MIN = -10
EQ_STEP_MAX = 10
EQ_STEP_MAX_10 = 6  # the 10-band equalizer goes from -6 to +6


def eq_band_names(count: int) -> tuple[str, ...]:
    return EQ_BAND_NAMES_10 if count == len(EQ_BAND_NAMES_10) else EQ_BAND_NAMES


def eq_step_max(count: int) -> int:
    """Steps go from -max to +max; on the wire they travel as 0..2*max."""
    return EQ_STEP_MAX_10 if count == len(EQ_BAND_NAMES_10) else EQ_STEP_MAX


class EqPreset(enum.IntEnum):
    OFF = 0x00
    BRIGHT = 0x10
    EXCITED = 0x11
    MELLOW = 0x12
    RELAXED = 0x13
    VOCAL = 0x14
    TREBLE_BOOST = 0x15
    BASS_BOOST = 0x16
    SPEECH = 0x17
    # The WH-1000XM6 replaced the presets above with these (mos9527 EqPresetId)
    HEAVY = 0x30
    CLEAR = 0x31
    HARD = 0x32
    SOFT = 0x33
    # Slots that keep hand-set bands, named as in Sony's app. The XM5 reported
    # CUSTOM_1 with the user's own bands on hardware (PROTOCOL.md §6.3).
    MANUAL = 0xA0
    CUSTOM_1 = 0xA1
    CUSTOM_2 = 0xA2


EQ_BAND_SLOTS = (EqPreset.MANUAL, EqPreset.CUSTOM_1, EqPreset.CUSTOM_2)


class ChargingStatus(enum.IntEnum):
    NOT_CHARGING = 0
    CHARGING = 1
    UNKNOWN = 2
    CHARGED = 3


class NcAsmMode(enum.Enum):
    OFF = "off"
    NOISE_CANCELLING = "noise_cancelling"
    AMBIENT = "ambient"
    WIND = "wind"  # wind noise reduction: protocol V1 (WH-1000XM4) only


@dataclass(frozen=True)
class Battery:
    level: int
    charging: ChargingStatus | int  # raw int if the value is not one we know


@dataclass(frozen=True)
class NcAsmState:
    mode: NcAsmMode
    focus_on_voice: bool
    ambient_level: int
    # Only with sub-type 0x19 (WH-1000XM6): the ambient level follows the
    # surroundings. None means the headphones do not have it.
    auto_ambient: bool | None = None
    auto_ambient_sensitivity: int = 0  # kept as the headphones report it


@dataclass(frozen=True)
class Equalizer:
    preset: EqPreset | int  # raw int if the preset is not one we know
    clear_bass: int | None  # -10..+10; None without band values or on 10-band headphones
    bands: tuple[int, ...]  # one step per eq_band_names(len(bands)); empty when not sent


def init_request() -> bytes:
    return bytes((Command.INIT_REQUEST, 0x00))


def battery_request(version: str = V2, kind: int = BATTERY_SINGLE) -> bytes:
    if version == V1:
        if kind != BATTERY_SINGLE:
            raise ValueError("protocol V1 only has the single battery query")
        return bytes((Command.BATTERY_GET_V1, BATTERY_SINGLE))
    if kind not in BATTERY_KINDS:
        raise ValueError(f"battery kind must be one of {BATTERY_KINDS}, got {kind!r}")
    return bytes((Command.BATTERY_GET, kind))


def ncasm_request(subtype: int) -> bytes:
    _check_subtype(subtype)
    return bytes((Command.NCASM_GET, subtype))


def ncasm_set(subtype: int, state: NcAsmState) -> bytes:
    _check_subtype(subtype)
    if not AMBIENT_LEVEL_MIN <= state.ambient_level <= AMBIENT_LEVEL_MAX:
        raise ValueError(
            f"ambient level must be {AMBIENT_LEVEL_MIN}-{AMBIENT_LEVEL_MAX}, got {state.ambient_level}"
        )
    if subtype == NCASM_SUBTYPE_V1:
        return _ncasm_set_v1(state)
    if state.mode is NcAsmMode.WIND:
        raise ValueError("wind noise reduction is only available on the WH-1000XM4")
    payload = bytes((
        Command.NCASM_SET,
        subtype,
        _VALUE_CHANGED,
        0x00 if state.mode is NcAsmMode.OFF else 0x01,
        0x01 if state.mode is NcAsmMode.AMBIENT else 0x00,
        0x01 if state.focus_on_voice else 0x00,
        state.ambient_level,
    ))
    if subtype == NCASM_SUBTYPE_AUTO:
        if state.auto_ambient_sensitivity not in AUTO_AMBIENT_SENSITIVITIES:
            raise ValueError(f"unknown auto ambient sensitivity {state.auto_ambient_sensitivity!r}")
        payload += bytes((0x01 if state.auto_ambient else 0x00, state.auto_ambient_sensitivity))
    return payload


def _ncasm_set_v1(state: NcAsmState) -> bytes:
    """[68, 02, on, 02, mode, 01, voice, level], as Gadgetbridge V1 writes it for wind-capable headphones."""
    mode = {NcAsmMode.WIND: 1, NcAsmMode.NOISE_CANCELLING: 2}.get(state.mode, 0)
    # Gadgetbridge sends level 0 with noise cancelling and wind noise reduction
    level = 0 if mode else state.ambient_level
    return bytes((
        Command.NCASM_SET,
        NCASM_SUBTYPE_V1,
        0x00 if state.mode is NcAsmMode.OFF else _V1_ON_WRITE,
        _V1_WIND_CAPABLE,
        mode,
        0x01,
        0x01 if state.focus_on_voice else 0x00,
        level,
    ))


def equalizer_request(version: str = V2) -> bytes:
    return bytes((Command.EQ_GET, _EQ_TYPE[version]))


def equalizer_preset_set(preset: EqPreset, version: str = V2) -> bytes:
    """Select a preset; with no band values the headphones use the preset's own."""
    return bytes((Command.EQ_SET, _EQ_TYPE[version], EqPreset(preset), 0x00))


def equalizer_bands_set(
    clear_bass: int | None,
    bands: tuple[int, ...] | list[int],
    slot: EqPreset = EqPreset.MANUAL,
    version: str = V2,
) -> bytes:
    """Hand-set bands: Clear Bass and 5 bands (XM4, XM5), or 10 bands and no Clear Bass (XM6).

    V2 stores them in one of EQ_BAND_SLOTS (and selects it). V1 has no slots
    and ignores `slot`.
    """
    if len(bands) == len(EQ_BAND_NAMES):
        if clear_bass is None:
            raise ValueError("5-band equalizers need a Clear Bass value")
        values = [clear_bass, *bands]
    elif len(bands) == len(EQ_BAND_NAMES_10) and version == V2:
        if clear_bass is not None:
            raise ValueError("the 10-band equalizer has no Clear Bass")
        values = list(bands)
    else:
        raise ValueError(f"need {len(EQ_BAND_NAMES)} band values (or {len(EQ_BAND_NAMES_10)} on the WH-1000XM6), "
                         f"got {len(bands)}")
    step_max = eq_step_max(len(bands))
    for value in values:
        if type(value) is not int or not -step_max <= value <= step_max:
            raise ValueError(f"equalizer values must be whole numbers from {-step_max} to {step_max}")
    if version == V1:
        preset = _EQ_V1_BANDS_PRESET
    elif slot in EQ_BAND_SLOTS:
        preset = slot
    else:
        raise ValueError(f"bands can only be stored in {', '.join(s.name for s in EQ_BAND_SLOTS)}")
    return bytes((Command.EQ_SET, _EQ_TYPE[version], preset, len(values), *(v + step_max for v in values)))


def parse_equalizer(payload: bytes) -> Equalizer:
    """Parse an equalizer reply or notification: [57|59, type, preset, count, steps...].

    type is 00 (V2) or 01 (V1); count is 0, 6 (Clear Bass and 5 bands) or 10 (10 bands, V2).
    """
    if (
        len(payload) < 4
        or payload[0] not in (Command.EQ_RET, Command.EQ_NOTIFY)
        or payload[1] not in (_EQ_TYPE[V1], _EQ_TYPE[V2])
        or len(payload) != 4 + payload[3]
        or payload[3] not in (0, 1 + len(EQ_BAND_NAMES), len(EQ_BAND_NAMES_10))
        or (payload[1] == _EQ_TYPE[V1] and payload[3] == len(EQ_BAND_NAMES_10))
    ):
        raise PayloadError(f"not an equalizer reply: {to_hex(payload)}")
    try:
        preset: EqPreset | int = EqPreset(payload[2])
    except ValueError:
        preset = payload[2]
    count = payload[3]
    if count == 0:
        return Equalizer(preset, None, ())
    step_max = eq_step_max(count)
    steps = [b - step_max for b in payload[4:]]
    if any(not -step_max <= s <= step_max for s in steps):
        raise PayloadError(f"equalizer value out of range: {to_hex(payload)}")
    if count == len(EQ_BAND_NAMES_10):
        return Equalizer(preset, None, tuple(steps))
    return Equalizer(preset, steps[0], tuple(steps[1:]))


def parse_init_reply(payload: bytes) -> str:
    """Return V1 or V2. Only the payload length is used (PROTOCOL.md §5)."""
    if not payload or payload[0] != Command.INIT_REPLY:
        raise PayloadError(f"not an init reply: {to_hex(payload)}")
    if len(payload) == 8:
        return V2
    if len(payload) == 4:
        return V1
    raise PayloadError(f"unexpected init reply length {len(payload)}: {to_hex(payload)}")


def parse_battery(payload: bytes) -> Battery:
    """V1: [11|13, 00, level, charging]. V2: [23|25, 00, level, charging] or [23|25, 08, level, charging, threshold]."""
    if len(payload) >= 2 and payload[0] in BATTERY_REPLIES[V1]:
        kinds: tuple[int, ...] = (BATTERY_SINGLE,)
    elif len(payload) >= 2 and payload[0] in BATTERY_REPLIES[V2]:
        kinds = BATTERY_KINDS
    else:
        kinds = ()
    if len(payload) < 4 or payload[1] not in kinds:
        raise PayloadError(f"not a single-battery reply: {to_hex(payload)}")
    level = payload[2]
    if level > 100:
        raise PayloadError(f"battery level {level} out of range")
    try:
        charging: ChargingStatus | int = ChargingStatus(payload[3])
    except ValueError:
        charging = payload[3]
    return Battery(level, charging)


def parse_ncasm(payload: bytes) -> tuple[int, NcAsmState]:
    """Parse an NC/ambient reply or notification. Returns (subtype, state)."""
    if len(payload) < 2 or payload[0] not in (Command.NCASM_RET, Command.NCASM_NOTIFY):
        raise PayloadError(f"not an NC/ambient reply: {to_hex(payload)}")
    subtype = payload[1]
    if subtype == NCASM_SUBTYPE_V1:
        return subtype, _parse_ncasm_v1(payload)
    if subtype not in NCASM_SUBTYPES:
        raise PayloadError(f"unknown NC/ambient subtype 0x{subtype:02x}")
    expected = 9 if subtype == NCASM_SUBTYPE_AUTO else 7
    if len(payload) != expected:
        raise PayloadError(f"not a {expected}-byte NC/ambient reply: {to_hex(payload)}")
    _changing, on, mode, focus, level = payload[2:7]
    if on not in (0, 1) or mode not in (0, 1) or focus not in (0, 1):
        raise PayloadError(f"unexpected NC/ambient values: {to_hex(payload)}")
    if level > AMBIENT_LEVEL_MAX:
        raise PayloadError(f"ambient level {level} out of range")
    if not on:
        nc_mode = NcAsmMode.OFF
    elif mode:
        nc_mode = NcAsmMode.AMBIENT
    else:
        nc_mode = NcAsmMode.NOISE_CANCELLING
    if subtype != NCASM_SUBTYPE_AUTO:
        return subtype, NcAsmState(nc_mode, bool(focus), level)
    auto, sensitivity = payload[7:9]
    if auto not in (0, 1) or sensitivity not in AUTO_AMBIENT_SENSITIVITIES:
        raise PayloadError(f"unexpected auto ambient values: {to_hex(payload)}")
    return subtype, NcAsmState(nc_mode, bool(focus), level, bool(auto), sensitivity)


def _parse_ncasm_v1(payload: bytes) -> NcAsmState:
    """[67|69, 02, on, setting type, mode, ?, voice, level] (Gadgetbridge V1 handleAmbientSoundControl)."""
    if len(payload) != 8:
        raise PayloadError(f"not an 8-byte NC/ambient reply: {to_hex(payload)}")
    on, setting, mode, _, focus, level = payload[2:]
    modes = {
        0x00: {0: NcAsmMode.AMBIENT, 1: NcAsmMode.NOISE_CANCELLING},
        _V1_WIND_CAPABLE: {0: NcAsmMode.AMBIENT, 1: NcAsmMode.WIND, 2: NcAsmMode.NOISE_CANCELLING},
    }.get(setting, {})
    if (on != 0 and on not in _V1_ON_READ) or mode not in modes or focus not in (0, 1):
        raise PayloadError(f"unexpected NC/ambient values: {to_hex(payload)}")
    if level > AMBIENT_LEVEL_MAX:
        raise PayloadError(f"ambient level {level} out of range")
    return NcAsmState(modes[mode] if on else NcAsmMode.OFF, bool(focus), level)


def _check_subtype(subtype: int) -> None:
    if subtype != NCASM_SUBTYPE_V1 and subtype not in NCASM_SUBTYPES:
        raise ValueError(f"unknown NC/ambient subtype {subtype!r}")


# --- Whitelist (PROTOCOL.md §7) ----------------------------------------------

# Exact payloads allowed for each protocol version. The two versions give some
# command bytes different meanings (0x22 is the V2 battery query but V1's
# "power off"), so a payload is only allowed for the version it was built for.
_ALLOWED_FIXED_PAYLOADS = {
    V1: frozenset(
        [init_request(), battery_request(V1), equalizer_request(V1), ncasm_request(NCASM_SUBTYPE_V1)]
        + [equalizer_preset_set(p, V1) for p in EqPreset]
    ),
    V2: frozenset(
        [init_request(), equalizer_request(V2)]
        + [battery_request(V2, k) for k in BATTERY_KINDS]
        + [ncasm_request(s) for s in NCASM_SUBTYPES]
        + [equalizer_preset_set(p, V2) for p in EqPreset]
    ),
}


def check_allowed(frame: Frame, version: str | None) -> None:
    """Raise ForbiddenCommandError unless the frame is one this tool is allowed to send.

    `version` is the protocol the headphones answered the handshake with; before
    the handshake (None) only the init request may be sent. Firmware update,
    factory reset, reboot, power off and pairing commands can never pass this
    check, whatever code path builds them.
    """
    if frame.seq not in (0, 1):
        raise ForbiddenCommandError(f"invalid sequence number {frame.seq}")
    if frame.type == MessageType.ACK:
        if frame.payload:
            raise ForbiddenCommandError("ACK frames must have an empty payload")
        return
    if frame.type == MessageType.COMMAND_1:
        payload = bytes(frame.payload)
        if version is None:
            if payload == init_request():
                return
        elif version == V1:
            if (
                payload in _ALLOWED_FIXED_PAYLOADS[V1]
                or _is_allowed_ncasm_set_v1(payload)
                or _is_allowed_eq_bands(payload, V1)
            ):
                return
        elif version == V2:
            if (
                payload in _ALLOWED_FIXED_PAYLOADS[V2]
                or _is_allowed_ncasm_set(payload)
                or _is_allowed_eq_bands(payload, V2)
            ):
                return
    raise ForbiddenCommandError(f"refusing to send non-whitelisted frame (protocol {version}): {frame}")


def _is_allowed_eq_bands(p: bytes, version: str) -> bool:
    if len(p) < 4 or p[:2] != bytes((Command.EQ_SET, _EQ_TYPE[version])):
        return False
    count = p[3]
    if version == V1:
        counts: tuple[int, ...] = (1 + len(EQ_BAND_NAMES),)
        slots: tuple[int, ...] = (_EQ_V1_BANDS_PRESET,)
    else:
        counts = (1 + len(EQ_BAND_NAMES), len(EQ_BAND_NAMES_10))
        slots = EQ_BAND_SLOTS
    step_max = eq_step_max(count)
    return (
        count in counts
        and len(p) == 4 + count
        and p[2] in slots
        and all(0 <= b <= 2 * step_max for b in p[4:])
    )


def _is_allowed_ncasm_set(p: bytes) -> bool:
    """V2: [68, 15|17, 01, on, ambient, voice, level] or [68, 19, ..., auto, sensitivity]."""
    expected = 9 if len(p) > 1 and p[1] == NCASM_SUBTYPE_AUTO else 7
    return (
        len(p) == expected
        and p[0] == Command.NCASM_SET
        and p[1] in NCASM_SUBTYPES
        and p[2] == _VALUE_CHANGED
        and p[3] in (0, 1)
        and p[4] in (0, 1)
        and p[5] in (0, 1)
        and AMBIENT_LEVEL_MIN <= p[6] <= AMBIENT_LEVEL_MAX
        and (expected == 7 or (p[7] in (0, 1) and p[8] in AUTO_AMBIENT_SENSITIVITIES))
    )


def _is_allowed_ncasm_set_v1(p: bytes) -> bool:
    """V1: [68, 02, 00|11, 02, mode 0..2, 01, voice, level]."""
    return (
        len(p) == 8
        and p[0] == Command.NCASM_SET
        and p[1] == NCASM_SUBTYPE_V1
        and p[2] in (0x00, _V1_ON_WRITE)
        and p[3] == _V1_WIND_CAPABLE
        and p[4] in (0, 1, 2)
        and p[5] == 0x01
        and p[6] in (0, 1)
        and 0 <= p[7] <= AMBIENT_LEVEL_MAX
    )

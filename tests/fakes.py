"""Stand-ins for the headphones, shared by the tests."""

import dataclasses
import time
from collections import deque

from sonyxm.protocol import (
    BATTERY_SINGLE,
    BATTERY_WITH_THRESHOLD,
    NCASM_SUBTYPE_AUTO,
    NCASM_SUBTYPE_V1,
    V1,
    V2,
    Command,
    EqPreset,
    Equalizer,
    Frame,
    MessageType,
    NcAsmMode,
    _pack,
    decode_frame,
    parse_ncasm,
)

CMD = MessageType.COMMAND_1
ACK = MessageType.ACK
INIT_REPLY = bytes.fromhex("01 00 03 00 00 00 00 00")  # WH-1000XM5 fw 1.1.3
INIT_REPLY_V1 = bytes.fromhex("01 00 40 10")  # WH-1000XM3, from Gadgetbridge's SonyHeadphonesProtocol
BATTERY_REPLY = bytes.fromhex("23 00 4b 00")  # 75 %, not charging


def device_ack(host_seq: int) -> bytes:
    return _pack(ACK, 1 - host_seq, b"")


def device_frame(seq: int, payload: bytes, msg_type: int = CMD) -> bytes:
    return _pack(msg_type, seq, payload)


class FakeSocket:
    """Records what the host sends and replays scripted bytes from the 'headphones'.

    respond(frame) is called for every frame the host sends and returns the
    chunks the headphones send back. Tests may also append to `incoming`
    directly (from any thread) to make the headphones speak on their own.
    """

    def __init__(self, respond=None):
        self.respond = respond or (lambda frame: [])
        self.sent: list[Frame] = []
        self.incoming: deque[bytes] = deque()
        self.timeout = None
        self.closed = False

    def sendall(self, data: bytes) -> None:
        frame = decode_frame(data)
        self.sent.append(frame)
        self.incoming.extend(self.respond(frame))

    def recv(self, size: int) -> bytes:
        if self.incoming:
            return self.incoming.popleft()
        time.sleep(self.timeout or 0)
        raise TimeoutError

    def settimeout(self, value) -> None:
        self.timeout = value

    def close(self) -> None:
        self.closed = True

    def host_commands(self) -> list[Frame]:
        return [f for f in self.sent if f.type == CMD]

    def host_acks(self) -> list[Frame]:
        return [f for f in self.sent if f.type == ACK]


def well_behaved(frame: Frame) -> list[bytes]:
    """ACK every command, then answer init and battery requests."""
    if frame.type != CMD:
        return []
    replies = {Command.INIT_REQUEST: INIT_REPLY, Command.BATTERY_GET: BATTERY_REPLY}
    chunks = [device_ack(frame.seq)]
    if frame.payload[0] in replies:
        chunks.append(device_frame(0, replies[frame.payload[0]]))
    return chunks


class SimulatedXm5:
    """Headphones that keep NC/ambient and equalizer state and apply writes, like the real XM5.

    Answers NC queries only for its own sub-type, and sends a notification
    after every write. min_level imitates headphones that refuse low levels.
    With protocol="v1" and sub-type 0x02 it plays a WH-1000XM4; see also
    simulated_xm4() and simulated_xm6().
    """

    # Band values the headphones report for a preset (Bright from Gadgetbridge's tests)
    PRESET_BANDS = {EqPreset.BRIGHT: (-1, (0, 5, 7, 7, 9))}

    def __init__(self, state, *, subtype=0x17, apply_writes=True, min_level=0, equalizer=None, protocol=V2,
                 battery_kind=BATTERY_SINGLE):
        self.state = state
        self.subtype = subtype
        self.apply_writes = apply_writes
        self.min_level = min_level
        self.equalizer = equalizer or Equalizer(EqPreset.OFF, 0, (0, 0, 0, 0, 0))
        self.protocol = protocol
        self.battery_kind = battery_kind
        self.writes = []

    def __call__(self, frame):
        if frame.type != CMD:
            return []
        command = frame.payload[0]
        if command == Command.INIT_REQUEST and self.protocol == V1:
            return [device_ack(frame.seq), device_frame(0, INIT_REPLY_V1)]
        if command in (Command.BATTERY_GET, Command.BATTERY_GET_V1):
            return [device_ack(frame.seq)] + self.battery_reply(frame.payload)
        if command == Command.NCASM_GET:
            chunks = [device_ack(frame.seq)]
            if frame.payload[1] == self.subtype:
                chunks.append(device_frame(0, self.payload(Command.NCASM_RET)))
            return chunks
        if command == Command.NCASM_SET:
            self.writes.append(frame.payload.hex(" "))
            _, wanted = parse_ncasm(bytes((Command.NCASM_RET,)) + frame.payload[1:])
            if self.apply_writes:
                self.state = dataclasses.replace(wanted, ambient_level=max(wanted.ambient_level, self.min_level))
            return [device_ack(frame.seq), device_frame(1, self.payload(Command.NCASM_NOTIFY))]
        if command == Command.EQ_GET:
            return [device_ack(frame.seq), device_frame(0, self.eq_payload(Command.EQ_RET))]
        if command == Command.EQ_SET:
            self.writes.append(frame.payload.hex(" "))
            p = frame.payload
            if self.apply_writes:
                ten = len(self.equalizer.bands) == 10
                if p[3] == 0:
                    clear_bass, bands = (None, (0,) * 10) if ten else self.PRESET_BANDS.get(p[2], (0, (0,) * 5))
                elif ten:
                    clear_bass, bands = None, tuple(b - 6 for b in p[4:])
                else:
                    steps = [b - 10 for b in p[4:]]
                    clear_bass, bands = steps[0], tuple(steps[1:])
                preset = EqPreset.MANUAL if p[2] == 0xFF else EqPreset(p[2])  # V1 files hand-set bands as Manual
                self.equalizer = Equalizer(preset, clear_bass, bands)
            return [device_ack(frame.seq), device_frame(1, self.eq_payload(Command.EQ_NOTIFY))]
        return well_behaved(frame)

    def battery_reply(self, payload):
        if self.protocol == V1:
            return [device_frame(0, bytes.fromhex("11 00 4b 00"))] if payload[0] == Command.BATTERY_GET_V1 else []
        if payload[0] != Command.BATTERY_GET or payload[1] != self.battery_kind:
            return []  # a query these headphones do not know: ACK only
        extra = b"\x14" if self.battery_kind == BATTERY_WITH_THRESHOLD else b""
        return [device_frame(0, bytes((Command.BATTERY_RET, self.battery_kind, 0x4B, 0x00)) + extra)]

    def eq_payload(self, command):
        e = self.equalizer
        kind = 0x01 if self.protocol == V1 else 0x00
        if len(e.bands) == 10:
            return bytes((command, kind, e.preset, 10, *(b + 6 for b in e.bands)))
        return bytes((command, kind, e.preset, 6, e.clear_bass + 10, *(b + 10 for b in e.bands)))

    def press_button(self, sock: FakeSocket, state) -> None:
        """The user changes the mode on the headphones themselves."""
        self.state = state
        sock.incoming.append(device_frame(1, self.payload(Command.NCASM_NOTIFY)))

    def payload(self, command):
        s = self.state
        if self.subtype == NCASM_SUBTYPE_V1:
            mode = {NcAsmMode.WIND: 1, NcAsmMode.NOISE_CANCELLING: 2}.get(s.mode, 0)
            level = 0 if mode else s.ambient_level
            return bytes((command, self.subtype, 0 if s.mode is NcAsmMode.OFF else 1, 0x02, mode, 0x01,
                          int(s.focus_on_voice), level))
        payload = bytes((
            command, self.subtype, 0x01,
            0 if s.mode is NcAsmMode.OFF else 1,
            1 if s.mode is NcAsmMode.AMBIENT else 0,
            int(s.focus_on_voice),
            s.ambient_level,
        ))
        if self.subtype == NCASM_SUBTYPE_AUTO:
            payload += bytes((int(bool(s.auto_ambient)), s.auto_ambient_sensitivity))
        return payload


def simulated_xm4(state, **kwargs):
    return SimulatedXm5(state, subtype=NCASM_SUBTYPE_V1, protocol=V1, **kwargs)


def simulated_xm6(state, **kwargs):
    kwargs.setdefault("equalizer", Equalizer(EqPreset.OFF, None, (0,) * 10))
    return SimulatedXm5(state, subtype=NCASM_SUBTYPE_AUTO, battery_kind=BATTERY_WITH_THRESHOLD, **kwargs)

"""A session with the headphones: sequence numbers, ACKs, timeouts (PROTOCOL.md §4-5).

Works on any object with socket-like sendall/recv/settimeout/close, so it can
be tested with a fake socket.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Protocol

from . import bluetooth
from .models import SERVICE_V1, SERVICE_V2, find_model
from .protocol import (
    V1,
    Command,
    Frame,
    FrameError,
    FrameParser,
    MessageType,
    ack_for,
    encode_frame,
    init_request,
    parse_init_reply,
    to_hex,
)

Log = Callable[[str], None]

DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_REQUEST_TIMEOUT = 5.0
# Gadgetbridge waits this long after connecting before the first write.
SETTLE_DELAY = 0.5
# Gadgetbridge resends the init request this often, at most twice, because the
# headphones sometimes ignore the first one.
INIT_RETRY_INTERVAL = 1.25
INIT_RETRIES = 2


class SessionError(Exception):
    """Base class for errors while talking to connected headphones."""


class ResponseTimeout(SessionError):
    pass


class ConnectionClosed(SessionError):
    pass


class SocketLike(Protocol):
    def sendall(self, data: bytes, /) -> None: ...
    def recv(self, size: int, /) -> bytes: ...
    def settimeout(self, value: float | None, /) -> None: ...
    def close(self) -> None: ...


class Connection:
    def __init__(
        self,
        sock: SocketLike,
        log: Log | None = None,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self._sock = sock
        self._log = log
        self._request_timeout = request_timeout
        self._parser = FrameParser(on_error=self._on_bad_bytes)
        self._seq = 0
        # Protocol version from the handshake. Until then only the init request
        # may be sent (protocol.check_allowed).
        self.version: str | None = None
        # State of the request in flight, if any
        self._sent: Frame | None = None
        self._acked = False
        self._expect: frozenset[int] = frozenset()
        self._reply: Frame | None = None
        # Receives frames the headphones send on their own while listen() runs
        self._listener: Callable[[Frame], None] | None = None

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._sock.close()

    def handshake(self) -> str:
        """Send the init request; return the protocol version ("v1" or "v2") and use it from now on."""
        reply = self.request(
            init_request(),
            {Command.INIT_REPLY},
            retries=INIT_RETRIES,
            retry_interval=INIT_RETRY_INTERVAL,
        )
        assert reply is not None
        self.version = parse_init_reply(reply.payload)
        return self.version

    def request(
        self,
        payload: bytes,
        reply_commands: Iterable[int] = (),
        *,
        timeout: float | None = None,
        retries: int = 0,
        retry_interval: float = INIT_RETRY_INTERVAL,
    ) -> Frame | None:
        """Send one COMMAND_1 payload and wait for its ACK.

        If reply_commands is given, also wait for a COMMAND_1 frame whose first
        payload byte is one of them, and return it. ACK and reply may arrive in
        either order. The request is only resent while no ACK has arrived.
        """
        if timeout is None:
            timeout = self._request_timeout
        frame = Frame(MessageType.COMMAND_1, self._seq, payload)
        self._sent = frame
        self._acked = False
        self._expect = frozenset(reply_commands)
        self._reply = None
        try:
            deadline = time.monotonic() + timeout
            self._send(frame)
            next_retry = time.monotonic() + retry_interval
            while not self._done():
                now = time.monotonic()
                if now >= deadline:
                    return self._give_up(timeout)
                if not self._acked and retries > 0 and now >= next_retry:
                    retries -= 1
                    self._debug("no ACK yet, resending")
                    self._send(frame)
                    next_retry = now + retry_interval
                wait_until = deadline if self._acked or retries == 0 else min(deadline, next_retry)
                self._read(wait_until - now)
            return self._reply
        finally:
            self._sent = None

    def listen(self, on_frame: Callable[[Frame], None], duration: float | None = None) -> None:
        """Pass frames the headphones send on their own (notifications) to on_frame.

        Every frame is acknowledged. Runs for `duration` seconds, or until
        interrupted (KeyboardInterrupt) when duration is None.
        """
        deadline = None if duration is None else time.monotonic() + duration
        self._listener = on_frame
        try:
            while deadline is None or time.monotonic() < deadline:
                # Wake up at least once a second so Ctrl+C is noticed on Windows
                remaining = 1.0 if deadline is None else deadline - time.monotonic()
                self._read(min(remaining, 1.0))
        finally:
            self._listener = None

    def _done(self) -> bool:
        return self._acked and (not self._expect or self._reply is not None)

    def _give_up(self, timeout: float) -> Frame:
        assert self._sent is not None
        if self._reply is not None:
            # The headphones answered but the ACK got lost. The ACK would have
            # carried the next sequence number, so derive it ourselves.
            self._seq = 1 - self._sent.seq
            self._debug("reply arrived without an ACK; continuing")
            return self._reply
        what = "ACK" if not self._acked else "reply"
        raise ResponseTimeout(
            f"no {what} from the headphones within {timeout:g}s for {to_hex(self._sent.payload)}"
        )

    def _send(self, frame: Frame) -> None:
        raw = encode_frame(frame, self.version)  # refuses anything off this protocol's whitelist
        self._debug(f">> {to_hex(raw)}    [{frame}]")
        self._sock.sendall(raw)

    def _read(self, timeout: float) -> None:
        self._sock.settimeout(max(timeout, 0.001))
        try:
            data = self._sock.recv(4096)
        except TimeoutError:
            return
        if not data:
            raise ConnectionClosed("the headphones closed the connection")
        self._debug(f"<< {to_hex(data)}")
        for frame in self._parser.feed(data):
            self._on_frame(frame)

    def _on_frame(self, frame: Frame) -> None:
        self._debug(f"   [{frame}]")
        if frame.type == MessageType.ACK:
            self._on_ack(frame)
            return
        if frame.type in (MessageType.COMMAND_1, MessageType.COMMAND_2):
            self._send(ack_for(frame))
            if (
                self._sent is not None
                and self._reply is None
                and frame.type == MessageType.COMMAND_1
                and frame.payload
                and frame.payload[0] in self._expect
            ):
                self._reply = frame
            elif self._listener is not None:
                self._listener(frame)
            else:
                self._debug("   (not the reply we are waiting for; ignored)")
            return
        self._debug("   (unknown message type; ignored)")

    def _on_ack(self, frame: Frame) -> None:
        if self._sent is None or self._acked or frame.seq != 1 - self._sent.seq:
            self._debug("   (stale ACK; ignored)")
            return
        self._acked = True
        self._seq = frame.seq  # the next request uses the ACK's sequence number

    def _on_bad_bytes(self, raw: bytes, error: FrameError) -> None:
        self._debug(f"   dropped {to_hex(raw)}: {error}")

    def _debug(self, text: str) -> None:
        if self._log is not None:
            self._log(text)


def connect(
    address: str | None = None,
    *,
    timeout: float = DEFAULT_CONNECT_TIMEOUT,
    log: Log | None = None,
) -> tuple[Connection, bluetooth.PairedDevice]:
    """Pick the paired headphones (by address, or any supported model by name), connect, and wait a moment.

    Connects to the control service the model is known to offer, and falls back
    to the other one if the headphones do not have it.
    """
    device = bluetooth.pick_headphones(bluetooth.list_paired_devices(), address)
    model = find_model(device.name)
    if log:
        log(f"using {device.name} at {device.address} (connected: {device.connected}, "
            f"model: {model.name if model else 'unknown'})")
    services = [SERVICE_V1, SERVICE_V2] if model is not None and model.protocol == V1 else [SERVICE_V2, SERVICE_V1]
    for service in services:
        try:
            sock = bluetooth.open_rfcomm(device.address, timeout=timeout, log=log, service=service)
            break
        except bluetooth.ServiceNotFoundError:
            if service == services[-1]:
                raise
            if log:
                log(f"no service {service}; trying the other Sony control service")
    time.sleep(SETTLE_DELAY)
    return Connection(sock, log), device

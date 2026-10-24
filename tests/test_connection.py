"""Tests for sonyxm.connection, using a fake socket that plays the headphones."""

import time
import unittest
from unittest import mock

from tests.fakes import (
    ACK,
    BATTERY_REPLY,
    CMD,
    INIT_REPLY,
    FakeSocket,
    device_ack,
    device_frame,
    well_behaved,
)
from sonyxm import bluetooth, connection
from sonyxm.connection import Connection, ConnectionClosed, ResponseTimeout
from sonyxm.models import SERVICE_V1, SERVICE_V2
from sonyxm.protocol import (
    V1,
    V2,
    Command,
    ForbiddenCommandError,
    Frame,
    MessageType,
    _pack,
    battery_request,
    init_request,
)


def after_handshake(sock) -> Connection:
    """A connection to V2 headphones, as it is after the handshake."""
    conn = Connection(sock)
    conn.version = V2
    return conn


class HandshakeTest(unittest.TestCase):
    def test_handshake(self):
        sock = FakeSocket(well_behaved)
        conn = Connection(sock)
        self.assertEqual(conn.handshake(), "v2")
        self.assertEqual(sock.host_commands(), [Frame(CMD, 0, init_request())])
        # The init reply (device seq 0) must be acknowledged with seq 1
        self.assertEqual(sock.host_acks(), [Frame(ACK, 1)])

    def test_version_is_kept(self):
        conn = Connection(FakeSocket(well_behaved))
        self.assertIsNone(conn.version)
        conn.handshake()
        self.assertEqual(conn.version, V2)

    def test_only_init_before_the_handshake(self):
        sock = FakeSocket(well_behaved)
        with self.assertRaises(ForbiddenCommandError):
            Connection(sock).request(battery_request())
        self.assertEqual(sock.sent, [])

    def test_v1_headphones_get_v1_rules(self):
        def xm4(frame):
            if frame.type != CMD:
                return []
            return [device_ack(frame.seq), device_frame(0, bytes.fromhex("01 00 40 10"))]

        sock = FakeSocket(xm4)
        conn = Connection(sock)
        self.assertEqual(conn.handshake(), V1)
        # 22 00 asks a V2 headset for its battery but means "power off" to V1 headphones
        with self.assertRaises(ForbiddenCommandError):
            conn.request(battery_request())
        self.assertEqual(len(sock.host_commands()), 1)

    def test_sequence_follows_ack(self):
        sock = FakeSocket(well_behaved)
        conn = Connection(sock)
        conn.handshake()
        reply = conn.request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(reply.payload, BATTERY_REPLY)
        self.assertEqual([f.seq for f in sock.host_commands()], [0, 1])
        conn.request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual([f.seq for f in sock.host_commands()], [0, 1, 0])

    def test_init_is_resent_when_ignored(self):
        calls = []

        def ignore_first(frame):
            if frame.type == CMD:
                calls.append(frame)
                if len(calls) == 1:
                    return []
            return well_behaved(frame)

        sock = FakeSocket(ignore_first)
        conn = Connection(sock)
        reply = conn.request(
            init_request(), {Command.INIT_REPLY}, timeout=2, retries=2, retry_interval=0.05
        )
        self.assertEqual(reply.payload, INIT_REPLY)
        # Same frame, same sequence number, sent twice
        self.assertEqual(sock.host_commands(), [Frame(CMD, 0, init_request())] * 2)

    def test_gives_up_after_retries(self):
        sock = FakeSocket()
        conn = Connection(sock)
        with self.assertRaisesRegex(ResponseTimeout, "no ACK"):
            conn.request(init_request(), {Command.INIT_REPLY}, timeout=0.3, retries=2, retry_interval=0.05)
        self.assertEqual(len(sock.host_commands()), 3)


class OrderingTest(unittest.TestCase):
    def test_reply_before_ack(self):
        sock = FakeSocket(lambda f: [device_frame(0, BATTERY_REPLY), device_ack(f.seq)] if f.type == CMD else [])
        reply = after_handshake(sock).request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(reply.payload, BATTERY_REPLY)

    def test_ack_and_reply_in_one_chunk(self):
        sock = FakeSocket(lambda f: [device_ack(f.seq) + device_frame(1, BATTERY_REPLY)] if f.type == CMD else [])
        reply = after_handshake(sock).request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(reply.payload, BATTERY_REPLY)
        self.assertEqual(sock.host_acks(), [Frame(ACK, 0)])

    def test_bytes_arrive_one_at_a_time(self):
        def trickle(frame):
            if frame.type != CMD:
                return []
            data = device_ack(frame.seq) + device_frame(0, BATTERY_REPLY)
            return [bytes((b,)) for b in data]

        reply = after_handshake(FakeSocket(trickle)).request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(reply.payload, BATTERY_REPLY)

    def test_unrelated_notification_is_acked_and_skipped(self):
        notify = bytes.fromhex("69 15 01 01 00 00 14")

        def chatty(frame):
            if frame.type != CMD:
                return []
            return [device_frame(1, notify), device_ack(frame.seq), device_frame(0, BATTERY_REPLY)]

        sock = FakeSocket(chatty)
        reply = after_handshake(sock).request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(reply.payload, BATTERY_REPLY)
        self.assertEqual(sock.host_acks(), [Frame(ACK, 0), Frame(ACK, 1)])

    def test_command_2_frames_are_acked(self):
        def table2(frame):
            if frame.type != CMD:
                return []
            return [device_frame(0, bytes.fromhex("47 01 00"), MessageType.COMMAND_2), device_ack(frame.seq)]

        sock = FakeSocket(table2)
        after_handshake(sock).request(battery_request())
        self.assertEqual(sock.host_acks(), [Frame(ACK, 1)])

    def test_stale_ack_is_ignored(self):
        def stale_first(frame):
            if frame.type != CMD:
                return []
            # An ACK carrying the same seq we sent is stale; only 1 - seq counts
            return [_pack(ACK, frame.seq, b""), device_ack(frame.seq), device_frame(0, BATTERY_REPLY)]

        conn = after_handshake(FakeSocket(stale_first))
        conn.request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(conn._seq, 1)

    def test_garbage_and_bad_frames_are_skipped(self):
        bad_checksum = bytes.fromhex("3e 0c 00 00 00 00 04 23 00 4b 00 00 3c")

        def noisy(frame):
            if frame.type != CMD:
                return []
            return [b"\x00\xff", bad_checksum, device_ack(frame.seq), device_frame(0, BATTERY_REPLY)]

        reply = after_handshake(FakeSocket(noisy)).request(battery_request(), {Command.BATTERY_RET})
        self.assertEqual(reply.payload, BATTERY_REPLY)


class FailureTest(unittest.TestCase):
    def test_no_reply_after_ack(self):
        sock = FakeSocket(lambda f: [device_ack(f.seq)] if f.type == CMD else [])
        with self.assertRaisesRegex(ResponseTimeout, "no reply"):
            after_handshake(sock).request(battery_request(), {Command.BATTERY_RET}, timeout=0.1)

    def test_timeout_is_respected(self):
        start = time.monotonic()
        with self.assertRaises(ResponseTimeout):
            after_handshake(FakeSocket()).request(battery_request(), timeout=0.2)
        self.assertLess(time.monotonic() - start, 1.0)

    def test_reply_without_ack(self):
        sock = FakeSocket(lambda f: [device_frame(0, BATTERY_REPLY)] if f.type == CMD else [])
        conn = after_handshake(sock)
        reply = conn.request(battery_request(), {Command.BATTERY_RET}, timeout=0.1)
        self.assertEqual(reply.payload, BATTERY_REPLY)
        self.assertEqual(conn._seq, 1)

    def test_connection_closed(self):
        class Closing(FakeSocket):
            def recv(self, size):
                return b""

        with self.assertRaises(ConnectionClosed):
            after_handshake(Closing()).request(battery_request())

    def test_forbidden_payload_is_never_sent(self):
        sock = FakeSocket(well_behaved)
        with self.assertRaises(ForbiddenCommandError):
            after_handshake(sock).request(bytes.fromhex("f8 09 01"))
        self.assertEqual(sock.sent, [])

    def test_close(self):
        sock = FakeSocket()
        with Connection(sock):
            pass
        self.assertTrue(sock.closed)


class ListenTest(unittest.TestCase):
    def test_notifications_are_passed_on_and_acked(self):
        sock = FakeSocket()
        sock.incoming.extend([
            device_frame(0, bytes.fromhex("69 15 01 01 01 00 0a")),
            _pack(ACK, 1, b""),  # stray ACK: ignored, not passed on
            device_frame(1, bytes.fromhex("25 00 50 00")),
        ])
        received = []
        start = time.monotonic()
        Connection(sock).listen(received.append, duration=0.2)
        self.assertGreaterEqual(time.monotonic() - start, 0.2)
        self.assertEqual([f.payload.hex(" ") for f in received], ["69 15 01 01 01 00 0a", "25 00 50 00"])
        self.assertEqual(sock.host_acks(), [Frame(ACK, 1), Frame(ACK, 0)])

    def test_listener_is_not_used_by_requests(self):
        sock = FakeSocket(well_behaved)
        conn = Connection(sock)
        conn.listen(lambda frame: self.fail("nothing to hear"), duration=0.05)
        # Unrelated frames during a later request are ignored again, not sent to the old listener
        conn.handshake()


class ConnectTest(unittest.TestCase):
    """connect() picks the headphones and the control service their model offers."""

    def connect_with(self, name, offered):
        device = bluetooth.PairedDevice(name, "12:34:56:78:9A:BC", True, True)
        tried = []

        def open_rfcomm(address, timeout, log, service):
            tried.append(service)
            if service not in offered:
                raise bluetooth.connect_error(10108)
            return FakeSocket()

        with mock.patch.object(bluetooth, "list_paired_devices", lambda: [device]), \
                mock.patch.object(bluetooth, "open_rfcomm", open_rfcomm), \
                mock.patch.object(connection, "SETTLE_DELAY", 0):
            conn, picked = connection.connect(timeout=1)
        self.assertIs(picked, device)
        return tried

    def test_xm4_uses_v1_first(self):
        self.assertEqual(self.connect_with("WH-1000XM4", {SERVICE_V1}), [SERVICE_V1])

    def test_xm5_uses_v2(self):
        self.assertEqual(self.connect_with("WH-1000XM5", {SERVICE_V2}), [SERVICE_V2])

    def test_falls_back_to_the_other_service(self):
        self.assertEqual(self.connect_with("WH-1000XM4", {SERVICE_V2}), [SERVICE_V1, SERVICE_V2])
        self.assertEqual(self.connect_with("WH-1000XM6", {SERVICE_V1}), [SERVICE_V2, SERVICE_V1])

    def test_neither_service(self):
        with self.assertRaises(bluetooth.ServiceNotFoundError):
            self.connect_with("WH-1000XM5", set())


class DebugLogTest(unittest.TestCase):
    def test_raw_bytes_are_logged(self):
        lines = []
        conn = Connection(FakeSocket(well_behaved), log=lines.append)
        conn.handshake()
        text = "\n".join(lines)
        self.assertIn(">> 3e 0c 00 00 00 00 02 00 00 0e 3c", text)
        self.assertIn("<< 3e 01 01 00 00 00 00 02 3c", text)
        self.assertIn("[COMMAND_1 seq=0 payload=01 00 03 00 00 00 00 00]", text)


if __name__ == "__main__":
    unittest.main()

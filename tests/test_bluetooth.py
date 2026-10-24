"""Tests for the parts of sonyxm.bluetooth that do not need a Bluetooth adapter."""

import ctypes
import sys
import unittest

from sonyxm import bluetooth as bt
from sonyxm.bluetooth import BluetoothError, DeviceNotFoundError, PairedDevice
from sonyxm.models import SERVICE_V2


def device(name, address, connected=False):
    return PairedDevice(name=name, address=address, connected=connected, authenticated=True)


class AddressTest(unittest.TestCase):
    def test_format(self):
        self.assertEqual(bt.format_address(0x123456789ABC), "12:34:56:78:9A:BC")
        self.assertEqual(bt.format_address(0x01), "00:00:00:00:00:01")

    def test_parse(self):
        for text in ("12:34:56:78:9A:BC", "12-34-56-78-9a-bc", "123456789abc", " 12:34:56:78:9A:BC "):
            with self.subTest(text=text):
                self.assertEqual(bt.parse_address(text), 0x123456789ABC)

    def test_parse_rejects_bad_input(self):
        for text in ("", "12:34:56:78:9A", "12:34:56:78:9A:BC:00", "zz:34:56:78:9A:BC"):
            with self.subTest(text=text), self.assertRaises(BluetoothError):
                bt.parse_address(text)


class PickTest(unittest.TestCase):
    XM5 = device("WH-1000XM5", "12:34:56:78:9A:BC", connected=True)
    MOUSE = device("MX Master 3", "11:22:33:44:55:66")

    def test_by_name(self):
        self.assertEqual(bt.pick_headphones([self.MOUSE, self.XM5]), self.XM5)

    def test_name_match_ignores_case(self):
        renamed = device("My wh-1000xm5", "12:34:56:78:9A:BC")
        self.assertEqual(bt.pick_headphones([renamed]), renamed)

    def test_none_paired(self):
        with self.assertRaisesRegex(DeviceNotFoundError, "MX Master 3"):
            bt.pick_headphones([self.MOUSE])
        with self.assertRaisesRegex(DeviceNotFoundError, "none"):
            bt.pick_headphones([])

    def test_several_prefers_the_connected_one(self):
        other = device("WH-1000XM5", "AA:BB:CC:DD:EE:FF")
        self.assertEqual(bt.pick_headphones([other, self.XM5]), self.XM5)

    def test_several_ambiguous(self):
        a = device("WH-1000XM5", "AA:BB:CC:DD:EE:FF")
        b = device("WH-1000XM5", "AA:BB:CC:DD:EE:00")
        with self.assertRaisesRegex(DeviceNotFoundError, "--address"):
            bt.pick_headphones([a, b])

    def test_by_address(self):
        self.assertEqual(bt.pick_headphones([self.XM5, self.MOUSE], "11-22-33-44-55-66"), self.MOUSE)
        self.assertEqual(bt.pick_headphones([self.XM5], "123456789abc"), self.XM5)

    def test_address_not_paired(self):
        with self.assertRaises(DeviceNotFoundError):
            bt.pick_headphones([self.XM5], "AA:BB:CC:DD:EE:FF")

    def test_any_supported_model(self):
        for name in ("WH-1000XM4", "WH-1000XM6"):
            with self.subTest(name=name):
                other = device(name, "AA:BB:CC:DD:EE:FF")
                self.assertEqual(bt.pick_headphones([self.MOUSE, other]), other)
        # Sony earbuds with a similar name are not ours
        self.assertRaises(DeviceNotFoundError, bt.pick_headphones, [device("WF-1000XM5", "AA:BB:CC:DD:EE:FF")])

    def test_the_connected_model_wins(self):
        xm4 = device("WH-1000XM4", "AA:BB:CC:DD:EE:FF")
        xm6 = device("WH-1000XM6", "AA:BB:CC:DD:EE:00", connected=True)
        self.assertEqual(bt.pick_headphones([xm4, xm6]), xm6)
        with self.assertRaisesRegex(bt.SeveralDevicesError, "WH-1000XM4.*WH-1000XM6"):
            bt.pick_headphones([xm4, device("WH-1000XM6", "AA:BB:CC:DD:EE:00")])


class ConnectErrorTest(unittest.TestCase):
    def test_channel_in_use(self):
        error = bt.connect_error(10048)
        self.assertIsInstance(error, bt.ChannelInUseError)
        self.assertIn("already connected", str(error))

    def test_service_not_found(self):
        self.assertIsInstance(bt.connect_error(10108), bt.ServiceNotFoundError)

    def test_other_errors(self):
        error = bt.connect_error(10060)
        self.assertNotIsInstance(error, bt.ChannelInUseError)
        self.assertIsInstance(error, BluetoothError)
        self.assertIn("did not answer", str(error))


@unittest.skipUnless(sys.platform == "win32", "Windows structures")
class StructLayoutTest(unittest.TestCase):
    """Sizes and offsets must match the Windows SDK headers exactly."""

    def test_sockaddr_bth(self):
        self.assertEqual(ctypes.sizeof(bt._SOCKADDR_BTH), 30)  # packed: 2 + 8 + 16 + 4
        self.assertEqual(bt._SOCKADDR_BTH.btAddr.offset, 2)
        self.assertEqual(bt._SOCKADDR_BTH.serviceClassId.offset, 10)
        self.assertEqual(bt._SOCKADDR_BTH.port.offset, 26)

    def test_sockaddr_bth_contents(self):
        raw = bytes(bt._sockaddr_bth(0x123456789ABC, SERVICE_V2))
        self.assertEqual(raw[0:2], bytes.fromhex("20 00"))  # AF_BTH = 32, little-endian
        self.assertEqual(raw[2:10], bytes.fromhex("bc 9a 78 56 34 12 00 00"))
        # GUID 956C7B26-D49A-4BA8-B03F-B17D393CB6E2 in its in-memory layout
        self.assertEqual(raw[10:26], bytes.fromhex("26 7b 6c 95 9a d4 a8 4b b0 3f b1 7d 39 3c b6 e2"))
        self.assertEqual(raw[26:30], bytes(4))

    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout")
    def test_device_info_64bit(self):
        self.assertEqual(ctypes.sizeof(bt._BLUETOOTH_DEVICE_INFO), 560)
        self.assertEqual(bt._BLUETOOTH_DEVICE_INFO.Address.offset, 8)
        self.assertEqual(bt._BLUETOOTH_DEVICE_INFO.stLastSeen.offset, 32)
        self.assertEqual(bt._BLUETOOTH_DEVICE_INFO.szName.offset, 64)
        self.assertEqual(ctypes.sizeof(bt._BLUETOOTH_DEVICE_SEARCH_PARAMS), 40)
        self.assertEqual(bt._BLUETOOTH_DEVICE_SEARCH_PARAMS.hRadio.offset, 32)


if __name__ == "__main__":
    unittest.main()

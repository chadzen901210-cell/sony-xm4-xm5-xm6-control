"""Tests for the parts of sonyxm.headtracker that need no sensor: orientation math and HID value decoding."""

import ctypes
import math
import sys
import unittest

from sonyxm import headtracker as ht
from sonyxm.headtracker import Angles, Orientation, Sample


def axis_angle(axis, degrees):
    """Rotation vector for a rotation of `degrees` about a unit axis."""
    r = math.radians(degrees)
    return tuple(a * r for a in axis)


def rotation(yaw=0.0, pitch=0.0, roll=0.0):
    """Quaternion for yaw about Z, then pitch about X, then roll about Y."""
    q = ht.quaternion_from_rotation_vector(*axis_angle((0, 0, 1), yaw))
    q = ht.quaternion_multiply(q, ht.quaternion_from_rotation_vector(*axis_angle((1, 0, 0), pitch)))
    return ht.quaternion_multiply(q, ht.quaternion_from_rotation_vector(*axis_angle((0, 1, 0), roll)))


def rotation_vector(q):
    """Inverse of quaternion_from_rotation_vector, to feed samples built from angles."""
    w, x, y, z = q
    angle = 2 * math.acos(max(-1.0, min(1.0, w)))
    s = math.sqrt(max(0.0, 1 - w * w))
    if s < 1e-12:
        return (0.0, 0.0, 0.0)
    return (x / s * angle, y / s * angle, z / s * angle)


class QuaternionTest(unittest.TestCase):
    def assertAngles(self, angles, yaw, pitch, roll):
        self.assertAlmostEqual(angles.yaw, yaw, places=6)
        self.assertAlmostEqual(angles.pitch, pitch, places=6)
        self.assertAlmostEqual(angles.roll, roll, places=6)

    def test_zero_rotation(self):
        self.assertEqual(ht.quaternion_from_rotation_vector(0, 0, 0), ht.IDENTITY)
        self.assertAngles(ht.angles_from_quaternion(ht.IDENTITY), 0, 0, 0)

    def test_quarter_turn(self):
        w, x, y, z = ht.quaternion_from_rotation_vector(0, 0, math.pi / 2)
        self.assertAlmostEqual(w, math.cos(math.pi / 4))
        self.assertAlmostEqual(z, math.sin(math.pi / 4))
        self.assertEqual((x, y), (0, 0))

    def test_single_axes(self):
        # Head axes: X = left ear to right, Y = back to nose, Z = up
        self.assertAngles(ht.angles_from_quaternion(rotation(yaw=30)), 30, 0, 0)  # turned left
        self.assertAngles(ht.angles_from_quaternion(rotation(pitch=20)), 0, 20, 0)  # looking up
        self.assertAngles(ht.angles_from_quaternion(rotation(roll=15)), 0, 0, 15)  # tilted right

    def test_combined(self):
        for yaw, pitch, roll in ((40, -25, 10), (-120, 60, -30), (170, 5, 80)):
            with self.subTest(yaw=yaw, pitch=pitch, roll=roll):
                self.assertAngles(ht.angles_from_quaternion(rotation(yaw, pitch, roll)), yaw, pitch, roll)

    def test_multiply_by_conjugate_is_identity(self):
        q = rotation(33, -12, 7)
        result = ht.quaternion_multiply(ht.quaternion_conjugate(q), q)
        for got, want in zip(result, ht.IDENTITY):
            self.assertAlmostEqual(got, want)


class OrientationTest(unittest.TestCase):
    def sample(self, yaw=0.0, pitch=0.0, roll=0.0, counter=0):
        return Sample(rotation_vector(rotation(yaw, pitch, roll)), None, counter)

    def test_first_sample_is_straight_ahead(self):
        orientation = Orientation()
        angles = orientation.update(self.sample(yaw=50, pitch=10))
        self.assertAlmostEqual(angles.yaw, 0, places=6)
        self.assertAlmostEqual(angles.pitch, 0, places=6)

    def test_relative_to_reference(self):
        orientation = Orientation()
        orientation.update(self.sample(yaw=50))
        angles = orientation.update(self.sample(yaw=80))
        self.assertAlmostEqual(angles.yaw, 30, places=6)

    def test_recenter(self):
        orientation = Orientation()
        orientation.update(self.sample(yaw=0))
        orientation.update(self.sample(yaw=-45))
        orientation.recenter()
        self.assertAlmostEqual(orientation.angles().yaw, 0, places=6)
        self.assertAlmostEqual(orientation.update(self.sample(yaw=-35)).yaw, 10, places=6)

    def test_recenters_once_the_filter_has_settled(self):
        orientation = Orientation(settle_samples=5)
        # Startup drift like the real XM5: roll creeps away while the head is still
        for i in range(4):
            orientation.update(self.sample(roll=-2.5 * i))
        self.assertFalse(orientation.settled)
        self.assertAlmostEqual(orientation.update(self.sample(roll=-10)).roll, 0, places=6)
        self.assertTrue(orientation.settled)
        # After settling, real movement is measured from the settled pose
        self.assertAlmostEqual(orientation.update(self.sample(roll=-4)).roll, 6, places=6)

    def test_settles_again_after_a_reset(self):
        orientation = Orientation(settle_samples=3)
        for _ in range(3):
            orientation.update(self.sample(counter=1))
        self.assertTrue(orientation.settled)
        orientation.update(self.sample(yaw=50, counter=2))
        self.assertFalse(orientation.settled)

    def test_reference_follows_a_reset_of_the_headphones(self):
        orientation = Orientation()
        orientation.update(self.sample(yaw=0, counter=1))
        orientation.update(self.sample(yaw=20, counter=1))
        # The headphones restarted their filter: their frame jumped, so start over
        self.assertAlmostEqual(orientation.update(self.sample(yaw=90, counter=2)).yaw, 0, places=6)


class DecodingTest(unittest.TestCase):
    def test_unit_exponent(self):
        self.assertEqual(ht.unit_exponent(0x08), -8)
        self.assertEqual(ht.unit_exponent(0x0D), -3)
        self.assertEqual(ht.unit_exponent(0x00), 0)
        self.assertEqual(ht.unit_exponent(0x07), 7)

    def test_unpack_int16(self):
        data = (-1).to_bytes(2, "little", signed=True) + (32767).to_bytes(2, "little") + \
            (-32767).to_bytes(2, "little", signed=True)
        self.assertEqual(ht.unpack_values(data, 16, 3, signed=True), [-1, 32767, -32767])
        self.assertEqual(ht.unpack_values(data, 16, 3, signed=False), [65535, 32767, 32769])

    def test_unpack_sub_byte_fields(self):
        # Three 4-bit fields packed LSB first: 0x1, 0x2, 0xF
        self.assertEqual(ht.unpack_values(bytes([0x21, 0x0F]), 4, 3, signed=False), [1, 2, 15])
        self.assertEqual(ht.unpack_values(bytes([0x21, 0x0F]), 4, 3, signed=True), [1, 2, -1])

    def test_rotation_scaling_from_the_android_descriptor(self):
        # Logical ±32767 maps to physical ±314159265 × 10^-8, i.e. ±π rad
        def physical(value):
            return ht.to_physical(value, -32767, 32767, -314159265, 314159265, -8)

        self.assertAlmostEqual(physical(32767), math.pi, places=6)
        self.assertAlmostEqual(physical(-32767), -math.pi, places=6)
        self.assertAlmostEqual(physical(0), 0.0, places=9)

    def test_angular_velocity_scaling(self):
        self.assertAlmostEqual(ht.to_physical(32767, -32767, 32767, -32, 32, 0), 32.0)

    def test_no_physical_range_means_logical(self):
        self.assertEqual(ht.to_physical(17, 0, 255, 0, 0, 0), 17.0)


@unittest.skipUnless(sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit Windows layout")
class StructLayoutTest(unittest.TestCase):
    """Sizes and offsets must match hidpi.h, hidsdi.h and setupapi.h."""

    def test_sizes(self):
        self.assertEqual(ctypes.sizeof(ht._HIDP_CAPS), 64)
        self.assertEqual(ctypes.sizeof(ht._HIDP_VALUE_CAPS), 72)
        self.assertEqual(ctypes.sizeof(ht._HIDP_BUTTON_CAPS), 72)
        self.assertEqual(ctypes.sizeof(ht._SP_DEVICE_INTERFACE_DATA), 32)
        self.assertEqual(ctypes.sizeof(ht._SP_DEVINFO_DATA), 32)
        self.assertEqual(ctypes.sizeof(ht._OVERLAPPED), 32)

    def test_value_caps_offsets(self):
        self.assertEqual(ht._HIDP_VALUE_CAPS.BitSize.offset, 18)
        self.assertEqual(ht._HIDP_VALUE_CAPS.ReportCount.offset, 20)
        self.assertEqual(ht._HIDP_VALUE_CAPS.UnitsExp.offset, 32)
        self.assertEqual(ht._HIDP_VALUE_CAPS.LogicalMin.offset, 40)
        self.assertEqual(ht._HIDP_VALUE_CAPS.PhysicalMax.offset, 52)
        self.assertEqual(ht._HIDP_VALUE_CAPS.Range.offset, 56)
        self.assertEqual(ht._HIDP_BUTTON_CAPS.Range.offset, 56)

    def test_guid_from_uuid(self):
        g = ht._GUID.from_uuid("00001124-0000-1000-8000-00805F9B34FB")
        self.assertEqual((g.Data1, g.Data2, g.Data3), (0x1124, 0, 0x1000))
        self.assertEqual(bytes(g.Data4), bytes.fromhex("800000805f9b34fb"))


if __name__ == "__main__":
    unittest.main()

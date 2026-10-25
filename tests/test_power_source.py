"""Plugged in or not (core/power_source.py), the question the wake listener and sleep mode ask
every few seconds. Windows is faked except in one read-only test of the real reading."""
import ctypes
import unittest
from unittest import mock

from aj.core import power_source as ps


def windows_says(ac_line, battery_flag, ok=True):
    """A stand-in for kernel32 whose GetSystemPowerStatus fills in the given reading."""
    def call(ref):
        ref._obj.ACLineStatus = ac_line
        ref._obj.BatteryFlag = battery_flag
        return 1 if ok else 0
    fake = mock.Mock()
    fake.GetSystemPowerStatus.side_effect = call
    return mock.patch.object(ps, "_kernel32", fake)


class PowerSourceTests(unittest.TestCase):
    def test_plugged_in_or_on_battery_as_windows_says(self):
        self.assertIs(ps.ac_from(1, 8), True)    # plugged in, charging
        self.assertIs(ps.ac_from(1, 1), True)    # plugged in, full
        self.assertIs(ps.ac_from(0, 1), False)   # on battery
        self.assertIs(ps.ac_from(0, 4), False)   # on battery, critical

    def test_no_battery_at_all_counts_as_plugged_in(self):
        # A desktop, or the laptop with its battery out: the mains is all it can run on.
        self.assertIs(ps.ac_from(255, 128), True)
        self.assertIs(ps.ac_from(1, 128), True)

    def test_unknown_is_none_so_callers_stay_on_the_safe_side(self):
        self.assertIsNone(ps.ac_from(255, 1))
        self.assertIsNone(ps.ac_from(255, 255))  # 255 is "unknown", even though bit 128 is set in it

    def test_the_reading_goes_through_the_struct(self):
        with windows_says(0, 128):
            self.assertIs(ps.on_ac_power(), False)  # Windows saying "battery" wins over the flag
        with windows_says(1, 8):
            self.assertIs(ps.on_ac_power(), True)
        with windows_says(255, 128):
            self.assertIs(ps.on_ac_power(), True)

    def test_a_failed_call_is_none_never_an_exception(self):
        with windows_says(1, 1, ok=False):
            self.assertIsNone(ps.on_ac_power())
        with mock.patch.object(ps, "_read", side_effect=OSError("no")):
            self.assertIsNone(ps.on_ac_power())

    def test_the_struct_is_the_size_windows_expects(self):
        self.assertEqual(ctypes.sizeof(ps._SystemPowerStatus), 12)

    def test_the_real_reading_is_an_answer(self):
        self.assertIn(ps.on_ac_power(), (True, False, None))  # reads the laptop, changes nothing


if __name__ == "__main__":
    unittest.main()

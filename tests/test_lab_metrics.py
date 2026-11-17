"""The lab window's CPU and GPU meters (ui/lab_metrics.py). The CPU figure is checked against real
work and a real sleep. The GPU side's instance names are parsed as plain strings, PDH's array is
faked to check the buffer sizing and the struct layout, and one test reads the real counter on
this laptop (skipped where Windows has no GPU Engine counters)."""
import ctypes
import time
import unittest
from unittest import mock

from aj.ui import lab_metrics as lm
from aj.ui.lab_metrics import ProcessMeters, adapter_of, by_adapter

AMD = "luid_0x00000000_0x00010DF0"
NVIDIA = "luid_0x00000000_0x0A41AD5A"


def engine(adapter: str, eng: int = 0, pid: int = 1234) -> str:
    return f"pid_{pid}_{adapter}_phys_0_eng_{eng}_engtype_3D"


def burn(seconds: float) -> None:
    """Pure-Python work on this thread for this long: one core, busy."""
    end = time.perf_counter() + seconds
    x = 0
    while time.perf_counter() < end:
        x = (x * 31 + 7) % 1_000_003


def pdh_with(readings, collect_status=0, array_status=0):
    """A stand-in for pdh.dll whose counter array holds these (name, percent, CStatus) readings,
    laid out as PDH lays them out: the items, then their names, in the caller's buffer. Like PDH it
    answers PDH_MORE_DATA, with the size it needs, when the buffer is missing or too small."""
    item_size = ctypes.sizeof(lm._FmtCounterValueItem)
    names = [ctypes.create_unicode_buffer(name) for name, _, _ in readings]
    need = item_size * len(readings) + sum(ctypes.sizeof(n) for n in names)
    calls = []

    def get_array(counter, fmt, size_ref, count_ref, buffer):
        calls.append(size_ref._obj.value)
        count_ref._obj.value = len(readings)
        if array_status:
            return array_status
        if buffer is None or size_ref._obj.value < need:
            size_ref._obj.value = need
            return lm._PDH_MORE_DATA
        items = (lm._FmtCounterValueItem * len(readings)).from_buffer(buffer)
        offset = item_size * len(readings)
        for i, ((_, percent, status), name) in enumerate(zip(readings, names)):
            ctypes.memmove(ctypes.addressof(buffer) + offset, name, ctypes.sizeof(name))
            ctypes.c_void_p.from_buffer(buffer, i * item_size).value = ctypes.addressof(buffer) + offset
            items[i].FmtValue.CStatus = status
            items[i].FmtValue.doubleValue = percent
            offset += ctypes.sizeof(name)
        return 0

    fake = mock.Mock()
    fake.PdhCollectQueryData.return_value = collect_status
    fake.PdhGetFormattedCounterArrayW.side_effect = get_array
    fake.calls = calls
    return fake


def meters_on(fake):
    """ProcessMeters reading the fake: no real query is opened, and the handles are stand-ins."""
    with mock.patch.object(ProcessMeters, "_open_gpu"):
        meters = ProcessMeters()
    meters._opener.join()
    meters._handles = ("query", "counter")
    return meters, mock.patch.object(lm, "_pdh", fake)


class CpuTests(unittest.TestCase):
    def setUp(self):
        self.meters = ProcessMeters()
        self.meters.wait_for_gpu()  # the opening thread's own CPU (~0.2 s) must not land in a reading

    def tearDown(self):
        self.meters.close()

    def test_the_first_call_is_zero(self):
        self.assertEqual(self.meters.cpu_percent(), 0.0)

    def test_one_busy_core_reads_about_100(self):
        self.meters.cpu_percent()
        burn(0.3)
        self.assertTrue(60 <= self.meters.cpu_percent() <= 120)

    def test_sleeping_reads_about_0(self):
        self.meters.cpu_percent()
        time.sleep(0.4)
        self.assertLess(self.meters.cpu_percent(), 10)


class InstanceNameTests(unittest.TestCase):
    def test_the_adapter_is_the_luid_part(self):
        self.assertEqual(adapter_of("pid_1234_luid_0x00000000_0x0000D1A3_phys_0_eng_0_engtype_3D"),
                         "luid_0x00000000_0x0000D1A3")
        # PowerShell's Get-Counter writes the same name in small letters: the case is kept.
        self.assertEqual(adapter_of("pid_9_luid_0x00000000_0x00010df0_phys_0_eng_1_engtype_3d"),
                         "luid_0x00000000_0x00010df0")

    def test_a_name_without_a_luid_has_no_adapter(self):
        for name in ("", "_Total", "pid_1234_phys_0_eng_0_engtype_3D", "pid_1234_luid_0x_0x1_phys_0"):
            with self.subTest(name=name):
                self.assertIsNone(adapter_of(name))

    def test_engines_are_summed_per_adapter(self):
        readings = [(engine(AMD, 0), 30.0), (engine(AMD, 1), 12.5), (engine(NVIDIA, 0), 4.0),
                    ("not an engine", 99.0)]
        self.assertEqual(by_adapter(readings), {AMD: 42.5, NVIDIA: 4.0})
        self.assertEqual(by_adapter([]), {})


class GpuArrayTests(unittest.TestCase):
    """PDH's counter array, faked: the parts the real machine doesn't reliably show."""

    def test_the_structs_have_the_x64_layout(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("32-bit Python")
        self.assertEqual(ctypes.sizeof(lm._FmtCounterValue), 16)
        self.assertEqual(lm._FmtCounterValue.value.offset, 8)  # after the DWORD and 4 bytes of padding
        self.assertEqual(ctypes.sizeof(lm._FmtCounterValueItem), 24)
        self.assertEqual(lm._FmtCounterValueItem.FmtValue.offset, 8)

    def test_the_buffer_is_sized_by_pdh_then_kept(self):
        fake = pdh_with([(engine(AMD, 0), 30.0, 0), (engine(AMD, 1), 10.0, 1), (engine(NVIDIA), 2.5, 0)])
        meters, using = meters_on(fake)
        with using:
            self.assertEqual(meters.gpu_by_adapter(), {AMD: 40.0, NVIDIA: 2.5})
            self.assertEqual(fake.calls[0], 0)  # the first call has no buffer and asks the size
            self.assertEqual(len(fake.calls), 2)
            meters._sampled_at = float("-inf")  # as if a second had passed
            self.assertAlmostEqual(meters.gpu_percent(), 42.5)
            self.assertEqual(len(fake.calls), 3)  # the kept buffer was big enough: one call

    def test_readings_close_together_share_one_collection(self):
        fake = pdh_with([(engine(AMD), 30.0, 0)])
        meters, using = meters_on(fake)
        with using:
            self.assertEqual(meters.gpu_percent(), 30.0)
            self.assertEqual(meters.gpu_by_adapter(), {AMD: 30.0})
        self.assertEqual(fake.PdhCollectQueryData.call_count, 1)

    def test_engines_with_no_figure_yet_are_left_out(self):
        invalid = 0xC0000BBA  # PDH_CSTATUS_INVALID_DATA: an engine seen in one collection only
        meters, using = meters_on(pdh_with([(engine(AMD), 30.0, 0), (engine(NVIDIA), 0.0, invalid)]))
        with using:
            self.assertEqual(meters.gpu_by_adapter(), {AMD: 30.0})
        meters, using = meters_on(pdh_with([(engine(AMD), 0.0, invalid)]))
        with using:
            self.assertIsNone(meters.gpu_percent())
            self.assertEqual(meters.gpu_by_adapter(), {})

    def test_no_engines_at_all_is_zero(self):
        meters, using = meters_on(pdh_with([], collect_status=lm._PDH_NO_DATA))
        with using:
            self.assertEqual(meters.gpu_percent(), 0.0)
            self.assertIsInstance(meters.gpu_percent(), float)
            self.assertEqual(meters.gpu_by_adapter(), {})

    def test_the_first_collection_is_none(self):
        meters, using = meters_on(pdh_with([], array_status=0xC0000BBA))
        with using:
            self.assertIsNone(meters.gpu_percent())

    def test_a_failing_pdh_is_none_never_an_exception(self):
        fake = pdh_with([(engine(AMD), 30.0, 0)])
        fake.PdhCollectQueryData.side_effect = OSError("access violation")
        meters, using = meters_on(fake)
        with using:
            self.assertIsNone(meters.gpu_percent())
            self.assertEqual(meters.gpu_by_adapter(), {})

    def test_before_the_query_is_open_and_after_close_there_is_no_figure(self):
        meters, using = meters_on(pdh_with([(engine(AMD), 30.0, 0)]))
        meters._handles = None
        with using:
            self.assertIsNone(meters.gpu_percent())
            meters._handles = ("query", "counter")
            meters.close()
            meters.close()
            self.assertIsNone(meters.gpu_percent())
            self.assertEqual(meters.gpu_by_adapter(), {})


class RealCounterTests(unittest.TestCase):
    def test_the_real_counter_reads_as_a_number(self):
        meters = ProcessMeters()
        try:
            if not meters.wait_for_gpu(10):
                self.skipTest("PDH has no GPU Engine counters here")
            time.sleep(0.3)
            start = time.perf_counter()
            value = meters.gpu_percent()
            cost = time.perf_counter() - start
            # This process draws nothing, so it has no engines and reads 0.0; a float all the same.
            self.assertIsInstance(value, float)
            self.assertGreaterEqual(value, 0.0)
            self.assertIsInstance(meters.gpu_by_adapter(), dict)
            self.assertLess(cost, 0.025)  # ~0.2 ms measured; this only catches a gross regression
        finally:
            meters.close()
        self.assertIsNone(meters.gpu_percent())


if __name__ == "__main__":
    unittest.main()

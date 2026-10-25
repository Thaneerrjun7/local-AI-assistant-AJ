"""calculate(): exact sums, nothing but arithmetic evaluated."""
import unittest

from aj.tools import calc, intents, registry
from aj.tools.registry import Level, ToolError


class CalcTests(unittest.TestCase):
    def test_exact_answers(self):
        self.assertEqual(calc.calculate("17 * 23"), "It's 391.")  # the model said 171 (2026-10-21)
        self.assertEqual(calc.calculate("15 / 100 * 80"), "It's 12.")
        self.assertEqual(calc.calculate("sqrt(144)"), "It's 12.")
        self.assertEqual(calc.calculate("100 / 7"), "It's 14.2857.")
        self.assertEqual(calc.calculate("1,000 * 1,000"), "It's 1,000,000.")
        self.assertEqual(registry.get("calculate").level, Level.SAFE)

    def test_only_arithmetic_runs(self):
        for bad in ['__import__("os")', 'open("x")', "x + 1", "2 ** 10000", "1 / 0", "sqrt(-1)", "[1, 2]"]:
            with self.assertRaises(ToolError, msg=bad):
                calc.calculate(bad)

    def test_spoken_sums(self):
        for said, expr in [("what's 17 times 23", "17 * 23"), ("what is 15 percent of 80", "15 /100* 80"),
                           ("square root of 144", "sqrt(144)"), ("2 to the power of 10", "2 ** 10"),
                           ("100 divided by 7", "100 / 7")]:
            self.assertEqual(intents.match(said), ("calculate", {"expression": expr}), said)
        for said in ["open spotify", "volume 30", "what time is it", "whats 5", "set a timer for 5 minutes"]:
            self.assertNotEqual((intents.match(said) or ("",))[0], "calculate", said)


class ReminderWithoutATaskTests(unittest.TestCase):
    def test_remind_me_at_a_time_alone_is_left_to_the_model(self):
        # It set a reminder called "at" (found 2026-10-21); the model asks what about instead.
        self.assertIsNone(intents.match("remind me at 5:30"))
        self.assertEqual(intents.match("remind me at 5:30 to call mom")[1]["text"], "call mom")


class SpelledNumberTests(unittest.TestCase):
    def test_numbers_written_as_words_are_checked_too(self):
        from aj.core.delivery import Delivery, numbers_in
        self.assertEqual(numbers_in("eighty percent, seventeen, one hundred and five, twenty-eight, 42"),
                         {"80", "17", "105", "28", "42"})
        d = Delivery(react=False)
        d.start_turn("how is my battery?")
        d.ground("Battery at 80%, plugged in.", fact=True)
        self.assertEqual(d.shape("Battery eighty percent."), "Battery eighty percent.")
        self.assertIsNone(d.shape("It was at seventeen percent."))


if __name__ == "__main__":
    unittest.main()

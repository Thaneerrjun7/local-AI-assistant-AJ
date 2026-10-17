"""The per-turn length rule: one sentence by default, a real explanation when asked."""
import unittest

from ultron import persona


class ReminderTests(unittest.TestCase):
    def test_explanation_requests(self):
        for said in ["explain how a CPU works", "why is the sky blue?", "how does inflation happen",
                     "tell me more about black holes", "what's the difference between RAM and storage",
                     "can you walk me through it"]:
            self.assertEqual(persona.reminder_for(said), persona.EXPLAIN_REMINDER, said)

    def test_everything_else_is_short(self):
        for said in ["hey, how are you doing?", "how's it going", "what's the capital of Australia?",
                     "tell me a joke", "open spotify", "thanks", "how many days are in a leap year?"]:
            self.assertEqual(persona.reminder_for(said), persona.SHORT_REMINDER, said)


if __name__ == "__main__":
    unittest.main()

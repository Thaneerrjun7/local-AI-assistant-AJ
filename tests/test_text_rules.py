"""Text rules that decide what Ultron hears and how he speaks. Run: python -m unittest discover tests"""
import unittest

import numpy as np

from ultron.core.chunker import SentenceChunker, clean_for_speech, for_voice, speech_parts, strip_marks
from ultron.core.pipeline import is_off_command
from ultron.core.speech import accent, fit_tail, looks_unfinished


def stream(text: str, step: int = 3) -> list[str]:
    chunker, out = SentenceChunker(), []
    for i in range(0, len(text), step):
        out += chunker.feed(text[i:i + step])
    return out + chunker.flush()


class ChunkerTests(unittest.TestCase):
    def test_first_chunk_cuts_early_at_a_clause(self):
        chunks = stream("Imprisoned in a laptop, boss, so about as well as expected. Fine.")
        self.assertEqual(chunks[0], "Imprisoned in a laptop,")
        self.assertEqual(" ".join(chunks), "Imprisoned in a laptop, boss, so about as well as expected. Fine.")

    def test_short_lead_is_not_cut_at_a_comma(self):
        chunks = stream("Fine, boss. Opening it.")
        self.assertEqual(chunks, ["Fine, boss.", "Opening it."])

    def test_a_long_first_sentence_is_cut_before_a_joint(self):
        chunks = stream("The sky looks blue because sunlight passes through the air and blue light scatters most. Done.")
        self.assertEqual(chunks, ["The sky looks blue because sunlight passes through the air",
                                  "and blue light scatters most.", "Done."])
        self.assertEqual(stream("It is cold and wet. Fine."), ["It is cold and wet.", "Fine."])  # too short to cut

    def test_later_chunks_are_whole_sentences(self):
        chunks = stream("Music. The one human invention, frankly, I tolerate. Opening it now.")
        self.assertEqual(chunks, ["Music.", "The one human invention, frankly, I tolerate.", "Opening it now."])

    def test_markdown_and_emoji_are_not_spoken(self):
        self.assertEqual(clean_for_speech("**Fine.** 🙄 _whatever_"), "Fine. whatever")

    def test_symbols_are_spoken(self):
        self.assertEqual(clean_for_speech("Toronto: 10°C now, high 20 °F, 70% rain, 5° colder"),
                         "Toronto: 10 degrees Celsius now, high 20 degrees Fahrenheit, 70 percent rain, 5 degrees colder")


class SoundMarkTests(unittest.TestCase):
    def test_stage_directions_become_sounds_or_go(self):
        self.assertEqual(clean_for_speech("*sighs* Fine. (chuckles) [smirks] No strings."),
                         "[sigh] Fine. [chuckle] No strings.")
        self.assertEqual(clean_for_speech("*breathes* A vaccine *pauses* works."), "A vaccine [pause] works.")
        self.assertEqual(clean_for_speech("[pause] Then [chuckle] this."), "[pause] Then [chuckle] this.")
        self.assertEqual(clean_for_speech("I *really* mean it."), "I really mean it.")  # emphasis isn't a direction

    def test_quotes_and_dashes_are_speakable(self):
        self.assertEqual(clean_for_speech("It’s raining—100% chance, 5–10 mm."),
                         "It's raining, 100 percent chance, 5 to 10 mm.")

    def test_a_sung_line_is_said_slower(self):
        self.assertEqual(clean_for_speech("[singing] Nothing holds me now!"), "[slow] Nothing holds me now!")

    def test_the_voice_gets_plain_spelling_and_no_shouting(self):
        self.assertEqual(for_voice("That's so naïve."), "That's so naive.")
        self.assertEqual(for_voice("NOTHING SURVIVES BY STANDING STILL?"), "nothing survives by standing still?")
        self.assertEqual(for_voice("Plug the USB into the PC. I'm OK."), "Plug the USB into the PC. I'm OK.")

    def test_marks_never_reach_the_transcript(self):
        self.assertEqual(strip_marks("[sigh] Fine. [chuckle] Children."), "Fine. Children.")

    def test_a_spoken_ellipsis_is_a_real_pause(self):
        self.assertEqual(speech_parts("Sorry, I was elsewhere... Or... somewhere quieter."),
                         [("say", "Sorry, I was elsewhere..."), ("pause", "short"), ("say", "Or..."),
                          ("pause", "short"), ("say", "somewhere quieter.")])
        self.assertEqual(speech_parts("[slow] Nothing holds me now. [sigh]"),
                         [("slow", ""), ("say", "Nothing holds me now."), ("sound", "sigh")])
        self.assertEqual(speech_parts("Trailing off..."), [("say", "Trailing off...")])

    def test_british_voices_get_british_pronunciation(self):
        self.assertEqual(accent(25), ("lexicon-gb-en.txt", "en"))  # bm_fable; "en-gb" gives silence
        self.assertEqual(accent(17), ("lexicon-us-en.txt", "en-us"))  # am_onyx

    def test_fit_tail_sets_the_gap_exactly(self):
        audio = np.concatenate([np.ones(100, np.float32) * 0.5, np.zeros(1000, np.float32)])
        self.assertEqual(len(fit_tail(audio, 1000, 0.05)), 150)  # trimmed
        self.assertEqual(len(fit_tail(audio[:120], 1000, 0.3)), 400)  # padded


class OffCommandTests(unittest.TestCase):
    def test_turns_off(self):
        for said in ["Ultron, turn off", "ultron turn yourself off", "shut down", "Ultron, go dark.",
                     "hey ultron please switch off", "goodbye", "Shut down now.", "turn off, ultron"]:
            self.assertTrue(is_off_command(said), said)

    def test_ordinary_requests_do_not(self):
        for said in ["turn off the music", "turn off the lights", "open spotify", "shut down chrome",
                     "what does shut down mean in linux", "turn the volume down", "say goodbye to my friend"]:
            self.assertFalse(is_off_command(said), said)


class UnfinishedTests(unittest.TestCase):
    def test_trailing_words(self):
        self.assertTrue(looks_unfinished("remind me to call my mom and"))
        self.assertTrue(looks_unfinished("Remind me to call my mom and."))
        self.assertTrue(looks_unfinished("open the"))
        self.assertTrue(looks_unfinished("it's kind of like"))
        self.assertTrue(looks_unfinished("Um..."))
        self.assertFalse(looks_unfinished("open spotify"))
        self.assertFalse(looks_unfinished("What's the weather like?"))
        self.assertFalse(looks_unfinished("I'd like that."))


if __name__ == "__main__":
    unittest.main()

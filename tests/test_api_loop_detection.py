import unittest

from gemini_translator.api.base import BaseApiHandler


class ApiLoopDetectionTests(unittest.TestCase):
    def setUp(self):
        self.handler = BaseApiHandler.__new__(BaseApiHandler)

    def test_repeated_blocks_away_from_tail_are_not_treated_as_generation_loop(self):
        refrain = (
            "<p>Izuku wrote the same careful note in the kitchen ledger.</p>"
        )
        text = "\n".join([
            refrain,
            "<p>Inko put the kettle on and checked the rice.</p>",
            refrain,
            "<p>The morning train rattled past the apartment.</p>",
            refrain,
            "<p>All Might's message waited unanswered on the phone.</p>",
            refrain,
            "<p>After that, the scene moved on to school.</p>",
            "<p>Lunch came and went without another repeated line.</p>",
            "<p>The partial response ends with fresh, non-looping text.</p>",
            "<p>This final paragraph proves the repetition is not at the cutoff.</p>",
        ])

        self.assertFalse(self.handler._detect_looping(text))

    def test_repeated_sequence_at_tail_is_treated_as_generation_loop(self):
        cycle = [
            "<p>Izuku checked the notebook again.</p>",
            "<p>Inko asked whether he had eaten breakfast.</p>",
            "<p>He promised he would leave in five minutes.</p>",
        ]
        text = "\n".join([
            "<p>The chapter begins normally.</p>",
            "<p>The translation stays unique for a while.</p>",
            *cycle,
            *cycle,
            *cycle,
        ])

        self.assertTrue(self.handler._detect_looping(text))


if __name__ == "__main__":
    unittest.main()

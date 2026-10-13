import unittest

from gemini_translator.core.worker_helpers.taskers.glossary_batch_processor import (
    filter_glossary_items_for_source_text,
    limit_glossary_terms_by_frequency,
)


class GlossaryBatchProcessorTests(unittest.TestCase):
    def test_source_text_filter_rejects_terms_present_only_in_prompt_examples(self):
        glossary_items = [
            {"original": "Ping", "rus": "Пинг", "note": "Персонаж; Псевдоним"},
            {
                "original": "Martian Manhunter",
                "rus": "Марсианский Охотник",
                "note": "Персонаж; Прозвище",
            },
        ]

        filtered, discarded_count = filter_glossary_items_for_source_text(
            glossary_items,
            "Martian Manhunter stepped into the room.",
            use_jieba_for_glossary_search=True,
        )

        self.assertEqual(discarded_count, 1)
        self.assertEqual([item["original"] for item in filtered], ["Martian Manhunter"])

    def test_limit_glossary_terms_keeps_highest_frequency_terms(self):
        glossary_items = [
            {"original": "Low", "rus": "Низкий", "note": ""},
            {"original": "Tie A", "rus": "Связь А", "note": ""},
            {"original": "High", "rus": "Высокий", "note": ""},
            {"original": "Tie B", "rus": "Связь Б", "note": ""},
        ]

        limited, discarded = limit_glossary_terms_by_frequency(
            glossary_items,
            3,
            {
                "Low": 1,
                "Tie A": 3,
                "High": 7,
                "Tie B": 3,
            },
        )

        self.assertEqual([item["original"] for item in limited], ["High", "Tie A", "Tie B"])
        self.assertEqual([item["original"] for item in discarded], ["Low"])


if __name__ == "__main__":
    unittest.main()

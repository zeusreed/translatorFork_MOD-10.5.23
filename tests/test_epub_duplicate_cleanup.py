import unittest

from bs4 import BeautifulSoup

from gemini_translator.core.epub_duplicate_helpers import (
    analyze_duplicate_findings,
    extract_duplicate_review_blocks,
)


def _build_chapter_info(index, path, html):
    soup = BeautifulSoup(html, "html.parser")
    return {
        "index": index,
        "path": path,
        "name": path.split("/")[-1],
        "blocks": extract_duplicate_review_blocks(soup),
    }


class EpubDuplicateCleanupTests(unittest.TestCase):
    def test_start_duplicate_scan_stops_after_first_mismatch(self):
        chapter_infos = [
            _build_chapter_info(
                0,
                "Text/ch1.xhtml",
                """
                <html><body>
                    <h1>Chapter Alpha</h1>
                    <p>Actual first paragraph.</p>
                    <p>Chapter Alpha</p>
                </body></html>
                """,
            ),
        ]

        analysis = analyze_duplicate_findings(chapter_infos)

        self.assertEqual(analysis["start_findings"], [])

    def test_boundary_scan_stops_when_final_and_first_blocks_differ(self):
        chapter_infos = [
            _build_chapter_info(
                0,
                "Text/ch1.xhtml",
                """
                <html><body>
                    <h1>Chapter 1</h1>
                    <p>Shared line.</p>
                    <p>Different final line.</p>
                </body></html>
                """,
            ),
            _build_chapter_info(
                1,
                "Text/ch2.xhtml",
                """
                <html><body>
                    <h1>Chapter 2</h1>
                    <p>Shared line.</p>
                    <p>Different next ending.</p>
                </body></html>
                """,
            ),
        ]

        analysis = analyze_duplicate_findings(chapter_infos)

        self.assertEqual(analysis["boundary_findings"], [])

    def test_ending_scan_does_not_check_penultimate_after_final_mismatch(self):
        chapter_infos = [
            _build_chapter_info(
                0,
                "Text/ch1.xhtml",
                """
                <html><body>
                    <h1>Chapter 1</h1>
                    <p>First chapter text.</p>
                    <p>Shared penultimate line.</p>
                    <p>Final line A.</p>
                </body></html>
                """,
            ),
            _build_chapter_info(
                1,
                "Text/ch2.xhtml",
                """
                <html><body>
                    <h1>Chapter 2</h1>
                    <p>Second chapter text.</p>
                    <p>Shared penultimate line.</p>
                    <p>Final line B.</p>
                </body></html>
                """,
            ),
        ]

        analysis = analyze_duplicate_findings(chapter_infos)

        self.assertEqual(analysis["boundary_findings"], [])

    def test_ending_scan_stops_at_first_mismatch_while_walking_backwards(self):
        chapter_infos = [
            _build_chapter_info(
                0,
                "Text/ch1.xhtml",
                """
                <html><body>
                    <h1>Chapter 1</h1>
                    <p>Shared third line from end.</p>
                    <p>Penultimate line A.</p>
                    <p>Shared final line.</p>
                </body></html>
                """,
            ),
            _build_chapter_info(
                1,
                "Text/ch2.xhtml",
                """
                <html><body>
                    <h1>Chapter 2</h1>
                    <p>Shared third line from end.</p>
                    <p>Penultimate line B.</p>
                    <p>Shared final line.</p>
                </body></html>
                """,
            ),
        ]

        analysis = analyze_duplicate_findings(chapter_infos)
        tail_findings = analysis["boundary_findings"]

        self.assertEqual(len(tail_findings), 2)
        self.assertEqual({finding["text"] for finding in tail_findings}, {"Shared final line."})

    def test_ending_scan_continues_while_suffix_blocks_match(self):
        chapter_infos = [
            _build_chapter_info(
                0,
                "Text/ch1.xhtml",
                """
                <html><body>
                    <h1>Chapter 1</h1>
                    <p>Unique first chapter text.</p>
                    <p>Shared penultimate line.</p>
                    <p>Shared final line.</p>
                </body></html>
                """,
            ),
            _build_chapter_info(
                1,
                "Text/ch2.xhtml",
                """
                <html><body>
                    <h1>Chapter 2</h1>
                    <p>Unique second chapter text.</p>
                    <p>Shared penultimate line.</p>
                    <p>Shared final line.</p>
                </body></html>
                """,
            ),
        ]

        analysis = analyze_duplicate_findings(chapter_infos)
        tail_findings = analysis["boundary_findings"]

        self.assertEqual(len(tail_findings), 4)
        self.assertEqual(
            {finding["text"] for finding in tail_findings},
            {"Shared penultimate line.", "Shared final line."},
        )

    def test_ending_scan_keeps_different_final_groups_separate(self):
        chapter_infos = []
        endings = (
            ("Shared penultimate A.", "Shared final X."),
            ("Shared penultimate B.", "Shared final X."),
            ("Shared penultimate A.", "Shared final Y."),
            ("Shared penultimate B.", "Shared final Y."),
        )
        for index, (penultimate, final) in enumerate(endings):
            chapter_infos.append(
                _build_chapter_info(
                    index,
                    f"Text/ch{index + 1}.xhtml",
                    f"""
                    <html><body>
                        <h1>Chapter {index + 1}</h1>
                        <p>Unique chapter text {index + 1}.</p>
                        <p>{penultimate}</p>
                        <p>{final}</p>
                    </body></html>
                    """,
                )
            )

        analysis = analyze_duplicate_findings(chapter_infos)
        tail_findings = analysis["boundary_findings"]

        self.assertEqual(len(tail_findings), 4)
        self.assertEqual(
            {finding["text"] for finding in tail_findings},
            {"Shared final X.", "Shared final Y."},
        )

    def test_repeated_end_markers_are_reported_for_chapter_tails(self):
        chapter_infos = [
            _build_chapter_info(
                0,
                "Text/ch1.xhtml",
                """
                <html><body>
                    <h1>Chapter 1</h1>
                    <p>First chapter text.</p>
                    <p>End chapter</p>
                </body></html>
                """,
            ),
            _build_chapter_info(
                1,
                "Text/ch2.xhtml",
                """
                <html><body>
                    <h1>Chapter 2</h1>
                    <p>Second chapter text.</p>
                    <p>End chapter</p>
                </body></html>
                """,
            ),
            _build_chapter_info(
                2,
                "Text/ch3.xhtml",
                """
                <html><body>
                    <h1>Chapter 3</h1>
                    <p>Third chapter text.</p>
                    <p>Unique ending</p>
                </body></html>
                """,
            ),
        ]

        analysis = analyze_duplicate_findings(chapter_infos)
        tail_findings = analysis["boundary_findings"]

        self.assertEqual(len(tail_findings), 2)
        self.assertEqual(
            {finding["chapter_path"] for finding in tail_findings},
            {"Text/ch1.xhtml", "Text/ch2.xhtml"},
        )
        self.assertEqual(
            {finding["text"] for finding in tail_findings},
            {"End chapter"},
        )


if __name__ == "__main__":
    unittest.main()

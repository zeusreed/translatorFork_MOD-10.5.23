import re
from types import SimpleNamespace

from bs4 import BeautifulSoup, NavigableString

from gemini_translator.core.worker_helpers.response_parser import ResponseParser
from gemini_translator.utils.text import (
    prettify_html,
    process_body_tag,
    repair_unbalanced_paragraphs,
    validate_html_structure,
)


def _visible_body_text_outside_paragraphs(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    if soup.body is None:
        return []

    outside = []
    for node in soup.body.descendants:
        if not isinstance(node, NavigableString) or not node.strip():
            continue
        if node.find_parent("p") is not None:
            continue
        if node.find_parent(["h1", "h2", "h3", "h4", "h5", "h6", "script", "style"]):
            continue
        outside.append(re.sub(r"\s+", " ", str(node)).strip())
    return outside


def test_prettify_keeps_inline_markup_inside_paragraphs_when_splitting_lines():
    source = (
        "<body>"
        "<p><em>Первая строка.</em>\nВторая строка.</p>"
        "<p><strong>Третья строка.</strong>\n<span>Четвертая строка.</span></p>"
        "</body>"
    )

    result = prettify_html(source)

    soup = BeautifulSoup(result, "html.parser")
    assert [tag.get_text(" ", strip=True) for tag in soup.find_all("p")] == [
        "Первая строка.",
        "Вторая строка.",
        "Третья строка.",
        "Четвертая строка.",
    ]
    assert _visible_body_text_outside_paragraphs(result) == []


def test_unbalanced_repair_reuses_an_existing_orphan_closing_tag():
    damaged = "<body><p>Первый.</p>Второй.</p></body>"

    repaired = repair_unbalanced_paragraphs(damaged)

    assert repaired == "<body><p>Первый.</p><p>Второй.</p></body>"
    assert repaired.lower().count("<p>") == repaired.lower().count("</p>")


def test_unbalanced_repair_removes_a_standalone_trailing_closing_tag():
    damaged = "<body><p>Первый.</p><p>Второй.</p></p></body>"

    repaired = repair_unbalanced_paragraphs(damaged)

    assert repaired == "<body><p>Первый.</p><p>Второй.</p></body>"


def test_validator_repairs_an_orphan_paragraph_without_adding_an_extra_close():
    original = "<body><p>Первый.</p><p>Второй.</p></body>"
    translated = "<body><p>Первый перевод.</p>Второй перевод.</p></body>"

    is_valid, reason, repaired = validate_html_structure(original, translated)

    assert is_valid, reason
    assert repaired == "<body><p>Первый перевод.</p><p>Второй перевод.</p></body>"


def test_single_file_save_with_prettify_does_not_drop_paragraph_wrappers(tmp_path):
    original = (
        "<html><head><title>Chapter</title></head><body>"
        "<p>First.</p><p>Second.</p>"
        "</body></html>"
    )
    prefix, _, suffix = process_body_tag(original, return_parts=True, body_content_only=False)
    translated = "<body><p><em>Первый.</em>\nВторой.</p></body>"
    output_path = tmp_path / "chapter_translated.html"
    parser = ResponseParser(
        worker=SimpleNamespace(use_prettify=True),
        log_callback=lambda _message: None,
    )

    parser.process_and_save_single_file(
        translated_body_content=translated,
        original_full_content=original,
        prefix_html=prefix,
        suffix_html=suffix,
        output_path=str(output_path),
        original_internal_path="Text/chapter.xhtml",
        version_suffix="_translated.html",
    )

    saved = output_path.read_text(encoding="utf-8")
    assert _visible_body_text_outside_paragraphs(saved) == []
    assert len(BeautifulSoup(saved, "html.parser").find_all("p")) == 2

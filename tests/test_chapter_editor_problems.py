from gemini_translator.ui.dialogs.chapter_editor import _analyze_chapter_problems


def _by_title(problems):
    return {problem.title: problem for problem in problems}


def test_epub_markup_is_not_reported_as_editorial_text():
    chapter = '''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <style>.dialog::before { content: "a  b.."; }</style>
    <script>const example = "two  spaces..";</script>
  </head>
  <body>
    <p class="dialog" data-note="two  spaces" title="2 > 1">— Корректная реплика</p>
    <p>
      Обычный текст <span title="markup..">без проблем</span>.
    </p>
  </body>
</html>'''

    assert _analyze_chapter_problems(chapter) == []


def test_real_text_problems_have_exact_source_ranges_and_are_sorted():
    chapter = '<p>Первая  фраза с "прямыми кавычками"..</p>'

    problems = _analyze_chapter_problems(chapter)
    by_title = _by_title(problems)

    assert [problem.start for problem in problems] == sorted(
        problem.start for problem in problems
    )
    assert chapter[
        by_title["Лишние пробелы"].start : by_title["Лишние пробелы"].end
    ] == "  "
    assert chapter[
        by_title["Прямые кавычки"].start : by_title["Прямые кавычки"].end
    ] == '"прямыми кавычками"'
    assert chapter[
        by_title["Подозрительные точки"].start : by_title["Подозрительные точки"].end
    ] == ".."
    assert all(problem.line == 1 for problem in problems)


def test_indentation_is_ignored_but_spaces_across_inline_tags_are_found():
    chapter = '''<body>
  <p>
    Отступы разметки не являются ошибкой.
  </p>
  <p><em>А эти</em>  два пробела находятся в тексте.</p>
</body>'''

    spaces = [
        problem
        for problem in _analyze_chapter_problems(chapter)
        if problem.title == "Лишние пробелы"
    ]

    assert len(spaces) == 1
    assert chapter[spaces[0].start : spaces[0].end] == "  "
    assert spaces[0].line == 5


def test_encoded_straight_quotes_are_reported_as_visible_text():
    chapter = "<p>Здесь &quot;прямые кавычки&quot;.</p>"

    problem = _by_title(_analyze_chapter_problems(chapter))["Прямые кавычки"]

    assert chapter[problem.start : problem.end] == "&quot;прямые кавычки&quot;"


def test_structural_counts_ignore_comments_scripts_and_attributes():
    original = '''<html><body>
<!-- <p><h2>not markup</h2></p> -->
<script>const sample = "<p>not markup</p>";</script>
<p>Один абзац</p><h1>Заголовок</h1>
</body></html>'''
    translated = '''<x:html xmlns:x="http://www.w3.org/1999/xhtml"><x:body data-example="<p>">
<!-- <p><p><h3>still not markup</h3></p></p> -->
<x:p>Один абзац</x:p><x:h1>Заголовок</x:h1>
</x:body></x:html>'''

    titles = {
        problem.title
        for problem in _analyze_chapter_problems(translated, original)
    }

    assert "Количество абзацев" not in titles
    assert "Структура заголовков" not in titles


def test_heading_levels_are_compared_individually():
    original = "<body><p>Текст</p><h1>Заголовок</h1></body>"
    translated = "<body><p>Текст</p><h2>Заголовок</h2></body>"

    problem = _by_title(
        _analyze_chapter_problems(translated, original)
    )["Структура заголовков"]

    assert "h1: 0 / 1" in problem.details
    assert "h2: 1 / 0" in problem.details


def test_valid_dialogue_start_is_not_a_problem_but_service_marker_is():
    chapter = "<p>— Обычная реплика.</p>\n<!-- RESTORED_IMAGE_WARNING -->"

    problems = _analyze_chapter_problems(chapter)

    assert [problem.title for problem in problems] == ["Восстановленная картинка"]

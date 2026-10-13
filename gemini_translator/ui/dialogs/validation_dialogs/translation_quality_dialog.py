# -*- coding: utf-8 -*-
"""The «Качество перевода» window: a header, four tabs and one action bar."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QTextBlockFormat, QTextCursor
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ....qa.report_snapshot import BookQaReportSnapshot
from ....qa.settings import QaSettings
from .quality_report_view import QualityReportView
from .quality_settings_view import QualitySettingsView
from .quality_suggestions_view import QualitySuggestionsView
from .quality_widgets import (
    EmptyState,
    StatusChip,
    format_checked_at,
    make_button,
    make_label,
)


# The log keeps the newest chapters; a six-hundred-chapter book would otherwise
# grow one document until the window slows down.
LOG_MAX_BLOCKS = 4000
LOG_EMPTY_TITLE = "Журнал прохода пуст"
LOG_EMPTY_TEXT = (
    "Здесь появится каждая глава текущего прохода: что найдено, что исправлено "
    "и что осталось предложением."
)
# Space between two chapters of the log, where a ruled line used to be.
LOG_ENTRY_SPACING = 10
# One height for every button of the action bar: «Остановить проверку» takes the
# primary button's place without its padding, and the window jumped each time.
ACTION_BUTTON_HEIGHT = 36
STOP_TEXT = "Остановить проверку"
STOPPING_TEXT = "Останавливаю…"


def describe_checks(settings: QaSettings) -> str:
    """Name the checks that run after each chapter, the way the header says it."""
    checks = []
    if settings.check_language_after_chapter:
        checks.append("язык")
    cometkiwi = settings.capabilities.cometkiwi_enabled
    if settings.check_completeness_after_chapter:
        checks.append("полнота")
        # CometKiwi scores what the completeness check aligned, and the
        # refused fixes along with it.
        if cometkiwi:
            checks.append("оценка CometKiwi")
    elif cometkiwi and settings.check_language_after_chapter:
        # Without the alignment there is no chapter to score, but a fix the
        # language check refused still has its paragraph and the source.
        checks.append("оценка правок CometKiwi")
    if not checks:
        return "Проверки после глав выключены."
    return "После каждой главы: " + ", ".join(checks) + "."


def _scores_expected(settings: QaSettings, snapshot: BookQaReportSnapshot) -> bool:
    """Whether the score card has a score to show now or one to wait for."""
    return settings.capabilities.cometkiwi_enabled and (
        settings.check_completeness_after_chapter or snapshot.has_scores
    )


# The overlay card opens at this size, never beyond nine tenths of the main
# window: near the bare minimum the chapter list and a fix card crowded each other.
PREFERRED_SIZE = QSize(1440, 980)


class TranslationQualityDialog(QDialog):
    """Show what quality control found and let the user act on it."""

    check_chapter_requested = pyqtSignal(str)
    check_all_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    undo_chapter_requested = pyqtSignal(str)
    undo_all_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    settings_changed = pyqtSignal(object)
    embedding_test_requested = pyqtSignal(object)
    export_requested = pyqtSignal(str)
    apply_suggestion_requested = pyqtSignal(str)
    dismiss_suggestion_requested = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        *,
        settings: QaSettings | None = None,
        key_counter=None,
        book_title: str = "",
        model_choices=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Качество перевода")
        self.setMinimumSize(1040, 640)
        self._settings = settings or QaSettings()
        self._snapshot = BookQaReportSnapshot()
        self._busy = False
        # Set between «Остановить проверку» and the moment the pass has ended.
        self._stopping = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self._build_header(book_title))

        self.report_view = QualityReportView(self)
        self.suggestions_view = QualitySuggestionsView(self)
        self.settings_view = QualitySettingsView(
            self._settings,
            key_counter=key_counter,
            model_choices=model_choices,
            parent=self,
        )
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self.report_view, "Отчёт")
        self.tabs.addTab(self.suggestions_view, "Предложения")
        self.tabs.addTab(self._build_log_tab(), "Журнал правок")
        self.tabs.addTab(self.settings_view, "Настройки")
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self._build_action_bar())

        self.report_view.check_chapter_requested.connect(self.check_chapter_requested.emit)
        self.report_view.undo_chapter_requested.connect(self._request_undo_chapter)
        self.report_view.undo_all_requested.connect(self._request_undo_all)
        self.report_view.open_suggestions_requested.connect(
            lambda: self.tabs.setCurrentWidget(self.suggestions_view)
        )
        self.report_view.apply_suggestion_requested.connect(
            self.apply_suggestion_requested.emit
        )
        self.report_view.dismiss_suggestion_requested.connect(
            self.dismiss_suggestion_requested.emit
        )
        self.suggestions_view.apply_requested.connect(self.apply_suggestion_requested.emit)
        self.suggestions_view.dismiss_requested.connect(
            self.dismiss_suggestion_requested.emit
        )
        self.settings_view.settings_changed.connect(self._on_settings_changed)
        self.settings_view.embedding_test_requested.connect(
            self.embedding_test_requested.emit
        )

        self._refresh_header()
        self._update_action_state()

    # -- building ----------------------------------------------------------

    def _build_header(self, book_title: str) -> QFrame:
        header = QFrame(self)
        header.setObjectName("projectHeaderCard")
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(12)
        intro = QVBoxLayout()
        intro.setSpacing(2)
        intro.addWidget(make_label("Качество перевода", "sectionEyebrow", parent=header))
        self.title_label = make_label(
            book_title or "Книга без названия", "heroTitle", wrap=True, parent=header
        )
        self.subtitle_label = make_label("", "heroSubtitle", wrap=True, parent=header)
        intro.addWidget(self.title_label)
        intro.addWidget(self.subtitle_label)
        row.addLayout(intro, 1)
        self.state_chip = StatusChip("Готово к проверке", "success", header)
        row.addWidget(self.state_chip, 0, Qt.AlignmentFlag.AlignTop)
        return header

    def _build_log_tab(self) -> QWidget:
        """The running account of what the check changed, chapter by chapter.

        The report is rebuilt from the journal; a pass over a book takes hours,
        and this is what can be read while it runs.
        """
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 10, 0, 0)
        self.log_stack = QStackedWidget(page)
        self.log_empty_state = EmptyState(LOG_EMPTY_TITLE, LOG_EMPTY_TEXT, self.log_stack)
        self.log_view = QTextEdit(self.log_stack)
        self.log_view.setReadOnly(True)
        # Reading the log is looking, not typing: switching to the tab must not
        # hand it the focus and draw the accent frame round the whole page.
        self.log_view.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # A long book would otherwise grow the document without limit.
        self.log_view.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.log_stack.addWidget(self.log_empty_state)
        self.log_stack.addWidget(self.log_view)
        self.log_stack.setCurrentWidget(self.log_empty_state)
        page_layout.addWidget(self.log_stack)
        return page

    def _build_action_bar(self) -> QFrame:
        bar = QFrame(self)
        bar.setObjectName("actionBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)
        self.status_label = make_label("", "helperLabel", wrap=True, parent=bar)
        row.addWidget(self.status_label, 1)
        self.progress = QProgressBar(bar)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        # The bar shows only how much is done; the numbers and the estimate
        # are in the status line, where there is room for them.
        self.progress.setTextVisible(False)
        self.progress.setMinimumWidth(160)
        self.progress.setVisible(False)
        row.addWidget(self.progress)

        self.export_button = make_button("Экспорт отчёта", "compactActionButton", bar)
        self.check_all_button = make_button("Проверить все главы", "compactActionButton", bar)
        self.resume_button = make_button("Продолжить проверку", "primaryActionButton", bar)
        self.resume_button.setToolTip(
            "Проверить только те главы, которые ещё не проверялись, были "
            "отложены, остались с неустранённым риском или изменились после "
            "проверки. Уже улаженные главы не перепроверяются.\n"
            "Начинает с главы после последней проверенной, а пропуски "
            "в начале книги берёт в конце."
        )
        # Takes the primary button's place while a pass runs: the one thing to
        # do then is stop it.
        self.cancel_button = make_button(STOP_TEXT, "dangerActionButton", bar)
        self.close_button = make_button("Закрыть", "ghostActionButton", bar)

        self.export_button.clicked.connect(self._request_export)
        self.check_all_button.clicked.connect(self.check_all_requested.emit)
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.cancel_button.clicked.connect(self._request_cancel)
        self.close_button.clicked.connect(self.reject)
        for button in (
            self.export_button,
            self.check_all_button,
            self.resume_button,
            self.cancel_button,
            self.close_button,
        ):
            button.setMinimumHeight(ACTION_BUTTON_HEIGHT)
            row.addWidget(button)
        return bar

    # -- public API --------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return PREFERRED_SIZE.expandedTo(self.minimumSize())

    def set_report(self, snapshot: BookQaReportSnapshot) -> None:
        """Replace the report with an immutable snapshot from the journal."""
        self.report_view.set_report(
            snapshot, scoring_enabled=_scores_expected(self._settings, snapshot)
        )
        self._snapshot = snapshot
        self.suggestions_view.set_suggestions(snapshot.suggestions)
        waiting = len(snapshot.suggestions)
        self.tabs.setTabText(
            self.tabs.indexOf(self.suggestions_view),
            f"Предложения ({waiting})" if waiting else "Предложения",
        )
        self._refresh_header()
        self._update_action_state()

    def set_busy(self, busy: bool) -> None:
        """Disable everything a running check must own exclusively."""
        self._busy = bool(busy)
        self._stopping = False
        self.progress.setVisible(self._busy)
        # Fixes stay open during a pass; set_checking_chapters locks the one
        # chapter a check holds.
        self.report_view.set_busy(self._busy)
        self._refresh_header()
        self._update_action_state()

    def set_checking_chapters(self, chapter_ids) -> None:
        """Lock «Применить» for the chapters a check holds right now."""
        chapter_ids = frozenset(chapter_ids or ())
        self.report_view.set_checking_chapters(chapter_ids)
        self.suggestions_view.set_checking_chapters(chapter_ids)

    def set_deciding_suggestions(self, suggestion_ids) -> None:
        """Lock the cards of the fixes whose answer has not come back yet."""
        suggestion_ids = frozenset(suggestion_ids or ())
        self.report_view.set_deciding_suggestions(suggestion_ids)
        self.suggestions_view.set_deciding_suggestions(suggestion_ids)

    def set_progress(self, checked: int, total: int, chapter_id: str = "") -> None:
        """Show honest progress of a whole-book pass."""
        total = max(int(total), 0)
        checked = min(int(checked), total) if total else 0
        self.progress.setVisible(True)
        self.progress.setRange(0, total or 0)
        self.progress.setValue(checked)
        suffix = f" — {chapter_id}" if chapter_id else ""
        self.progress.setFormat(f"Проход: %v из %m{suffix}")
        # Numbers first: if the line runs short of room, the chapter's name is
        # what gets cut.
        parts = [f"Проход: {checked} из {total}"]
        if chapter_id:
            parts.append(str(chapter_id))
        self.status_label.setText(" · ".join(parts))

    def append_log(self, html: str) -> None:
        """Add one finished chapter to the log and keep the newest in view."""
        text = str(html or "").strip()
        if not text:
            return
        self.log_stack.setCurrentWidget(self.log_view)
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log_view.document().isEmpty():
            # Each chapter in a paragraph of its own: inserted HTML otherwise
            # runs on into the paragraph before it.
            spacing = QTextBlockFormat()
            spacing.setTopMargin(LOG_ENTRY_SPACING)
            cursor.insertBlock(spacing)
        cursor.insertHtml(text)
        self.log_view.setTextCursor(cursor)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def set_status(self, message: str) -> None:
        """Report one short outcome or failure without touching the report."""
        self.status_label.setText(str(message or ""))

    def set_embedding_result(self, message: str) -> None:
        """Show what the connection check answered next to the embedding settings."""
        self.settings_view.set_embedding_result(message)

    def selected_chapter_id(self) -> str:
        """Return the chapter the user is acting on, if any."""
        return self.report_view.selected_chapter_id()

    def select_chapter(self, chapter_id: str) -> bool:
        """Move the report to one chapter; used when navigating from a finding."""
        return self.report_view.select_chapter(chapter_id)

    def qa_settings(self) -> QaSettings:
        """Return the settings exactly as the settings tab currently expresses them."""
        return self.settings_view.qa_settings()

    # -- internals ---------------------------------------------------------

    def _on_settings_changed(self, settings: QaSettings) -> None:
        self._settings = settings
        self.report_view.set_report(
            self._snapshot, scoring_enabled=_scores_expected(settings, self._snapshot)
        )
        self._refresh_header()
        self.settings_changed.emit(settings)

    def _refresh_header(self) -> None:
        parts = [describe_checks(self._settings)]
        last_pass = format_checked_at(self._snapshot.last_checked_at)
        # While a pass runs, the last one is no longer the news.
        if last_pass and not self._busy:
            parts.append(f"Последний проход: {last_pass}.")
        self.subtitle_label.setText(" ".join(parts))
        if self._busy:
            self.state_chip.set_state("Идёт проверка", "warning")
        else:
            self.state_chip.set_state("Готово к проверке", "success")

    def _update_action_state(self) -> None:
        busy = self._busy
        self.export_button.setEnabled(bool(self._snapshot.rows) and not busy)
        self.check_all_button.setEnabled(not busy)
        self.resume_button.setEnabled(not busy)
        self.resume_button.setVisible(not busy)
        self.cancel_button.setText(STOPPING_TEXT if self._stopping else STOP_TEXT)
        self.cancel_button.setEnabled(busy and not self._stopping)
        self.cancel_button.setVisible(busy)

    def _request_cancel(self) -> None:
        if not self._busy or self._stopping:
            return
        self._stopping = True
        self._update_action_state()
        self.cancel_requested.emit()

    def _request_export(self) -> None:
        """Ask where to write the report bundle, then hand the path over."""
        directory = QFileDialog.getExistingDirectory(self, "Куда сохранить отчёт", "")
        if directory:
            self.export_requested.emit(directory)

    def _request_check_chapter(self) -> None:
        chapter_id = self.selected_chapter_id()
        if chapter_id:
            self.check_chapter_requested.emit(chapter_id)

    def _request_undo_chapter(self, chapter_id: str = "") -> None:
        chapter_id = chapter_id or self.selected_chapter_id()
        if chapter_id and self._confirm_undo([chapter_id]):
            self.undo_chapter_requested.emit(chapter_id)

    def _request_undo_all(self) -> None:
        chapters = list(self._snapshot.repaired_chapters)
        if chapters and self._confirm_undo(chapters):
            self.undo_all_requested.emit()

    def _confirm_undo(self, chapters) -> bool:
        listing = "\n".join(f"  • {chapter}" for chapter in chapters[:20])
        if len(chapters) > 20:
            listing += f"\n  … и ещё {len(chapters) - 20}"
        answer = QMessageBox.question(
            self,
            "Отменить автоматические исправления",
            "Будут восстановлены исходные версии глав:\n"
            f"{listing}\n\nГлавы, изменённые вручную после исправления, "
            "останутся нетронутыми.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

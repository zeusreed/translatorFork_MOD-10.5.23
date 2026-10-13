# -*- coding: utf-8 -*-
"""The «Отчёт» tab: book totals, the chapter list, and the selected chapter."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ....qa.report_snapshot import (
    CHAPTER_STATUS_LABELS,
    BookQaReportSnapshot,
    ChapterQaRow,
    chapter_display_name,
    chapter_display_names,
)
from ... import theme_manager
from .quality_widgets import (
    EmptyState,
    MetricCard,
    StatusChip,
    SuggestionCarousel,
    chapters_caption,
    format_checked_at,
    make_button,
    make_label,
    plural,
)
from .translation_quality_models import DECISION_LABELS


# Only the statuses that ask for attention are coloured: a column where every
# «Проверена» is green hides the one chapter that is not.
ATTENTION_TONES = {"deferred": "warning", "blocked": "danger"}
# How the chapter card's sentence begins. High QA risk asks for review while
# translation continues.
STATUS_SENTENCES = {
    "checked": "Проверена",
    "blocked": "Проверена",
    "deferred": "Отложена",
    "": "Нет данных о проверке",
}
BLOCK_CHIP_TEXT = "Требует проверки"
BASE_COLUMNS = (
    ("Глава", "chapter_id"),
    ("Статус", "status"),
    ("Исправлено", "applied_repairs"),
    ("Ждут решения", "pending_suggestions"),
)
# Only the short numbers a list has room for.  The book norm and the confirmed
# gaps are sentences; the chapter card shows them in full.
COMPLETENESS_COLUMNS = (
    ("Длина", "length_ratio"),
    ("Пропуски", "possible_gaps"),
)
SCORE_COLUMNS = (("Оценка", "quality_score"),)
# Cell padding and the rounded selection around a chapter name.
CHAPTER_COLUMN_PADDING = 36
# Text reads from the left edge; numbers sit centred under their headers.
TEXT_FIELDS = frozenset({"chapter_id", "status"})
REPORT_EMPTY_TITLE = "Отчёт пока пуст"
REPORT_EMPTY_TEXT = (
    "Проверенные главы появятся здесь после первого прохода. Проверка идёт после "
    "каждой переведённой главы или по кнопке «Проверить все главы»."
)
NO_CHAPTER_TITLE = "Глава не выбрана"
NO_CHAPTER_TEXT = "Выберите главу в списке слева."
NO_REMARKS_TITLE = "Замечаний нет"
NO_REMARKS_TEXT = (
    "У всех глав статус «Проверена», нет автоисправлений, правок, "
    "ждущих решения, и возможных пропусков."
)


def report_columns(snapshot: BookQaReportSnapshot) -> tuple[tuple[str, str], ...]:
    """The table's columns: completeness and score only where the book has them."""
    columns = BASE_COLUMNS
    if snapshot.has_completeness:
        columns += COMPLETENESS_COLUMNS
    if snapshot.has_scores:
        columns += SCORE_COLUMNS
    return columns


class QualityReportView(QWidget):
    """Show one report snapshot and ask for actions on what the user selected."""

    check_chapter_requested = pyqtSignal(str)
    undo_chapter_requested = pyqtSignal(str)
    undo_all_requested = pyqtSignal()
    open_suggestions_requested = pyqtSignal()
    apply_suggestion_requested = pyqtSignal(str)
    dismiss_suggestion_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._snapshot = BookQaReportSnapshot()
        self._busy = False
        self._scoring_enabled = False
        self._display_names: dict[str, str] = {}
        self._chapter_names_width = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.content = QWidget(self.stack)
        self.empty_state = EmptyState(REPORT_EMPTY_TITLE, REPORT_EMPTY_TEXT, self.stack)
        self.stack.addWidget(self.content)
        self.stack.addWidget(self.empty_state)

        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        content_layout.addLayout(self._build_totals())
        split = QHBoxLayout()
        split.setSpacing(10)
        # The table takes the wider half: with scores it has seven columns, and
        # at the window's usual width the last one went under a horizontal scroll.
        split.addWidget(self._build_chapter_list(), 6)
        split.addWidget(self._build_chapter_card(), 5)
        content_layout.addLayout(split, 1)

        self.stack.setCurrentWidget(self.empty_state)
        self._update_actions()

    # -- building ----------------------------------------------------------

    def _build_totals(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.checked_card = MetricCard("Проверено", parent=self.content)
        self.repaired_card = MetricCard("Исправлено автоматически", parent=self.content)
        # Always available: with nothing waiting it leads to an empty state that
        # says why, while a greyed-out button only looked broken.
        self.pending_card = MetricCard("Ждут решения", "Открыть предложения", self.content)
        self.pending_card.action_clicked.connect(self.open_suggestions_requested.emit)
        self.score_card = MetricCard("Оценка CometKiwi", parent=self.content)
        self.score_card.setVisible(False)
        row.addWidget(self.checked_card, 3)
        row.addWidget(self.repaired_card, 2)
        row.addWidget(self.pending_card, 2)
        row.addWidget(self.score_card, 2)
        return row

    def _build_chapter_list(self) -> QFrame:
        card = QFrame(self.content)
        card.setObjectName("projectPathCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(make_label("Главы", "projectCardTitle", parent=card))
        header.addStretch(1)
        # Off whenever the window opens: a filter left on from another day
        # would hide chapters from someone who forgot switching it on.
        self._remarks_only = False
        self.remarks_only_check = QCheckBox(remarks_caption(0), card)
        self.remarks_only_check.toggled.connect(self._on_remarks_only_toggled)
        header.addWidget(self.remarks_only_check)
        layout.addLayout(header)

        self.table = QTableWidget(0, len(BASE_COLUMNS), card)
        self.table.setObjectName("qualityChapterTable")
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._set_columns(BASE_COLUMNS)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.viewport().installEventFilter(self)
        layout.addWidget(self.table, 1)
        self.no_remarks_state = EmptyState(NO_REMARKS_TITLE, NO_REMARKS_TEXT, card)
        # Inside the list's own card, not a second card of its own.
        self.no_remarks_state.setObjectName("chapterListEmptyState")
        self.no_remarks_state.setVisible(False)
        layout.addWidget(self.no_remarks_state, 1)

        undo_row = QHBoxLayout()
        self.undo_all_button = make_button(
            "Отменить все автоисправления книги", "dangerActionButton", card
        )
        self.undo_all_button.clicked.connect(self.undo_all_requested.emit)
        undo_row.addWidget(self.undo_all_button)
        undo_row.addStretch(1)
        layout.addLayout(undo_row)
        return card

    def _build_chapter_card(self) -> QFrame:
        self.chapter_card = QFrame(self.content)
        self.chapter_card.setObjectName("projectHeaderCard")
        card_layout = QVBoxLayout(self.chapter_card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)
        # Everything above the buttons scrolls as one when the window is short.
        # Squeezed instead, the norm lines ran over each other and a waiting fix
        # shrank to an empty outline.
        self.chapter_body = QWidget(self.chapter_card)
        self.chapter_layout = QVBoxLayout(self.chapter_body)
        self.chapter_layout.setContentsMargins(0, 0, 0, 0)
        self.chapter_layout.setSpacing(8)
        self.chapter_scroll = QScrollArea(self.chapter_card)
        self.chapter_scroll.setWidgetResizable(True)
        self.chapter_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.chapter_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.chapter_scroll.setWidget(self.chapter_body)
        card_layout.addWidget(self.chapter_scroll, 1)
        self.chapter_layout.addWidget(
            make_label("Глава", "sectionEyebrow", parent=self.chapter_card)
        )
        self.chapter_title_label = make_label(
            NO_CHAPTER_TITLE, "heroTitle", wrap=True, parent=self.chapter_card
        )
        # The chapter's full path inside the book, when the title is only its
        # file name: two books can name a file alike, a path tells them apart.
        self.chapter_path_label = make_label(
            "", "mutedLabel", wrap=True, parent=self.chapter_card
        )
        self.chapter_path_label.setVisible(False)
        self.chapter_meta_label = make_label(
            NO_CHAPTER_TEXT, "heroSubtitle", wrap=True, parent=self.chapter_card
        )
        block_row = QHBoxLayout()
        block_row.setContentsMargins(0, 0, 0, 0)
        self.chapter_block_chip = StatusChip(BLOCK_CHIP_TEXT, "danger", self.chapter_card)
        self.chapter_block_chip.setVisible(False)
        block_row.addWidget(self.chapter_block_chip)
        block_row.addStretch(1)
        self.chapter_details_label = make_label(
            "", "mutedLabel", wrap=True, parent=self.chapter_card
        )
        self.chapter_details_label.setVisible(False)
        self.chapter_layout.addWidget(self.chapter_title_label)
        self.chapter_layout.addWidget(self.chapter_path_label)
        self.chapter_layout.addWidget(self.chapter_meta_label)
        self.chapter_layout.addLayout(block_row)
        self.chapter_layout.addWidget(self.chapter_details_label)
        self.pending_title_label = make_label(
            "", "projectCardTitle", parent=self.chapter_card
        )
        self.pending_title_label.setVisible(False)
        self.chapter_layout.addWidget(self.pending_title_label)
        # One waiting fix at a time: a column of them scrolled inside a box
        # a few lines tall, and only the first was ever read.
        self.pending_carousel = SuggestionCarousel(self.chapter_card)
        self.pending_carousel.apply_requested.connect(self.apply_suggestion_requested.emit)
        self.pending_carousel.dismiss_requested.connect(
            self.dismiss_suggestion_requested.emit
        )
        self.pending_carousel.setVisible(False)
        self.chapter_layout.addWidget(self.pending_carousel)
        # Holds the buttons at the bottom of the card whatever is shown above.
        self.chapter_layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.check_chapter_button = make_button(
            "Проверить главу", "compactActionButton", self.chapter_card
        )
        self.undo_chapter_button = make_button(
            "Отменить исправления главы", "compactActionButton", self.chapter_card
        )
        self.check_chapter_button.clicked.connect(self._request_check_chapter)
        self.undo_chapter_button.clicked.connect(self._request_undo_chapter)
        buttons.addWidget(self.check_chapter_button)
        buttons.addWidget(self.undo_chapter_button)
        buttons.addStretch(1)
        card_layout.addLayout(buttons)
        return self.chapter_card

    # -- public API --------------------------------------------------------

    def set_report(
        self, snapshot: BookQaReportSnapshot, *, scoring_enabled: bool = False
    ) -> None:
        """Show one immutable report; an unchanged chapter list is not rebuilt."""
        if not isinstance(snapshot, BookQaReportSnapshot):
            raise TypeError("snapshot must be a BookQaReportSnapshot")
        previous = self._snapshot
        self._snapshot = snapshot
        self._scoring_enabled = bool(scoring_enabled)
        self.stack.setCurrentWidget(self.content if snapshot.rows else self.empty_state)
        self._refresh_totals()
        self.remarks_only_check.setText(remarks_caption(snapshot.remark_count))
        if snapshot.rows != previous.rows or self.table.rowCount() != len(
            self._visible_rows()
        ):
            self._rebuild_table()
        if snapshot.rows and not self.selected_chapter_id():
            # Opening on nothing left half the tab empty; the chapter that most
            # needs a look is the natural place to start.
            if not self.select_chapter(_attention_chapter(snapshot)):
                self._select_first_visible()
        self._refresh_chapter_card()
        self._update_actions()

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._update_actions()

    def set_checking_chapters(self, chapter_ids) -> None:
        self.pending_carousel.set_checking_chapters(chapter_ids)

    def set_deciding_suggestions(self, suggestion_ids) -> None:
        self.pending_carousel.set_deciding_suggestions(suggestion_ids)

    def selected_chapter_id(self) -> str:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return ""
        item = self.table.item(indexes[0].row(), 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def select_chapter(self, chapter_id: str) -> bool:
        for index, row in enumerate(self._visible_rows()):
            if row.chapter_id == chapter_id:
                self.table.selectRow(index)
                return True
        return False

    # -- Qt events ---------------------------------------------------------

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            # Section widths settle after the resize itself is delivered.
            QTimer.singleShot(0, self._fit_chapter_column)
        return super().eventFilter(watched, event)

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange:
            # The theme installs its stylesheet before its palette, so the
            # colours are read once both have changed.
            QTimer.singleShot(0, self._recolor_statuses)

    # -- internals ---------------------------------------------------------

    def _set_columns(self, columns) -> None:
        """Only the chapter column stretches; every other one is as wide as its values."""
        table = self.table
        table.setColumnCount(len(columns))
        header = table.horizontalHeader()
        for index, (title, field_name) in enumerate(columns):
            item = QTableWidgetItem(title)
            item.setTextAlignment(_alignment(field_name))
            table.setHorizontalHeaderItem(index, item)
            header.setSectionResizeMode(
                index,
                QHeaderView.ResizeMode.Stretch
                if field_name == "chapter_id"
                else QHeaderView.ResizeMode.ResizeToContents,
            )

    def _rebuild_table(self) -> None:
        selected = self.selected_chapter_id()
        columns = report_columns(self._snapshot)
        rows = self._visible_rows()
        # Names are told apart across the whole book, not only what is shown.
        self._display_names = chapter_display_names(
            row.chapter_id for row in self._snapshot.rows
        )
        table = self.table
        table.setUpdatesEnabled(False)
        table.blockSignals(True)
        try:
            table.clearContents()
            self._set_columns(columns)
            table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                for column_index, (_title, field_name) in enumerate(columns):
                    table.setItem(
                        row_index,
                        column_index,
                        _item(row, field_name, self._display_names),
                    )
        finally:
            table.blockSignals(False)
            table.setUpdatesEnabled(True)
        metrics = table.fontMetrics()
        widest = max(
            (metrics.horizontalAdvance(name) for name in self._display_names.values()),
            default=0,
        )
        self._chapter_names_width = (
            max(widest, metrics.horizontalAdvance(columns[0][0])) + CHAPTER_COLUMN_PADDING
        )
        if selected:
            self.select_chapter(selected)
        filtered_empty = self._remarks_only and bool(self._snapshot.rows) and not rows
        self.table.setVisible(not filtered_empty)
        self.no_remarks_state.setVisible(filtered_empty)
        QTimer.singleShot(0, self._fit_chapter_column)

    def _fit_chapter_column(self) -> None:
        """Give the chapter column the spare width, but never less than its names.

        Stretched, it was the first to shrink: with «Длина» and «Пропуски» in a
        narrow list it went down to nothing and no chapter could be told apart.
        """
        table = self.table
        if table.columnCount() == 0:
            return
        header = table.horizontalHeader()
        needed = self._chapter_names_width
        others = header.length() - header.sectionSize(0)
        if table.viewport().width() - others >= needed:
            if header.sectionResizeMode(0) != QHeaderView.ResizeMode.Stretch:
                header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            return
        if header.sectionResizeMode(0) != QHeaderView.ResizeMode.Fixed:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, needed)

    def _recolor_statuses(self) -> None:
        """Repaint the coloured statuses in the palette that is current now."""
        columns = report_columns(self._snapshot)
        column = next(
            (index for index, (_title, name) in enumerate(columns) if name == "status"),
            None,
        )
        rows = self._visible_rows()
        if column is None or self.table.rowCount() != len(rows):
            return
        for row_index, row in enumerate(rows):
            item = self.table.item(row_index, column)
            if item is not None:
                _paint_status(item, row)

    def _visible_rows(self) -> tuple[ChapterQaRow, ...]:
        """The chapters the list shows: all of them, or only those with remarks."""
        rows = self._snapshot.rows
        if not self._remarks_only:
            return rows
        return tuple(row for row in rows if row.has_remarks)

    def _on_remarks_only_toggled(self, checked: bool) -> None:
        self._remarks_only = bool(checked)
        self._rebuild_table()
        if not self.selected_chapter_id():
            # The chosen chapter was hidden: the first one left takes its place.
            self._select_first_visible()
        self._refresh_chapter_card()
        self._update_actions()

    def _select_first_visible(self) -> None:
        rows = self._visible_rows()
        if rows:
            self.select_chapter(rows[0].chapter_id)

    def _refresh_totals(self) -> None:
        snapshot = self._snapshot
        self.checked_card.set_values(
            f"{snapshot.checked_count} из {len(snapshot.rows)}",
            _held_back(snapshot.deferred_count, snapshot.blocking_count),
        )
        self.repaired_card.set_values(
            str(snapshot.repair_count),
            f"{_in_chapters(len(snapshot.repaired_chapters))} Откатываются по главе."
            if snapshot.repair_count
            else "Автоисправлений пока нет.",
        )
        self.pending_card.set_values(
            str(snapshot.pending_suggestion_count),
            _in_chapters(len(snapshot.pending_suggestion_chapters))
            if snapshot.pending_suggestion_count
            else "Непринятых правок нет.",
        )
        average = snapshot.average_score
        scored = sum(1 for row in snapshot.rows if row.quality_score is not None)
        self.score_card.set_values(
            f"{average:.2f}" if average is not None else "—",
            f"Оценено: {chapters_caption(scored)}." if scored else "Оценок пока нет.",
        )
        self.score_card.setVisible(self._scoring_enabled)

    def _selected_row(self) -> ChapterQaRow | None:
        chapter_id = self.selected_chapter_id()
        if not chapter_id:
            return None
        return next(
            (row for row in self._snapshot.rows if row.chapter_id == chapter_id), None
        )

    def _refresh_chapter_card(self) -> None:
        row = self._selected_row()
        if row is None:
            self.chapter_title_label.setText(NO_CHAPTER_TITLE)
            self.chapter_path_label.clear()
            self.chapter_path_label.setVisible(False)
            self.chapter_meta_label.setText(NO_CHAPTER_TEXT)
            self.chapter_block_chip.setVisible(False)
            self.chapter_details_label.clear()
            self.chapter_details_label.setVisible(False)
            self._refresh_pending("")
            return
        name = self._display_names.get(row.chapter_id) or chapter_display_name(
            row.chapter_id
        )
        self.chapter_title_label.setText(name)
        shows_path = name != row.chapter_id
        self.chapter_path_label.setText(row.chapter_id if shows_path else "")
        self.chapter_path_label.setVisible(shows_path)
        self.chapter_meta_label.setText(_chapter_meta(row))
        self.chapter_block_chip.setVisible(row.status == "blocked" or bool(row.blocked_reason))
        details = _chapter_details(
            row, self._snapshot.decisions_by_chapter.get(row.chapter_id, ())
        )
        self.chapter_details_label.setText(details)
        self.chapter_details_label.setVisible(bool(details))
        self._refresh_pending(row.chapter_id)

    def _refresh_pending(self, chapter_id: str) -> None:
        suggestions = self._snapshot.suggestions_for(chapter_id) if chapter_id else ()
        self.pending_carousel.set_suggestions(suggestions)
        self.pending_title_label.setText(f"Ждут решения: {len(suggestions)}")
        self.pending_title_label.setVisible(bool(suggestions))
        self.pending_carousel.setVisible(bool(suggestions))

    def _on_selection_changed(self) -> None:
        self._refresh_chapter_card()
        self._update_actions()

    def _update_actions(self) -> None:
        chapter_id = self.selected_chapter_id()
        repaired = set(self._snapshot.repaired_chapters)
        idle = not self._busy
        self.check_chapter_button.setEnabled(bool(chapter_id) and idle)
        self.undo_chapter_button.setEnabled(
            bool(chapter_id) and chapter_id in repaired and idle
        )
        self.undo_all_button.setEnabled(bool(repaired) and idle)

    def _request_check_chapter(self) -> None:
        chapter_id = self.selected_chapter_id()
        if chapter_id:
            self.check_chapter_requested.emit(chapter_id)

    def _request_undo_chapter(self) -> None:
        chapter_id = self.selected_chapter_id()
        if chapter_id:
            self.undo_chapter_requested.emit(chapter_id)


def remarks_caption(count: int) -> str:
    return f"Только с замечаниями ({count})"


def _attention_chapter(snapshot: BookQaReportSnapshot) -> str:
    """The chapter to open on: blocking first, then deferred, then one with suggestions."""
    rows = snapshot.rows
    for needs_attention in (
        lambda row: row.status == "blocked" or bool(row.blocked_reason),
        lambda row: row.status == "deferred",
        lambda row: row.pending_suggestions > 0,
    ):
        found = next((row for row in rows if needs_attention(row)), None)
        if found is not None:
            return found.chapter_id
    return rows[0].chapter_id if rows else ""


def _held_back(deferred: int, blocking: int) -> str:
    """Summarize deferred checks and chapters needing review."""
    parts = []
    if deferred:
        parts.append(
            f"{chapters_caption(deferred)} "
            f"{plural(deferred, 'отложена', 'отложены', 'отложено')}."
        )
    if blocking:
        parts.append(
            f"{chapters_caption(blocking)} "
            f"{plural(blocking, 'требует', 'требуют', 'требуют')} проверки."
        )
    return " ".join(parts) or "Отложенных и требующих проверки глав нет."


def _in_chapters(count: int) -> str:
    """«В 1 главе», «В 21 главе», «В 2 главах»: the locative after a number."""
    noun = "главе" if count % 10 == 1 and count % 100 != 11 else "главах"
    return f"В {count} {noun}."


def _alignment(field_name: str) -> Qt.AlignmentFlag:
    if field_name in TEXT_FIELDS:
        return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    return Qt.AlignmentFlag.AlignCenter


def _paint_status(item: QTableWidgetItem, row: ChapterQaRow) -> None:
    tone = ATTENTION_TONES.get(row.status)
    if tone:
        item.setForeground(QBrush(QColor(theme_manager.color(f"{tone}_text"))))
    else:
        item.setData(Qt.ItemDataRole.ForegroundRole, None)


def _item(row: ChapterQaRow, field_name: str, names: dict[str, str]) -> QTableWidgetItem:
    item = QTableWidgetItem(_cell_text(row, field_name, names))
    item.setTextAlignment(_alignment(field_name))
    if field_name == "chapter_id":
        item.setData(Qt.ItemDataRole.UserRole, row.chapter_id)
        if item.text() != row.chapter_id:
            item.setToolTip(row.chapter_id)
    elif field_name == "status":
        _paint_status(item, row)
        if row.blocked_reason:
            item.setToolTip(f"Замечание QA: {row.blocked_reason}")
    return item


def _cell_text(row: ChapterQaRow, field_name: str, names: dict[str, str]) -> str:
    if field_name == "chapter_id":
        return names.get(row.chapter_id) or chapter_display_name(row.chapter_id)
    if field_name == "status":
        label = CHAPTER_STATUS_LABELS.get(row.status, row.status)
        # Colour is never the only signal: a high-risk chapter says so in words.
        return f"⚠️ {label}" if row.blocked_reason else label
    if field_name in ("applied_repairs", "pending_suggestions"):
        value = int(getattr(row, field_name))
        return str(value) if value else ""
    if field_name == "quality_score":
        return f"{row.quality_score:.2f}" if row.quality_score is not None else "—"
    if not row.has_completeness:
        return "—"
    if field_name == "length_ratio":
        return f"{row.length_ratio:.2f}"
    return str(getattr(row, field_name))


def _chapter_meta(row: ChapterQaRow) -> str:
    sentence = STATUS_SENTENCES.get(
        row.status, CHAPTER_STATUS_LABELS.get(row.status, row.status)
    )
    checked_at = format_checked_at(row.checked_at)
    if checked_at:
        sentence += f" {checked_at}"
    if row.risk_label:
        sentence += f", риск {row.risk_label.lower()}"
    return f"{sentence}. Исправлено автоматически: {row.applied_repairs}."


def _chapter_details(row: ChapterQaRow, decisions) -> str:
    lines: list[str] = []
    if row.blocked_reason:
        lines.append(f"Причина: {row.blocked_reason}")
    if row.has_completeness:
        lines.append(
            f"{row.language_pair}, длина {row.length_ratio:.2f} — {row.profile_status}"
        )
        lines.append(f"Книжная норма: {row.book_position}")
        lines.append(
            f"Возможные пропуски: {row.possible_gaps}, подтверждённые: {row.confirmed_gaps}"
        )
        lines.append(
            f"Конфликты терминов: {row.glossary_conflicts}, остатки исходника: "
            f"{row.untranslated_fragments}, языковые дефекты: {row.language_issues}"
        )
    if row.quality_score is not None:
        lines.append(f"Оценка CometKiwi: {row.quality_score:.2f}")
    elif row.quality_score_problem:
        lines.append(f"Оценка CometKiwi не получена: {row.quality_score_problem}.")
    if decisions:
        lines.append(
            "Решения проверки: "
            + ", ".join(DECISION_LABELS.get(decision, decision) for decision in decisions)
        )
    return "\n".join(lines)

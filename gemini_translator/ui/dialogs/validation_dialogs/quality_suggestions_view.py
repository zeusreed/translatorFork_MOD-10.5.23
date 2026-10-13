# -*- coding: utf-8 -*-
"""The «Предложения» tab: language fixes the check proposed but did not apply."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ....qa.models import QaSuggestion
from ....qa.report_snapshot import chapter_display_names
from .quality_widgets import (
    EmptyState,
    SuggestionCard,
    category_label,
    make_button,
    make_label,
    pending_caption,
)


SUGGESTIONS_EMPTY_TITLE = "Непринятых правок нет"
SUGGESTIONS_EMPTY_TEXT = (
    "Правки, которые проверка не приняла, появятся здесь после следующего прохода. "
    "Прошлые проходы их не сохраняли."
)
# Cards are built this many at a time: a pass refreshes the report every few
# seconds, and hundreds of cards of a dozen widgets each would stall it.
SUGGESTION_PAGE_SIZE = 50


class QualitySuggestionsView(QWidget):
    """Every suggestion awaiting a decision, filterable by chapter and category."""

    apply_requested = pyqtSignal(str)
    dismiss_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._suggestions: tuple[QaSuggestion, ...] = ()
        self._checking: frozenset[str] = frozenset()
        self._deciding: frozenset[str] = frozenset()
        self._shown_limit = SUGGESTION_PAGE_SIZE
        self.cards: list[SuggestionCard] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.content = QWidget(self.stack)
        self.empty_state = EmptyState(SUGGESTIONS_EMPTY_TITLE, SUGGESTIONS_EMPTY_TEXT, self.stack)
        self.stack.addWidget(self.content)
        self.stack.addWidget(self.empty_state)

        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        filters = QFrame(self.content)
        filters.setObjectName("projectPathCard")
        filter_row = QHBoxLayout(filters)
        filter_row.setContentsMargins(12, 8, 12, 8)
        filter_row.setSpacing(10)
        filter_row.addWidget(make_label("Глава", "mutedLabel", parent=filters))
        self.chapter_filter = QComboBox(filters)
        self.chapter_filter.setMinimumWidth(180)
        filter_row.addWidget(self.chapter_filter)
        filter_row.addWidget(make_label("Категория", "mutedLabel", parent=filters))
        self.category_filter = QComboBox(filters)
        self.category_filter.setMinimumWidth(180)
        filter_row.addWidget(self.category_filter)
        filter_row.addStretch(1)
        self.counter_label = make_label("", "helperLabel", parent=filters)
        filter_row.addWidget(self.counter_label)
        content_layout.addWidget(filters)

        self.cards_container = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(8)
        self.more_button = make_button("", "compactActionButton", self.cards_container)
        self.more_button.clicked.connect(self._show_more)
        area = QScrollArea(self.content)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        area.setWidget(self.cards_container)
        content_layout.addWidget(area, 1)

        self.chapter_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.category_filter.currentIndexChanged.connect(self._on_filter_changed)
        self.stack.setCurrentWidget(self.empty_state)

    def set_suggestions(self, suggestions) -> None:
        """Show the suggestions awaiting a decision; an unchanged list is kept as is."""
        suggestions = tuple(suggestions)
        if suggestions == self._suggestions:
            return
        self._suggestions = suggestions
        self._reload_filters()
        self._shown_limit = SUGGESTION_PAGE_SIZE
        self.stack.setCurrentWidget(self.content if suggestions else self.empty_state)
        self._render_cards()

    def set_checking_chapters(self, chapter_ids) -> None:
        """A running pass locks «Применить» only for the chapters it holds."""
        self._checking = frozenset(chapter_ids or ())
        self._apply_locks()

    def set_deciding_suggestions(self, suggestion_ids) -> None:
        self._deciding = frozenset(suggestion_ids or ())
        self._apply_locks()

    def _apply_locks(self) -> None:
        for card in self.cards:
            card.set_locks(
                checking=card.suggestion.chapter_id in self._checking,
                deciding=card.suggestion.suggestion_id in self._deciding,
            )

    def visible_suggestions(self) -> tuple[QaSuggestion, ...]:
        chapter = self.chapter_filter.currentData() or ""
        category = self.category_filter.currentData() or ""
        return tuple(
            item
            for item in self._suggestions
            if (not chapter or item.chapter_id == chapter)
            and (not category or item.category == category)
        )

    def _reload_filters(self) -> None:
        chapters = list(dict.fromkeys(item.chapter_id for item in self._suggestions))
        chapter_names = chapter_display_names(chapters)
        categories = sorted({item.category for item in self._suggestions}, key=category_label)
        for combo, everything, values, label in (
            (self.chapter_filter, "Все главы", chapters, chapter_names.get),
            (self.category_filter, "Все категории", categories, category_label),
        ):
            current = combo.currentData() or ""
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(everything, "")
            for value in values:
                combo.addItem(label(value) or value, value)
            combo.setCurrentIndex(max(combo.findData(current), 0))
            combo.blockSignals(False)

    def _on_filter_changed(self, *_args) -> None:
        self._shown_limit = SUGGESTION_PAGE_SIZE
        self._render_cards()

    def _show_more(self) -> None:
        self._shown_limit += SUGGESTION_PAGE_SIZE
        self._render_cards()

    def _render_cards(self) -> None:
        for card in self.cards:
            card.setParent(None)
            card.deleteLater()
        self.cards = []
        while self.cards_layout.count():
            self.cards_layout.takeAt(0)
        visible = self.visible_suggestions()
        for suggestion in visible[: self._shown_limit]:
            card = SuggestionCard(suggestion, parent=self.cards_container)
            card.apply_requested.connect(self.apply_requested.emit)
            card.dismiss_requested.connect(self.dismiss_requested.emit)
            self.cards_layout.addWidget(card)
            self.cards.append(card)
        hidden = len(visible) - len(self.cards)
        self.more_button.setText(
            f"Показать ещё {min(hidden, SUGGESTION_PAGE_SIZE)} из {hidden}"
        )
        self.more_button.setVisible(hidden > 0)
        self._apply_locks()
        self.cards_layout.addWidget(self.more_button)
        self.cards_layout.addStretch(1)
        self.counter_label.setText(pending_caption(len(self._suggestions)))

# -*- coding: utf-8 -*-
"""Labels and snapshot types the quality window shares with its controller."""

from __future__ import annotations

from ....qa.report_snapshot import (
    CHAPTER_STATUS_LABELS,
    RELATIVE_RISK_LABELS,
    RISK_LABELS,
    BookQaReportSnapshot,
    ChapterQaRow,
)


DECISION_LABELS = {
    "fixed": "Исправлено",
    "repair_rejected": "Исправление отклонено",
    "warning": "Предупреждение",
    "hallucinated_addition": "Добавленный факт",
    "no_gap": "Пропусков нет",
    "missing_content": "Потерян фрагмент",
    "covered": "Смысл передан",
    "intentional_foreign": "Намеренный иностранный текст",
    "ambiguous": "Неоднозначно",
    "excluded": "Исключено",
    "error": "Ошибка",
    "cancelled": "Отменено",
}

__all__ = (
    "BookQaReportSnapshot",
    "CHAPTER_STATUS_LABELS",
    "ChapterQaRow",
    "DECISION_LABELS",
    "RELATIVE_RISK_LABELS",
    "RISK_LABELS",
)

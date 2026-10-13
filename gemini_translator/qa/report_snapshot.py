"""One immutable, Qt-free view of the QA journal, ready to be shown or exported."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ..utils.text_sort import natural_sort_key
from .book_metrics import BookMetricsAnalyzer, RelativeRisk
from .estimators.cometkiwi_model_manager import describe_quality_score_status
from .models import ChapterMetrics, QaSuggestion, RiskLevel


RISK_LABELS = {
    RiskLevel.LOW: "Низкий",
    RiskLevel.MEDIUM: "Средний",
    RiskLevel.HIGH: "Высокий",
    RiskLevel.FAILED: "Сбой",
}
RELATIVE_RISK_LABELS = {
    RelativeRisk.UNAVAILABLE: "нет книжной нормы",
    RelativeRisk.LOW: "в норме книги",
    RelativeRisk.MEDIUM: "отклонение",
    RelativeRisk.HIGH: "сильное отклонение",
}
# What each recorded chapter status means to the person reading the report.
# The empty status belongs to a chapter known only from its repairs: a journal
# written before chapter states were recorded.
CHAPTER_STATUS_LABELS = {
    "checked": "Проверена",
    "deferred": "Отложена",
    "blocked": "Высокий риск",
    "": "Нет данных",
}


def chapter_display_name(chapter_id: str) -> str:
    """Name a chapter the way a person reads it: by its file name, not its path.

    A chapter id is the chapter's path inside the book, such as
    ``OEBPS/chapter12.xhtml``; in a narrow list every row began «OEBPS/chapt…».
    """
    text = str(chapter_id or "")
    stem = PurePosixPath(text).stem if text else ""
    return stem or text


def chapter_display_names(chapter_ids) -> dict[str, str]:
    """Display names for a whole book, keeping the folders where two files share a name."""
    ids = list(dict.fromkeys(str(item) for item in chapter_ids))
    names = {chapter_id: chapter_display_name(chapter_id) for chapter_id in ids}
    counts: dict[str, int] = {}
    for name in names.values():
        counts[name] = counts.get(name, 0) + 1
    for chapter_id, name in names.items():
        if counts[name] > 1:
            try:
                names[chapter_id] = str(PurePosixPath(chapter_id).with_suffix("")) or chapter_id
            except ValueError:
                names[chapter_id] = chapter_id
    return names


@dataclass(frozen=True, slots=True)
class ChapterQaRow:
    """One chapter as the report shows it, already formatted for reading."""

    chapter_id: str
    language_pair: str
    length_ratio: float
    profile_status: str
    book_position: str
    glossary_conflicts: int
    untranslated_fragments: int
    possible_gaps: int
    confirmed_gaps: int
    language_issues: int
    applied_repairs: int
    risk_label: str
    risk_level: str
    duration_seconds: float
    tokens: int
    blocked_reason: str = ""
    status: str = ""
    checked_at: str = ""
    pending_suggestions: int = 0
    # Only a completeness check produces metrics; without them every field
    # above that describes length, gaps or the book norm is a placeholder.
    has_completeness: bool = False
    quality_score: float | None = None
    # Why the chapter has no score, in words; empty when nothing failed.
    quality_score_problem: str = ""

    @property
    def has_remarks(self) -> bool:
        """Report whether the chapter wants a look: anything but a clean «Проверена»."""
        return (
            self.status != "checked"
            or bool(self.blocked_reason)
            or self.applied_repairs > 0
            or self.pending_suggestions > 0
            or self.possible_gaps > 0
        )


@dataclass(frozen=True, slots=True)
class BookQaReportSnapshot:
    """An immutable view of the journal, safe to hand to the UI thread."""

    rows: tuple[ChapterQaRow, ...] = ()
    decisions_by_chapter: dict[str, tuple[str, ...]] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    limited_mode_chapters: tuple[str, ...] = ()
    # Suggestions a person still has to decide on: pending, or stale because
    # the chapter changed.  Decided ones stay in the journal, not in the report.
    suggestions: tuple[QaSuggestion, ...] = ()

    @property
    def blocked_chapters(self) -> tuple[str, ...]:
        return tuple(row.chapter_id for row in self.rows if row.blocked_reason)

    @property
    def repaired_chapters(self) -> tuple[str, ...]:
        return tuple(row.chapter_id for row in self.rows if row.applied_repairs)

    @property
    def has_completeness(self) -> bool:
        return any(row.has_completeness for row in self.rows)

    @property
    def checked_count(self) -> int:
        return sum(1 for row in self.rows if row.status == "checked")

    @property
    def deferred_count(self) -> int:
        return sum(1 for row in self.rows if row.status == "deferred")

    @property
    def blocking_count(self) -> int:
        return sum(
            1 for row in self.rows if row.status == "blocked" or row.blocked_reason
        )

    @property
    def repair_count(self) -> int:
        return sum(row.applied_repairs for row in self.rows)

    @property
    def pending_suggestion_count(self) -> int:
        return sum(row.pending_suggestions for row in self.rows)

    @property
    def pending_suggestion_chapters(self) -> tuple[str, ...]:
        return tuple(row.chapter_id for row in self.rows if row.pending_suggestions)

    @property
    def remark_count(self) -> int:
        return sum(1 for row in self.rows if row.has_remarks)

    @property
    def has_scores(self) -> bool:
        return any(row.quality_score is not None for row in self.rows)

    @property
    def average_score(self) -> float | None:
        scores = [row.quality_score for row in self.rows if row.quality_score is not None]
        return sum(scores) / len(scores) if scores else None

    @property
    def last_checked_at(self) -> str:
        # One writer, one ISO format: the newest date is also the largest string.
        return max((row.checked_at for row in self.rows if row.checked_at), default="")

    def suggestions_for(self, chapter_id: str) -> tuple[QaSuggestion, ...]:
        return tuple(item for item in self.suggestions if item.chapter_id == chapter_id)

    @classmethod
    def from_journal(cls, journal, open_gates=()) -> "BookQaReportSnapshot":
        """Build the snapshot from a loaded journal and the queue's open gates."""
        gate_reasons = {
            str(getattr(gate, "chapter_id", "")): str(getattr(gate, "reason", ""))
            or "неустранённый риск"
            for gate in open_gates or ()
        }
        metrics_by_chapter = dict(getattr(journal, "metrics", None) or {})
        states = dict(getattr(journal, "chapter_states", None) or {})
        decisions: dict[str, list[str]] = {}
        for entry in getattr(journal, "candidates", ()):
            chapter_id = str(entry.get("chapter_id", ""))
            decision = str(entry.get("decision", ""))
            if chapter_id and decision:
                decisions.setdefault(chapter_id, []).append(decision)
        repairs_by_chapter: dict[str, int] = {}
        for repair in getattr(journal, "repairs", ()):
            chapter_id = str(repair.get("chapter_id", ""))
            if chapter_id:
                repairs_by_chapter[chapter_id] = repairs_by_chapter.get(chapter_id, 0) + 1
        waiting = [
            item
            for item in getattr(journal, "suggestions", ()) or ()
            if getattr(item, "awaits_decision", False)
        ]
        pending_by_chapter: dict[str, int] = {}
        for item in waiting:
            pending_by_chapter[item.chapter_id] = (
                pending_by_chapter.get(item.chapter_id, 0) + 1
            )

        # A chapter belongs in the report once anything about it was recorded:
        # a language-only check leaves a state and repairs, never metrics.
        chapter_ids = sorted(
            set(metrics_by_chapter)
            | set(states)
            | set(repairs_by_chapter)
            | set(pending_by_chapter),
            key=natural_sort_key,
        )
        metrics = [
            metrics_by_chapter[chapter_id]
            for chapter_id in chapter_ids
            if chapter_id in metrics_by_chapter
        ]
        positions = _book_positions(metrics)
        rows = tuple(
            _row_for(
                chapter_id,
                metrics_by_chapter.get(chapter_id),
                states.get(chapter_id),
                tuple(decisions.get(chapter_id, ())),
                repairs_by_chapter.get(chapter_id, 0),
                positions.get(chapter_id, "нет книжной нормы"),
                gate_reasons.get(chapter_id, ""),
                pending_by_chapter.get(chapter_id, 0),
            )
            for chapter_id in chapter_ids
        )
        return cls(
            rows=rows,
            decisions_by_chapter={
                chapter_id: tuple(values) for chapter_id, values in decisions.items()
            },
            limited_mode_chapters=tuple(
                item.chapter_id for item in metrics if _checked_without_alignment(item)
            ),
            suggestions=tuple(
                sorted(
                    waiting,
                    key=lambda item: (
                        natural_sort_key(item.chapter_id),
                        item.created_at,
                        item.suggestion_id,
                    ),
                )
            ),
        )


def _checked_without_alignment(metrics: ChapterMetrics) -> bool:
    """Report whether this chapter was checked without semantic comparison.

    The journal keeps no mode field, but it does not need one: a chapter that
    was aligned has aligned units, and a chapter checked in limited mode has
    source units and none aligned.  The report's counter for this has existed
    from the start and was never filled.
    """
    return metrics.source_units > 0 and metrics.aligned_units == 0


def _book_positions(metrics: list[ChapterMetrics]) -> dict[str, str]:
    """Describe each chapter against its own language pair, when that is known."""
    if not metrics:
        return {}
    try:
        analyzer = BookMetricsAnalyzer()
        frame = analyzer.analyze(metrics)
        if frame.empty:
            return {}
        positions: dict[str, str] = {}
        for item in metrics:
            risk = analyzer.classify_ratio_risk(frame, item.chapter_id)
            if risk.baseline.median is None:
                positions[item.chapter_id] = "нет книжной нормы"
                continue
            z_value = risk.robust_z
            label = RELATIVE_RISK_LABELS.get(risk.relative_risk, "")
            positions[item.chapter_id] = (
                f"медиана {risk.baseline.median:.2f}, z={z_value:.1f} ({label})"
                if z_value is not None
                else f"медиана {risk.baseline.median:.2f}"
            )
        return positions
    except Exception:  # noqa: BLE001 - a report must never fail on statistics
        return {}


def _row_for(
    chapter_id: str,
    metrics: ChapterMetrics | None,
    state,
    decisions: tuple[str, ...],
    applied_repairs: int,
    book_position: str,
    blocked_reason: str,
    pending_suggestions: int,
) -> ChapterQaRow:
    confirmed_gaps = sum(
        1 for decision in decisions if decision in {"fixed", "repair_rejected"}
    )
    status = str(getattr(state, "status", "") or "")
    checked_at = str(getattr(state, "updated_at", "") or "")
    if metrics is None:
        risk = _state_risk(state)
        return ChapterQaRow(
            chapter_id=chapter_id,
            language_pair="",
            length_ratio=0.0,
            profile_status="",
            book_position="",
            glossary_conflicts=0,
            untranslated_fragments=0,
            possible_gaps=0,
            confirmed_gaps=confirmed_gaps,
            language_issues=0,
            applied_repairs=applied_repairs,
            risk_label=RISK_LABELS.get(risk, "") if risk is not None else "",
            risk_level=str(risk) if risk is not None else "",
            duration_seconds=0.0,
            tokens=0,
            blocked_reason=blocked_reason,
            status=status,
            checked_at=checked_at,
            pending_suggestions=pending_suggestions,
        )
    untranslated = metrics.untranslated_by_script or {}
    risk = RiskLevel(metrics.risk_level) if metrics.risk_level else RiskLevel.LOW
    return ChapterQaRow(
        chapter_id=metrics.chapter_id,
        language_pair=f"{metrics.source_language} → {metrics.target_language}",
        length_ratio=metrics.length_ratio,
        profile_status=_profile_status(metrics),
        book_position=book_position,
        glossary_conflicts=metrics.glossary_conflicts,
        untranslated_fragments=sum(int(value) for value in untranslated.values()),
        possible_gaps=metrics.possible_gaps,
        confirmed_gaps=confirmed_gaps,
        language_issues=metrics.language_tool_issues,
        applied_repairs=applied_repairs,
        risk_label=RISK_LABELS.get(risk, str(risk)),
        risk_level=str(risk),
        duration_seconds=metrics.duration_seconds,
        tokens=metrics.input_tokens + metrics.output_tokens,
        blocked_reason=blocked_reason,
        status=status,
        checked_at=checked_at,
        pending_suggestions=pending_suggestions,
        has_completeness=True,
        quality_score=metrics.quality_score,
        quality_score_problem=describe_quality_score_status(
            metrics.quality_score_status
        ),
    )


def _state_risk(state) -> RiskLevel | None:
    """The risk a chapter state recorded, or None when there is no usable one."""
    value = getattr(state, "risk_level", None)
    if not value:
        return None
    try:
        return RiskLevel(value)
    except ValueError:
        return None


def _profile_status(metrics: ChapterMetrics) -> str:
    from .ratio_profiles import get_ratio_profile

    try:
        profile = get_ratio_profile(metrics.source_language, metrics.target_language)
    except KeyError:
        return "профиль не задан"
    inside = profile.contains(metrics.length_ratio)
    bounds = f"{profile.minimum:.2f}–{profile.maximum:.2f}"
    return f"{'в профиле' if inside else 'вне профиля'} {bounds}"

"""One entry point for chapter quality control: check, repair once, or report."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
import hashlib
import re
import time
from pathlib import Path

from ..utils.epub_json import (
    build_html_document_model,
    build_translation_payload,
    render_document_html,
)
from ..utils.text import escape_html
from .addition_detector import AdditionCandidate, ChapterContext
from .capabilities import QaCapabilitySettings
from .coverage_service import SEMANTIC_ALIGNMENT_MODE, CoverageAnalysis, CoverageRequest
from .estimators.base import SourceTranslationWindow
from .glossary_context import GlossaryContextSelector, GlossaryTerm
from .journal import QaJournal
from .language_validation import (
    DEFAULT_AUTO_FIX_CATEGORIES,
    DEFAULT_LANGUAGE_CHUNK_CHARS,
    LanguageQaRequest,
    LanguageQaResult,
    LanguageRepairConflict,
    LanguageReplacement,
    LanguageRuleIssue,
    RussianNlpAnalysis,
    apply_language_replacements,
)
from .llm.completion import CancellationToken, QaModelSelection
from .llm.omission_repairer import OmissionRepairError, RepairContext
from .book_metrics import (
    MIN_BASELINE_SOURCE_CHARS,
    BookMetricsAnalyzer,
    eligible_baseline_size,
)
from .models import (
    ChapterMetrics,
    Decision,
    QaChapterState,
    QaJournalEntry,
    QaModelValidationError,
    QaSuggestion,
    RiskLevel,
    SemanticUnit,
    VerifiedCandidate,
)
from .repair_store import (
    AppliedRepair,
    ManualEditConflict,
    RepairStore,
    RepairStoreError,
    UndoResult,
    atomic_write_bytes,
    content_digest,
)
from .repair_validator import ChapterSnapshot
from .semantic_units import flatten_visible_text
from .source_pairing import pair_source_text, payload_block_texts
from .structural_repair import (
    RepairValidationContext,
    StructuralPatch,
    StructuralRepairError,
    StructuralRepairEngine,
    document_fingerprint,
)


MAX_GLOSSARY_TERMS_PER_CANDIDATE = 12
# The terms the language check of one chapter may protect.  Each request shows
# the model only those its own paragraphs name, so a long list costs little.
MAX_GLOSSARY_TERMS_PER_LANGUAGE_CHECK = 60
# A chapter whose volume is far outside its language pair's profile or its own
# book's norm.  Deliberately not a deferred warning: nothing failed, the chapter
# was checked, and it is worth a second look rather than a retry.
RATIO_OUTLIER_WARNING = "length_ratio_outlier"
DEFERRED_WARNINGS = frozenset(
    {
        "coverage_failed",
        "embeddings_unavailable",
        "invalid_embedding_response",
        "alignment_capacity_exceeded",
        "verification_failed",
        "language_check_failed",
        "language_check_incomplete",
        "addition_detection_failed",
        "chapter_not_readable",
        "language_tool_unavailable",
        "slovnet_unavailable",
        "language_repair_not_written",
        "language_repair_not_recorded",
    }
)
# What each deferral means, in the words the log shows.  A code alone told the
# reader nothing about whether the key, the network or the model was at fault.
DEFERRED_REASONS = {
    "coverage_failed": "не удалось сравнить перевод с оригиналом",
    "embeddings_unavailable": "эмбеддинги недоступны",
    "invalid_embedding_response": "сервис эмбеддингов ответил непригодно",
    "alignment_capacity_exceeded": "глава слишком велика для выравнивания",
    "verification_failed": "проверка пропусков моделью не удалась",
    "language_check_failed": "языковая проверка упала с ошибкой",
    "language_check_incomplete": "языковая проверка не дошла до модели",
    "addition_detection_failed": "поиск добавленных фактов не удался",
    "chapter_not_readable": "файл главы не удалось прочитать",
    "language_tool_unavailable": "LanguageTool недоступен",
    "slovnet_unavailable": "Slovnet недоступен",
    "language_repair_not_written": "не удалось записать языковое исправление на диск",
    "language_repair_not_recorded": "языковое исправление записано, но не учтено в журнале правок",
}


def describe_deferral(code: str) -> str:
    """Turn a deferral code into the sentence the log shows; unknown stays as is."""
    text = str(code or "")
    return DEFERRED_REASONS.get(text, text)


@dataclass(frozen=True, slots=True)
class QaOptions:
    """Everything the user's settings decide about one QA pass."""

    capabilities: QaCapabilitySettings = field(default_factory=QaCapabilitySettings)
    check_completeness: bool = True
    auto_repair_omissions: bool = True
    check_language: bool = True
    auto_repair_language: bool = True
    detect_additions: bool = True
    auto_fix_language_categories: tuple[str, ...] = DEFAULT_AUTO_FIX_CATEGORIES
    max_repairs_per_chapter: int = 5
    # How much text one language-check request may carry, translation and
    # source together.  Read from the project's translation limit, so a book is
    # checked in the same portions it was translated in.
    language_chunk_chars: int = DEFAULT_LANGUAGE_CHUNK_CHARS

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, QaCapabilitySettings):
            raise QaModelValidationError("capabilities must be QaCapabilitySettings")
        if (
            isinstance(self.max_repairs_per_chapter, bool)
            or not isinstance(self.max_repairs_per_chapter, int)
            or self.max_repairs_per_chapter < 0
        ):
            raise QaModelValidationError(
                "max_repairs_per_chapter must be a non-negative integer"
            )
        if (
            isinstance(self.language_chunk_chars, bool)
            or not isinstance(self.language_chunk_chars, int)
            or self.language_chunk_chars < 1
        ):
            raise QaModelValidationError(
                "language_chunk_chars must be a positive integer"
            )


@dataclass(frozen=True, slots=True, eq=False)
class ChapterQaRequest:
    """One chapter, its files, and the project state a QA pass may consult."""

    chapter_id: str
    coverage_request: CoverageRequest
    translated_path: Path
    model: QaModelSelection
    session_id: str = "session"
    glossary: tuple[GlossaryTerm, ...] = ()
    style_guide: str = ""
    # Source text by *translated* block id, for a caller that already knows the
    # pairing.  Left empty, the pairing is made from the source payload against
    # the chapter as it stands when a step reads it (see _source_paragraphs).
    source_text_by_block: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("chapter_id", "session_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise QaModelValidationError(f"{field_name} must be a nonempty string")
        if not isinstance(self.coverage_request, CoverageRequest):
            raise QaModelValidationError("coverage_request must be a CoverageRequest")
        if not isinstance(self.model, QaModelSelection):
            raise QaModelValidationError("model must be a QaModelSelection")
        object.__setattr__(self, "translated_path", Path(self.translated_path))
        if not isinstance(self.glossary, tuple) or not all(
            isinstance(term, GlossaryTerm) for term in self.glossary
        ):
            raise QaModelValidationError("glossary must be GlossaryTerm values")

    @property
    def target_document_id(self) -> str:
        """The identity semantic units were extracted under, not the chapter id."""
        document_id = self.coverage_request.target_payload["document_id"]
        return str(document_id)

    @property
    def source_language(self) -> str:
        return self.coverage_request.source_language

    @property
    def target_language(self) -> str:
        return self.coverage_request.target_language


@dataclass(frozen=True, slots=True)
class OmissionRepairOutcome:
    """What happened to exactly one verified omission candidate."""

    candidate_id: str
    decision: Decision
    attempted: bool = False
    patch_id: str = ""
    reasons: tuple[str, ...] = ()
    source_text: str = ""
    inserted_text: str = ""


@dataclass(frozen=True, slots=True)
class ChapterChange:
    """One thing this pass changed, refused, or proposed, with both texts."""

    kind: str
    identity: str
    before: str = ""
    after: str = ""
    note: str = ""
    # A source fragment and its translation share no words: a word-level diff
    # between them would highlight almost everything and mean nothing.
    same_language: bool = False


@dataclass(frozen=True, slots=True)
class SuggestionWindows:
    """A refused fix as CometKiwi weighs it: its paragraph as it is and with the fix."""

    suggestion_id: str
    before: SourceTranslationWindow
    after: SourceTranslationWindow


@dataclass(frozen=True, slots=True, eq=False)
class ChapterQaResult:
    """The complete, auditable outcome of one chapter QA pass."""

    chapter_id: str
    risk_level: RiskLevel
    may_continue_translation: bool
    coverage_mode: str
    verified: tuple[VerifiedCandidate, ...] = ()
    repairs: tuple[OmissionRepairOutcome, ...] = ()
    additions: tuple[AdditionCandidate, ...] = ()
    language: LanguageQaResult | None = None
    metrics: ChapterMetrics | None = None
    warnings: tuple[str, ...] = ()
    # One source ↔ translation passage per aligned span, for the CometKiwi
    # chapter score. Empty without the completeness check; the coordinator
    # drops it once the score is taken, since it holds the chapter's text.
    quality_windows: tuple[SourceTranslationWindow, ...] = ()
    # Both versions of each paragraph a refused fix would change, scored in
    # the same request and dropped with the chapter's windows.
    suggestion_windows: tuple[SuggestionWindows, ...] = ()

    def changes(self) -> tuple["ChapterChange", ...]:
        """List every change and refusal this pass produced, in reading order."""
        categories = {
            issue.issue_id: issue.category
            for issue in (getattr(self.language, "issues", ()) if self.language else ())
        }
        records: list[ChapterChange] = []
        for repair in self.repairs:
            records.append(
                ChapterChange(
                    kind=(
                        "omission"
                        if repair.decision is Decision.FIXED
                        else "rejected"
                    ),
                    identity=repair.candidate_id,
                    before=repair.source_text,
                    after=repair.inserted_text,
                    note=", ".join(repair.reasons),
                )
            )
        for replacement in getattr(self.language, "applied", ()) or ():
            records.append(
                ChapterChange(
                    kind="language",
                    identity=replacement.issue_id,
                    before=replacement.original_text,
                    after=replacement.replacement_text,
                    note=categories.get(replacement.issue_id, ""),
                    same_language=True,
                )
            )
        refusals = dict(getattr(self.language, "refusals", {}) or {}) if self.language else {}
        for issue in getattr(self.language, "suggestions", ()) or ():
            reason = refusals.get(issue.issue_id, "")
            note = issue.category
            if reason:
                from .language_validation import describe_refusal

                note = f"{issue.category} — не применено: {describe_refusal(reason)}"
            records.append(
                ChapterChange(
                    kind="suggestion",
                    identity=issue.issue_id,
                    before=issue.original_text,
                    after=issue.replacement_text or "",
                    note=note,
                    same_language=True,
                )
            )
        for addition in self.additions:
            if addition.blocks_gate:
                records.append(
                    ChapterChange(
                        kind="addition",
                        identity=addition.candidate_id,
                        before="",
                        after=addition.context.target_text,
                    )
                )
        return tuple(records)

    def change_details_html(self) -> str:
        """Render the same report with the exact words that changed marked.

        The highlight is an addition to the labels, never a replacement for
        them: each pair still says which line is the original.
        """

        from .text_diff import highlight_added, highlight_pair

        groups = (
            ("omission", "Восстановленные пропуски", "было (оригинал)", "стало (перевод)"),
            ("language", "Языковые исправления", "было", "стало"),
            ("rejected", "Отклонённые исправления", "фрагмент оригинала", "предложенный перевод"),
            ("suggestion", "Предложения без применения", "было", "предложено"),
            ("addition", "Добавленные моделью факты", "", "в переводе"),
        )
        changes = self.changes()
        parts = [
            "<div style=\"font-family: Consolas, monospace;\">",
            f"<p><b>Глава:</b> {escape_html(self.chapter_id)}</p>",
        ]
        for kind, title, before_label, after_label in groups:
            selected = [change for change in changes if change.kind == kind]
            if not selected:
                continue
            parts.append(f"<p><b>{title}: {len(selected)}</b></p>")
            for change in selected:
                note = f", {escape_html(change.note)}" if change.note else ""
                parts.append(f"<p>[{escape_html(change.identity)}{note}]<br>")
                if change.before and change.after and change.same_language:
                    before_html, after_html = highlight_pair(change.before, change.after)
                    parts.append(f"{before_label}: {before_html}<br>")
                    parts.append(f"{after_label}: {after_html}</p>")
                elif change.before and change.after:
                    parts.append(f"{before_label}: {escape_html(change.before)}<br>")
                    parts.append(
                        f"{after_label}: {highlight_added(change.after)}</p>"
                    )
                elif change.after:
                    parts.append(
                        f"{after_label}: {highlight_added(change.after)}</p>"
                    )
                else:
                    parts.append(f"{before_label}: {escape_html(change.before)}</p>")
        unchecked = int(getattr(self.language, "unchecked_blocks", 0) or 0)
        if unchecked:
            total = int(getattr(self.language, "blocks_total", 0) or 0)
            parts.append(
                f"<p><b>ЯЗЫКОВАЯ ПРОВЕРКА НЕ ПРОШЛА: не проверено {unchecked} "
                f"из {total} абзацев</b><br>запросы не дошли до модели. "
                "Текущий проход идёт по списку глав, собранному на старте, и "
                "к этой главе не вернётся: она останется неулаженной, и её "
                "подберёт следующее «Продолжить проверку»</p>"
            )

        language_warnings = tuple(
            getattr(self.language, "warnings", ()) if self.language else ()
        )
        if language_warnings:
            from .language_validation import describe_refusal

            parts.append("<p><b>Предупреждения языковой проверки:</b><br>")
            parts.append(
                "<br>".join(
                    escape_html(describe_refusal(warning)) for warning in language_warnings
                )
            )
            parts.append("</p>")
        parts.append("</div>")
        return "".join(parts)

    @property
    def changed_anything(self) -> bool:
        """Report whether this pass actually rewrote part of the chapter."""
        return bool(self.applied_repair_ids) or bool(
            getattr(self.language, "applied", ()) if self.language else ()
        )

    def change_details(self) -> str:
        """Describe every change and refusal as before/after pairs, for the log.

        The log is where a reader decides whether to trust an automatic edit, so
        it must show the text that triggered it and the text that replaced it —
        not just a count.
        """

        lines: list[str] = [f"Глава: {self.chapter_id}"]
        categories = {
            issue.issue_id: issue.category
            for issue in (getattr(self.language, "issues", ()) if self.language else ())
        }
        applied = [
            repair for repair in self.repairs if repair.decision is Decision.FIXED
        ]
        rejected = [
            repair for repair in self.repairs if repair.decision is not Decision.FIXED
        ]

        if applied:
            lines.append("")
            lines.append(f"Восстановленные пропуски: {len(applied)}")
            for repair in applied:
                lines.append("")
                lines.append(f"  [{repair.candidate_id}]")
                lines.append(f"  было (оригинал): {repair.source_text or '—'}")
                lines.append(f"  стало (перевод): {repair.inserted_text or '—'}")

        language_applied = tuple(
            getattr(self.language, "applied", ()) if self.language else ()
        )
        if language_applied:
            lines.append("")
            lines.append(f"Языковые исправления: {len(language_applied)}")
            for replacement in language_applied:
                category = categories.get(replacement.issue_id, "")
                suffix = f", {category}" if category else ""
                lines.append("")
                lines.append(f"  [{replacement.issue_id}{suffix}]")
                lines.append(f"  было:  {replacement.original_text}")
                lines.append(f"  стало: {replacement.replacement_text}")

        if rejected:
            lines.append("")
            lines.append(f"Отклонённые исправления: {len(rejected)}")
            for repair in rejected:
                reasons = ", ".join(repair.reasons) or "без причины"
                lines.append(f"  [{repair.candidate_id}] {reasons}")
                if repair.source_text:
                    lines.append(f"  фрагмент оригинала: {repair.source_text}")

        suggestions = tuple(
            getattr(self.language, "suggestions", ()) if self.language else ()
        )
        if suggestions:
            from .language_validation import describe_refusal

            refusals = dict(getattr(self.language, "refusals", {}) or {})
            lines.append("")
            lines.append(f"Предложения без применения: {len(suggestions)}")
            for issue in suggestions[:20]:
                lines.append(f"  [{issue.issue_id}, {issue.category}] {issue.original_text}")
                if issue.replacement_text:
                    lines.append(f"  предложено: {issue.replacement_text}")
                reason = refusals.get(issue.issue_id)
                if reason:
                    lines.append(f"  причина: {describe_refusal(reason)}")

        unchecked = int(getattr(self.language, "unchecked_blocks", 0) or 0)
        if unchecked:
            total = int(getattr(self.language, "blocks_total", 0) or 0)
            lines.append("")
            lines.append(
                f"ЯЗЫКОВАЯ ПРОВЕРКА НЕ ПРОШЛА: не проверено {unchecked} "
                f"из {total} абзацев — запросы не дошли до модели. "
                "Текущий проход идёт по списку глав, собранному на старте, и "
                "к этой главе не вернётся: она останется неулаженной, и её "
                "подберёт следующее «Продолжить проверку»"
            )

        language_warnings = tuple(
            getattr(self.language, "warnings", ()) if self.language else ()
        )
        if language_warnings:
            from .language_validation import describe_refusal

            lines.append("")
            lines.append("Предупреждения языковой проверки:")
            for warning in language_warnings:
                lines.append(f"  {describe_refusal(warning)}")

        additions = [addition for addition in self.additions if addition.blocks_gate]
        if additions:
            lines.append("")
            lines.append(f"Добавленные моделью факты: {len(additions)}")
            for addition in additions:
                lines.append(f"  {addition.context.target_text}")

        return "\n".join(lines)

    @property
    def applied_repair_ids(self) -> tuple[str, ...]:
        return tuple(
            repair.candidate_id
            for repair in self.repairs
            if repair.decision is Decision.FIXED
        )


@dataclass(frozen=True, slots=True)
class SuggestionOutcome:
    """What became of one suggestion a person chose to apply or dismiss."""

    status: str
    suggestion_id: str
    chapter_id: str = ""
    detail: str = ""


_SUGGESTION_ALREADY_DECIDED = "правка уже решена или не найдена"


class TranslationQualityService:
    """Run the whole QA cascade for one chapter without touching the queue.

    The service decides nothing about the translation session: it returns a
    typed result carrying ``may_continue_translation`` and leaves the queue to
    the caller. Every stage is optional and every failure is a warning, so a QA
    outage never blocks or corrupts a translation.
    """

    def __init__(
        self,
        *,
        coverage,
        verifier,
        repairer,
        repair_engine: StructuralRepairEngine,
        repair_validator,
        store: RepairStore,
        journal: QaJournal,
        journal_path: Path | str,
        request_counter=None,
        additions=None,
        language=None,
        language_rules=None,
        russian_nlp=None,
        glossary_selector: GlossaryContextSelector | None = None,
        analysis_identity: str = "",
    ) -> None:
        for dependency, method, name in (
            (coverage, "analyze", "coverage"),
            (verifier, "verify", "verifier"),
            (repairer, "propose", "repairer"),
            (repair_engine, "preview", "repair_engine"),
            (repair_validator, "validate", "repair_validator"),
        ):
            if not callable(getattr(dependency, method, None)):
                raise QaModelValidationError(f"{name} must provide {method}")
        self._coverage = coverage
        self._verifier = verifier
        self._repairer = repairer
        self._engine = repair_engine
        self._repair_validator = repair_validator
        self._store = store
        self._journal = journal
        self._journal_path = Path(journal_path)
        # Whoever owns the completion client can say how many requests it has
        # made; the difference across one chapter is what that chapter cost.
        self._request_counter = request_counter
        self._additions = additions
        self._language = language
        self._language_rules = language_rules
        self._russian_nlp = russian_nlp
        self._glossary_selector = glossary_selector or GlossaryContextSelector()
        self._analysis_identity = str(analysis_identity or "")

    async def check_chapter(
        self,
        request: ChapterQaRequest,
        options: QaOptions,
        cancellation: CancellationToken,
    ) -> ChapterQaResult:
        """Check one chapter, repair at most once per candidate, and record it."""
        if not isinstance(request, ChapterQaRequest):
            raise TypeError("request must be a ChapterQaRequest")
        if not isinstance(options, QaOptions):
            raise TypeError("options must be a QaOptions")
        cancellation.raise_if_cancelled()

        # perf_counter, not monotonic: on Windows monotonic ticks every
        # ~15.6 ms, and a chapter that checks faster than one tick would
        # record the same zero this column showed before it was filled.
        started = time.perf_counter()
        requests_before = self._requests_made()
        warnings: list[str] = []
        coverage: CoverageAnalysis | None = None
        if options.check_completeness:
            try:
                coverage = await self._coverage.analyze(request.coverage_request)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - QA never breaks the translation
                warnings.append("coverage_failed")
        else:
            warnings.append("completeness_check_disabled")

        verified: tuple[VerifiedCandidate, ...] = ()
        repairs: tuple[OmissionRepairOutcome, ...] = ()
        additions: tuple[AdditionCandidate, ...] = ()
        if coverage is not None:
            warnings.extend(coverage.warnings)
            verified = await self._verify_candidates(
                coverage, request, cancellation, warnings
            )
            repairs = await self._repair_verified_once(
                verified, coverage, request, options, cancellation, warnings
            )
            additions = await self._detect_additions(
                coverage, request, options, cancellation, warnings
            )

        language = await self._check_language(
            request,
            options,
            cancellation,
            warnings,
            coverage.target_units if coverage is not None else (),
        )
        metrics = coverage.metrics if coverage is not None else None
        if metrics is not None:
            # The report has always had a column for this and always shown a
            # zero: nobody measured what a check actually costs in time.
            metrics = replace(
                metrics,
                duration_seconds=round(time.perf_counter() - started, 6),
                llm_requests=max(0, self._requests_made() - requests_before),
            )
        if metrics is not None and self._ratio_outlier(metrics):
            warnings.append(RATIO_OUTLIER_WARNING)
        risk, may_continue = _risk(verified, repairs, additions, coverage, warnings)
        result = ChapterQaResult(
            chapter_id=request.chapter_id,
            risk_level=risk,
            may_continue_translation=may_continue,
            coverage_mode=coverage.mode if coverage is not None else "unavailable",
            verified=verified,
            repairs=repairs,
            additions=additions,
            language=language,
            metrics=metrics,
            warnings=tuple(dict.fromkeys(warnings)),
            quality_windows=_quality_windows(coverage),
        )
        self._record(result, chapter_fingerprint(request.translated_path))
        fixes = self._suggestion_windows(result, request)
        if fixes:
            result = replace(result, suggestion_windows=fixes)
        return result

    def _requests_made(self) -> int:
        """Read the running request count, or zero when nobody is counting."""
        counter = self._request_counter
        if counter is None:
            return 0
        try:
            value = counter() if callable(counter) else getattr(counter, "requests_made", 0)
            return int(value)
        except Exception:  # noqa: BLE001 - accounting never fails a check
            return 0

    def _ratio_outlier(self, metrics: ChapterMetrics) -> bool:
        """Report whether this chapter's volume is unusual enough to look closer.

        The design has always said a statistical deviation raises a chapter to
        MEDIUM, and the search alone cannot deliver that: a chapter that lost a
        third of its text lowers its own expectations along with it, so the
        loudest evidence of all — the chapter being far outside the language
        pair's ratio and its own book's norm — has to come from the statistics.
        It only ever raises attention: length still proves nothing on its own,
        so it can neither block the queue nor authorize a repair.
        """
        # The same bar the book baseline sets for itself: a title page or a
        # three-line interlude has a ratio, and it means nothing.
        if (
            str(metrics.content_kind) != "narrative"
            or metrics.source_chars < MIN_BASELINE_SOURCE_CHARS
        ):
            return False
        try:
            analyzer = BookMetricsAnalyzer()
            history = {
                chapter_id: value
                for chapter_id, value in self._journal.metrics.items()
                if chapter_id != metrics.chapter_id
            }
            frame = analyzer.analyze([*history.values(), metrics])
            if frame.empty:
                return False
            return bool(
                analyzer.classify_ratio_risk(frame, metrics.chapter_id).requires_deep_check
            )
        except Exception:  # noqa: BLE001 - statistics never break a check
            return False

    def attach_quality_estimate(self, result: ChapterQaResult, estimate):
        """Record a quality estimate as evidence, without letting it change risk.

        The estimator is advisory by design: the returned result carries the
        score in its metrics and the journal keeps it, but ``risk_level`` and
        ``may_continue_translation`` are whatever the alignment and the model
        already decided.
        """
        metrics = result.metrics
        if metrics is None or getattr(estimate, "status", "") != "completed":
            status = _quality_score_status(estimate)
            if metrics is None:
                return result
            updated = replace(
                metrics,
                quality_estimator=getattr(estimate, "estimator", None),
                quality_score=None,
                quality_score_status=status,
            )
        else:
            updated = replace(
                metrics,
                quality_estimator=estimate.estimator,
                quality_score=estimate.chapter_score,
                quality_score_status="completed",
            )
        self._journal.upsert_metrics(updated)
        self._save_journal()
        return replace(result, metrics=updated)

    def attach_suggestion_scores(
        self, chapter_id: str, scores: Mapping[str, tuple[float, float]]
    ) -> int:
        """Write CometKiwi's two scores into each fix of the chapter; decisions stay.

        The pass recorded its fixes before the estimate came back, and a person
        may have decided one meanwhile: the scores are added all the same and
        never change what was decided.
        """
        written = 0
        for suggestion_id, (before, after) in scores.items():
            recorded = self._journal.suggestion(suggestion_id)
            if recorded is None or recorded.chapter_id != chapter_id:
                continue
            self._journal.set_suggestion_scores(suggestion_id, before, after)
            written += 1
        if written:
            self._save_journal()
        return written

    def _suggestion_windows(
        self, result: ChapterQaResult, request: ChapterQaRequest
    ) -> tuple[SuggestionWindows, ...]:
        """Both versions of each paragraph a refused fix would change, for CometKiwi.

        Built after the pass recorded its fixes: only a fix still waiting for a
        person is worth scoring, and the chapter as it stands now, with this
        pass's automatic edits, is the text the fix would go into.
        """
        suggestions = _suggestions_for(result) or ()
        if not suggestions:
            return ()
        try:
            html = request.translated_path.read_text(encoding="utf-8")
            model = build_html_document_model(html, document_id=request.chapter_id)
            blocks = build_translation_payload(model)["blocks"]
        except (OSError, ValueError):
            return ()
        paragraphs = {
            block.get("id"): flatten_visible_text(block["inlines"])[0] for block in blocks
        }
        sources = _source_paragraphs(request, tuple(paragraphs.items()))
        if not sources:
            return ()
        windows: list[SuggestionWindows] = []
        for suggestion in suggestions:
            recorded = self._journal.suggestion(suggestion.suggestion_id)
            if recorded is None or recorded.status != "pending":
                continue
            pair = _suggestion_window_pair(
                suggestion,
                str(sources.get(suggestion.block_id, "") or ""),
                paragraphs.get(suggestion.block_id, ""),
            )
            if pair is not None:
                windows.append(pair)
        return tuple(windows)

    @property
    def session_id(self) -> str:
        """The session every automatic repair of this service is recorded under."""
        return self._store.session_id

    async def undo_chapter(self, chapter_id: str) -> UndoResult:
        """Revert every automatic repair of one chapter without a model call."""
        return self._store.undo_chapter(chapter_id)

    async def undo_session(self, session_id: str) -> UndoResult:
        """Revert every automatic repair of one session, newest chapter first."""
        return self._store.undo_session(session_id)

    def suggestion(self, suggestion_id: str) -> QaSuggestion | None:
        """Find one recorded suggestion, whatever its state."""
        return self._journal.suggestion(suggestion_id)

    async def apply_suggestion(
        self, suggestion_id: str, translated_path: Path | str | None
    ) -> SuggestionOutcome:
        """Write one suggestion into its chapter the way an automatic fix is written.

        It is backed up first and recorded in the repair store, so «Отменить
        исправления главы» reverts it like any other fix.  A chapter that has
        changed since the check is left untouched and the suggestion goes stale.
        """
        suggestion = self._journal.suggestion(suggestion_id)
        if suggestion is None or suggestion.status != "pending":
            return SuggestionOutcome(
                "missing",
                suggestion_id,
                getattr(suggestion, "chapter_id", ""),
                _SUGGESTION_ALREADY_DECIDED,
            )
        chapter_id = suggestion.chapter_id
        if not suggestion.applicable:
            return SuggestionOutcome(
                "failed",
                suggestion_id,
                chapter_id,
                "такую правку вносят вручную",
            )
        path = Path(translated_path) if translated_path else None
        try:
            if path is None:
                raise FileNotFoundError(chapter_id)
            before = path.read_bytes()
        except OSError:
            return self._suggestion_went_stale(suggestion, "перевод главы не найден")
        try:
            model = build_html_document_model(before.decode("utf-8"), document_id=chapter_id)
            edited = apply_language_replacements(
                model,
                (
                    LanguageReplacement(
                        issue_id=suggestion_id,
                        block_id=suggestion.block_id,
                        original_text=suggestion.original_text,
                        replacement_text=suggestion.replacement_text,
                    ),
                ),
            )
        except (LanguageRepairConflict, ValueError):
            return self._suggestion_went_stale(
                suggestion, _why_it_does_not_fit(before, suggestion)
            )
        payload = render_document_html(edited).encode("utf-8")
        try:
            backup = self._store.backup_chapter(chapter_id, path)
            atomic_write_bytes(path, payload)
        except (OSError, RepairStoreError) as error:
            return SuggestionOutcome(
                "failed", suggestion_id, chapter_id, f"не удалось записать главу: {error}"
            )
        patch_id = "sug" + hashlib.sha256(suggestion_id.encode("utf-8")).hexdigest()[:20]
        applied = AppliedRepair(
            patch_id=patch_id,
            chapter_id=chapter_id,
            session_id=self._store.session_id,
            chapter_path=path,
            backup_path=backup.path,
            before_sha256=content_digest(before),
            after_sha256=content_digest(payload),
            inserted_text=suggestion.replacement_text[:500],
        )
        try:
            self._store.record_applied(applied)
        except Exception as error:  # noqa: BLE001 - a written fix must stay recorded or undone
            try:
                atomic_write_bytes(path, before)
            except OSError:
                pass
            return SuggestionOutcome(
                "failed",
                suggestion_id,
                chapter_id,
                f"исправление не записано в хранилище правок: {error}",
            )
        self._journal.append_repair(
            {
                "patch_id": patch_id,
                "chapter_id": chapter_id,
                "candidate_id": "suggestion",
                "session_id": self._store.session_id,
                "fragment": applied.inserted_text,
            }
        )
        self._journal.set_suggestion_status(suggestion_id, "applied")
        state = self._journal.chapter_states.get(chapter_id)
        if state is not None:
            # The chapter changed on purpose: «Продолжить проверку» must not
            # take this one accepted edit as a reason to check it again.
            self._journal.record_chapter_state(
                replace(state, fingerprint=chapter_fingerprint(path))
            )
        self._save_journal()
        return SuggestionOutcome("applied", suggestion_id, chapter_id)

    async def dismiss_suggestion(self, suggestion_id: str) -> SuggestionOutcome:
        """Take one suggestion off the list; the chapter is not touched."""
        suggestion = self._journal.suggestion(suggestion_id)
        if suggestion is None or not suggestion.awaits_decision:
            return SuggestionOutcome(
                "missing",
                suggestion_id,
                getattr(suggestion, "chapter_id", ""),
                _SUGGESTION_ALREADY_DECIDED,
            )
        self._journal.set_suggestion_status(suggestion_id, "dismissed")
        self._save_journal()
        return SuggestionOutcome("dismissed", suggestion_id, suggestion.chapter_id)

    def _suggestion_went_stale(
        self, suggestion: QaSuggestion, note: str
    ) -> SuggestionOutcome:
        self._journal.set_suggestion_status(suggestion.suggestion_id, "stale", note)
        self._save_journal()
        return SuggestionOutcome("stale", suggestion.suggestion_id, suggestion.chapter_id, note)

    async def _verify_candidates(
        self,
        coverage: CoverageAnalysis,
        request: ChapterQaRequest,
        cancellation: CancellationToken,
        warnings: list[str],
    ) -> tuple[VerifiedCandidate, ...]:
        items = []
        for candidate in coverage.candidates:
            cancellation.raise_if_cancelled()
            context = coverage.contexts.get(candidate.candidate_id)
            if context is None:
                warnings.append("candidate_context_missing")
                continue
            glossary = self._glossary_selector.select_for_candidate(
                request.glossary,
                context.source_text,
                f"{context.source_before} {context.source_after}".strip(),
                MAX_GLOSSARY_TERMS_PER_CANDIDATE,
            )
            items.append((candidate, context, glossary))
        verify_many = getattr(self._verifier, "verify_many", None)
        if items and callable(verify_many):
            # The candidates of a chapter share requests (OmissionVerifier).
            try:
                return tuple(await verify_many(items, request.model, cancellation))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad batch is not a failed chapter
                warnings.append("verification_failed")
                return ()
        verified: list[VerifiedCandidate] = []
        for candidate, context, glossary in items:
            try:
                verified.append(
                    await self._verifier.verify(
                        candidate, context, glossary, request.model, cancellation
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad candidate is not a failure
                warnings.append("verification_failed")
        return tuple(verified)

    async def _repair_verified_once(
        self,
        verified: Sequence[VerifiedCandidate],
        coverage: CoverageAnalysis,
        request: ChapterQaRequest,
        options: QaOptions,
        cancellation: CancellationToken,
        warnings: list[str],
    ) -> tuple[OmissionRepairOutcome, ...]:
        outcomes: list[OmissionRepairOutcome] = []
        eligible = [item for item in verified if item.eligible_for_repair]
        if not eligible:
            return ()
        if not options.auto_repair_omissions:
            warnings.append("omission_auto_repair_disabled")
            return tuple(
                OmissionRepairOutcome(item.candidate.candidate_id, Decision.WARNING)
                for item in eligible
            )

        units = {unit.unit_id: unit for unit in coverage.target_units}
        try:
            html = request.translated_path.read_text(encoding="utf-8")
        except OSError:
            warnings.append("chapter_not_readable")
            return tuple(
                OmissionRepairOutcome(item.candidate.candidate_id, Decision.WARNING)
                for item in eligible
            )
        model = build_html_document_model(
            html, document_id=request.target_document_id
        )

        for index, item in enumerate(eligible):
            cancellation.raise_if_cancelled()
            if index >= options.max_repairs_per_chapter:
                outcomes.append(
                    OmissionRepairOutcome(
                        item.candidate.candidate_id,
                        Decision.WARNING,
                        reasons=("repair_budget_exhausted",),
                    )
                )
                continue
            outcome, model = await self._repair_one(
                item, units, model, request, cancellation, coverage.target_units
            )
            outcomes.append(outcome)
        return tuple(outcomes)

    async def _repair_one(
        self,
        item: VerifiedCandidate,
        units: Mapping[str, SemanticUnit],
        model: dict,
        request: ChapterQaRequest,
        cancellation: CancellationToken,
        ordered_units: Sequence[SemanticUnit] = (),
    ) -> tuple[OmissionRepairOutcome, dict]:
        candidate_id = item.candidate.candidate_id
        before_window, after_window = _style_windows(item, ordered_units)
        glossary = self._glossary_selector.select_for_candidate(
            request.glossary,
            item.context.source_text,
            f"{item.context.source_before} {item.context.source_after}".strip(),
            MAX_GLOSSARY_TERMS_PER_CANDIDATE,
        )
        try:
            proposal = await self._repairer.propose(
                item,
                RepairContext(
                    chapter_id=request.chapter_id,
                    model=request.model,
                    cancellation=cancellation,
                    glossary=glossary,
                    style_guide=request.style_guide,
                    target_window_before=before_window,
                    target_window_after=after_window,
                ),
            )
        except asyncio.CancelledError:
            raise
        except OmissionRepairError as error:
            return (
                OmissionRepairOutcome(
                    candidate_id,
                    Decision.REPAIR_REJECTED,
                    attempted=error.reason != "not_eligible",
                    reasons=(error.reason, error.detail) if error.detail else (error.reason,),
                    source_text=item.context.source_text,
                ),
                model,
            )

        patch = _patch_for(item, units, model, request.chapter_id, proposal.translated_fragment)
        rejected = partial(
            OmissionRepairOutcome,
            candidate_id,
            Decision.REPAIR_REJECTED,
            attempted=True,
            source_text=item.context.source_text,
            inserted_text=proposal.translated_fragment,
        )
        if patch is None:
            return rejected(reasons=("anchor_units_unavailable",)), model
        try:
            preview = self._engine.preview(model, patch)
        except StructuralRepairError as error:
            return (
                rejected(patch_id=patch.patch_id, reasons=(type(error).__name__,)),
                model,
            )
        if preview.status == "already_applied":
            return (
                OmissionRepairOutcome(
                    candidate_id,
                    Decision.FIXED,
                    attempted=False,
                    patch_id=patch.patch_id,
                    source_text=item.context.source_text,
                    inserted_text=proposal.translated_fragment,
                ),
                model,
            )

        local = self._engine.validate(
            preview,
            RepairValidationContext(item.context.source_text, glossary),
        )
        if not local.accepted:
            return rejected(patch_id=patch.patch_id, reasons=local.reasons), model

        validation = await self._repair_validator.validate(
            ChapterSnapshot(request.chapter_id, preview.before_html),
            ChapterSnapshot(request.chapter_id, preview.rendered_html),
            item,
            proposal,
            model=request.model,
            cancellation=cancellation,
            glossary=glossary,
        )
        if not validation.accepted:
            return rejected(patch_id=patch.patch_id, reasons=validation.reasons), model

        try:
            self._engine.commit(preview, request.translated_path, self._store)
        except (ManualEditConflict, OSError) as error:
            return (
                rejected(patch_id=patch.patch_id, reasons=(type(error).__name__,)),
                model,
            )
        self._journal.append_repair(
            {
                "patch_id": patch.patch_id,
                "chapter_id": request.chapter_id,
                "candidate_id": candidate_id,
                "session_id": request.session_id,
                "fragment": proposal.translated_fragment,
            }
        )
        self._save_journal()
        return (
            OmissionRepairOutcome(
                candidate_id,
                Decision.FIXED,
                attempted=True,
                patch_id=patch.patch_id,
                source_text=item.context.source_text,
                inserted_text=preview.inserted_text.strip() or proposal.translated_fragment,
            ),
            preview.document_model,
        )

    async def _detect_additions(
        self,
        coverage: CoverageAnalysis,
        request: ChapterQaRequest,
        options: QaOptions,
        cancellation: CancellationToken,
        warnings: list[str],
    ) -> tuple[AdditionCandidate, ...]:
        if self._additions is None or not options.detect_additions:
            return ()
        if coverage.mode != SEMANTIC_ALIGNMENT_MODE:
            return ()
        try:
            return await self._additions.detect(
                coverage,
                ChapterContext(
                    chapter_id=request.chapter_id,
                    model=request.model,
                    cancellation=cancellation,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - additions are advisory
            warnings.append("addition_detection_failed")
            return ()

    async def _check_language(
        self,
        request: ChapterQaRequest,
        options: QaOptions,
        cancellation: CancellationToken,
        warnings: list[str],
        target_units: Sequence[SemanticUnit] = (),
    ) -> LanguageQaResult | None:
        if self._language is None or not options.check_language:
            return None
        try:
            html = request.translated_path.read_text(encoding="utf-8")
        except OSError:
            warnings.append("chapter_not_readable")
            return None
        model = build_html_document_model(html, document_id=request.chapter_id)
        language_request = LanguageQaRequest(
            chapter_id=request.chapter_id,
            document_model=model,
            source_language=request.source_language,
            target_language=request.target_language,
            model=request.model,
            cancellation=cancellation,
            # Paired against the chapter as it is now: a restored paragraph
            # moves the DOM ids of every block after it.
            source_text_by_block=_source_paragraphs(
                request, payload_block_texts(build_translation_payload(model))
            ),
            glossary=self._glossary_selector.select_for_candidate(
                request.glossary,
                " ".join(
                    text
                    for _, text in payload_block_texts(
                        request.coverage_request.source_payload
                    )
                ),
                "",
                MAX_GLOSSARY_TERMS_PER_LANGUAGE_CHECK,
            ),
            auto_fix_categories=options.auto_fix_language_categories,
            max_chunk_chars=options.language_chunk_chars,
        )
        rule_candidates = await self._collect_rules(
            target_units, options, warnings
        )
        nlp_analysis = self._analyze_nlp(target_units, options, warnings)
        try:
            result = await self._language.check_chapter(
                language_request,
                rule_candidates=rule_candidates,
                nlp_analysis=nlp_analysis,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - language QA never breaks a chapter
            warnings.append("language_check_failed")
            return None
        if result.unchecked_blocks:
            # Part of the chapter never reached the model.  Saying nothing here
            # would report it as checked and clean; instead the chapter is
            # deferred, so the next pass selects it again.  Not this one: a
            # pass walks the list it was given at the start and never grows.
            warnings.append("language_check_incomplete")
        if result.preview_model is not None and options.auto_repair_language:
            self._write_language_repairs(request, result, warnings)
        return result

    def _write_language_repairs(
        self, request: ChapterQaRequest, result: LanguageQaResult, warnings: list[str]
    ) -> None:
        """Write the confirmed language fixes the same way a repair is written.

        A language fix edits the book exactly as a restored omission does, so it
        gets the same backup and the same journal entry: without them the undo
        button would silently leave these edits in place.

        Every step below — the render, the backup, the rollback write, the
        journal append — can fail on its own (a disk that is out of space
        keeps failing the second time too, exactly when the code below tries
        to roll the file back), so the whole thing is guarded here: any
        failure degrades to a warning instead of escaping check_chapter and
        losing the whole chapter's result.
        """
        warnings_before = len(warnings)
        try:
            self._write_language_repairs_unguarded(request, result, warnings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a language fix must never break the whole check
            if len(warnings) == warnings_before:
                warnings.append("language_repair_not_written")

    def _write_language_repairs_unguarded(
        self, request: ChapterQaRequest, result: LanguageQaResult, warnings: list[str]
    ) -> None:
        path = request.translated_path
        payload = render_document_html(result.preview_model).encode("utf-8")
        try:
            before = path.read_bytes()
            backup = self._store.backup_chapter(request.chapter_id, path)
            atomic_write_bytes(path, payload)
        except (OSError, RepairStoreError):
            warnings.append("language_repair_not_written")
            return
        identity = "\x1f".join(
            (request.chapter_id,)
            + tuple(item.issue_id for item in result.applied)
        )
        patch_id = "lang" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
        applied = AppliedRepair(
            patch_id=patch_id,
            chapter_id=request.chapter_id,
            session_id=self._store.session_id,
            chapter_path=path,
            backup_path=backup.path,
            before_sha256=content_digest(before),
            after_sha256=content_digest(payload),
            inserted_text="; ".join(
                item.replacement_text for item in result.applied
            )[:500],
        )
        try:
            self._store.record_applied(applied)
        except Exception:  # noqa: BLE001 - a written fix must stay recorded or undone
            atomic_write_bytes(path, before)
            warnings.append("language_repair_not_recorded")
            return
        self._journal.append_repair(
            {
                "patch_id": patch_id,
                "chapter_id": request.chapter_id,
                "candidate_id": "language",
                "session_id": self._store.session_id,
                "fragment": applied.inserted_text,
            }
        )
        self._save_journal()

    async def _collect_rules(
        self,
        target_units: Sequence[SemanticUnit],
        options: QaOptions,
        warnings: list[str],
    ) -> tuple[LanguageRuleIssue, ...]:
        """Collect unconfirmed rule hints; an analyzer outage is only a warning."""
        if self._language_rules is None or not options.capabilities.language_tool_enabled:
            return ()
        try:
            result = await self._language_rules.collect(
                tuple(target_units), options.capabilities
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unavailable analyzer is a warning
            warnings.append("language_tool_unavailable")
            return ()
        warnings.extend(getattr(result, "warnings", ()) or ())
        hints = getattr(result, "hints", None)
        return tuple(hints()) if callable(hints) else tuple(result)

    def _analyze_nlp(
        self,
        target_units: Sequence[SemanticUnit],
        options: QaOptions,
        warnings: list[str],
    ) -> RussianNlpAnalysis | None:
        if self._russian_nlp is None or not options.capabilities.slovnet_enabled:
            return None
        try:
            result = self._russian_nlp.analyze(
                tuple(target_units), options.capabilities
            )
        except Exception:  # noqa: BLE001 - an unavailable analyzer is a warning
            warnings.append("slovnet_unavailable")
            return None
        warnings.extend(getattr(result, "warnings", ()) or ())
        report = getattr(result, "analysis", None)
        if report is None:
            return None
        to_analysis = getattr(report, "as_analysis", None)
        return to_analysis() if callable(to_analysis) else report

    def _record(self, result: ChapterQaResult, fingerprint: str = "") -> None:
        entries = [
            QaJournalEntry(
                entry_id=f"{result.chapter_id}:{repair.candidate_id}",
                chapter_id=result.chapter_id,
                decision=repair.decision,
            )
            for repair in result.repairs
        ]
        entries.extend(
            QaJournalEntry(
                entry_id=f"{result.chapter_id}:{addition.candidate_id}",
                chapter_id=result.chapter_id,
                decision=(
                    Decision.HALLUCINATED_ADDITION
                    if addition.blocks_gate
                    else Decision.WARNING
                ),
            )
            for addition in result.additions
        )
        self._journal.record_chapter_result(
            metrics=result.metrics,
            entries=entries,
            state=QaChapterState(
                chapter_id=result.chapter_id,
                status=_chapter_status(result),
                analysis_identity=self._analysis_identity,
                risk_level=result.risk_level,
                book_sample_size=eligible_baseline_size(
                    self._journal.metrics.values(),
                    getattr(result.metrics, "source_language", ""),
                    getattr(result.metrics, "target_language", ""),
                ),
                fingerprint=fingerprint,
                updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
            suggestions=_suggestions_for(result),
        )
        self._save_journal()

    def _save_journal(self) -> None:
        try:
            self._journal.save(self._journal_path)
        except OSError:
            # A journal that cannot be written must not undo an applied repair.
            return


# The reason can come from a runner process reached over the network - for a
# remote COMETKiwi server, an unauthenticated one - and is about to be
# written into translation_qa.json and the CSV export, so it is bounded to a
# short machine-readable token before it is persisted. One optional
# colon-separated suffix is allowed, matching what _safe_reason() in
# cometkiwi_client.py produces ("runner_error", or "runner_error:<suffix>").
# [0-9] here is the ASCII digit range on purpose: _safe_reason() keeps any
# character str.isdigit() calls true, which also includes non-ASCII digits
# such as "²" or "٣", so its own output is not guaranteed ASCII -
# this pattern, not that function, is the actual bound.
_REASON_PATTERN = re.compile(r"[a-z0-9_]{1,64}(?::[a-z0-9_]{1,64})?")


# The value this returns is "<status>:<reason>", and <reason> may itself
# contain one more ":" (for example "runner_error:out_of_memory"). A
# consumer must split on the FIRST ":" only, never on every ":".
def _quality_score_status(estimate) -> str:
    """Turn one quality estimate into the value stored in quality_score_status.

    A completed estimate always gives "completed" - unchanged, and this is
    also what clears an earlier reason once a chapter is rechecked and the
    estimator answers again. A non-completed estimate whose metadata carries
    a usable reason gives "<status>:<reason>", where <reason> is one ASCII
    identifier or two such identifiers joined by one ":" - the shape
    _safe_reason() produces for a runner failure, e.g.
    "runner_error:out_of_memory". A consumer must split on the FIRST ":"
    only. Anything else - no metadata, no "reason" key, metadata that is not
    a mapping, or a reason that is not that bounded shape - gives the bare
    status, exactly as before this function existed.
    """
    status = str(getattr(estimate, "status", "") or "unavailable")
    if status == "completed":
        return status
    metadata = getattr(estimate, "metadata", None)
    if not isinstance(metadata, Mapping):
        return status
    reason = metadata.get("reason")
    if not isinstance(reason, str) or not reason:
        return status
    if not _REASON_PATTERN.fullmatch(reason):
        reason = "invalid_reason"
    return f"{status}:{reason}"


def _quality_windows(
    coverage: CoverageAnalysis | None,
) -> tuple[SourceTranslationWindow, ...]:
    """Turn the completeness alignment into the passages CometKiwi scores.

    Every span with text on both sides is one window: up to three sentences of
    the source and the translation the alignment matched to them. A gap has
    nothing to compare and stays with the omission check. Without an alignment
    - the check switched off, or limited mode - there is nothing to score.
    """
    alignment = coverage.alignment if coverage is not None else None
    if alignment is None:
        return ()
    source = {unit.unit_id: unit.text for unit in coverage.source_units}
    target = {unit.unit_id: unit.text for unit in coverage.target_units}
    windows: list[SourceTranslationWindow] = []
    for index, span in enumerate(alignment.spans):
        source_text = _joined_units(span.source_unit_ids, source)
        translation = _joined_units(span.target_unit_ids, target)
        visible = sum(1 for character in translation if not character.isspace())
        if not source_text or visible <= 0:
            continue
        windows.append(
            SourceTranslationWindow(
                window_id=f"span-{index}",
                source=source_text,
                translation=translation,
                visible_chars=visible,
            )
        )
    return tuple(windows)


def _joined_units(unit_ids: Sequence[str], texts: Mapping[str, str]) -> str:
    parts = (texts.get(unit_id, "").strip() for unit_id in unit_ids)
    return " ".join(part for part in parts if part)


def _chapter_status(result: ChapterQaResult) -> str:
    """Classify the chapter for QA review without affecting translation."""
    if not result.may_continue_translation:
        return "blocked"
    if any(warning in DEFERRED_WARNINGS for warning in result.warnings):
        return "deferred"
    return "checked"


def _source_paragraphs(
    request: ChapterQaRequest, target_blocks: Sequence[tuple[str, str]]
) -> Mapping[str, str]:
    """Source text of each translated block of the chapter as it stands now.

    A pairing the caller supplied wins.  Otherwise paragraphs pair by their
    order, never by block id: the id is a DOM path and matches between source
    and translation only when both share the same markup.
    """
    if request.source_text_by_block:
        return request.source_text_by_block
    return pair_source_text(
        payload_block_texts(request.coverage_request.source_payload), target_blocks
    )


def _suggestions_for(result: ChapterQaResult) -> tuple[QaSuggestion, ...] | None:
    """The language fixes this pass refused, in the form the journal keeps.

    None when the language check did not run for the chapter: whatever an
    earlier pass left for the user then stays exactly as it was.
    """
    language = result.language
    if language is None:
        return None
    refusals = dict(getattr(language, "refusals", {}) or {})
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    suggestions: dict[str, QaSuggestion] = {}
    for issue in getattr(language, "suggestions", ()) or ():
        replacement = issue.replacement_text or ""
        if replacement == issue.original_text:
            # A replacement equal to its original proposes nothing to apply or decline.
            continue
        suggestion_id = QaSuggestion.identity(
            result.chapter_id, issue.block_id, issue.original_text, replacement
        )
        suggestions.setdefault(
            suggestion_id,
            QaSuggestion(
                suggestion_id=suggestion_id,
                chapter_id=result.chapter_id,
                block_id=issue.block_id,
                category=issue.category,
                original_text=issue.original_text,
                replacement_text=replacement,
                reason=str(refusals.get(issue.issue_id, "")),
                explanation=issue.explanation,
                created_at=created_at,
            ),
        )
    return tuple(suggestions.values())


def _suggestion_window_pair(
    suggestion: QaSuggestion, source: str, paragraph: str
) -> SuggestionWindows | None:
    """The paragraph as it is and with the fix, each against the paragraph's source.

    None when a score would mislead: no replacement to weigh, no source to
    weigh it against, or a fragment the paragraph holds more than once.
    """
    original = suggestion.original_text
    replacement = suggestion.replacement_text
    source = source.strip()
    if not source or not original.strip() or not replacement.strip():
        return None
    if paragraph.count(original) != 1:
        return None
    before = paragraph.strip()
    after = paragraph.replace(original, replacement, 1).strip()
    visible_before = sum(1 for character in before if not character.isspace())
    visible_after = sum(1 for character in after if not character.isspace())
    if visible_before <= 0 or visible_after <= 0:
        return None
    identity = suggestion.suggestion_id
    return SuggestionWindows(
        suggestion_id=identity,
        before=SourceTranslationWindow(f"{identity}:before", source, before, visible_before),
        after=SourceTranslationWindow(f"{identity}:after", source, after, visible_after),
    )


def _why_it_does_not_fit(chapter: bytes, suggestion: QaSuggestion) -> str:
    """Name what keeps a suggestion out of its chapter.

    A fragment still found in its paragraph means the text has not changed:
    calling the chapter changed would send the user after an edit nobody made.
    """
    try:
        model = build_html_document_model(
            chapter.decode("utf-8"), document_id=suggestion.chapter_id
        )
        blocks = build_translation_payload(model)["blocks"]
    except ValueError:
        return "глава изменилась после проверки"
    block = next((item for item in blocks if item.get("id") == suggestion.block_id), None)
    text = flatten_visible_text(block["inlines"])[0] if block is not None else ""
    found = text.count(suggestion.original_text) if suggestion.original_text.strip() else 0
    if found > 1:
        return "фрагмент встречается в абзаце не один раз"
    if found == 1:
        return "фрагмент задевает оформление текста, например курсив"
    return "глава изменилась после проверки"


# How much already-translated prose the repairer may see on each side of a gap.
# Three sentences is enough to carry the scene's terminology and tone, and the
# character cap keeps a chapter of long paragraphs from crowding out the gap
# itself in the prompt.
_STYLE_WINDOW_UNITS = 3
_STYLE_WINDOW_CHARS = 600


def _style_windows(
    item: VerifiedCandidate, ordered_units: Sequence[SemanticUnit]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the translated sentences around the gap, for style and terms only.

    The anchors alone are two sentences; a model given only those invents its
    own wording for a scene the book already has words for.
    """
    left_anchor = item.candidate.left_anchor
    right_anchor = item.candidate.right_anchor
    if not ordered_units or left_anchor is None or right_anchor is None:
        return ((), ())
    positions = {unit.unit_id: index for index, unit in enumerate(ordered_units)}
    left_id = left_anchor.target_unit_ids[-1] if left_anchor.target_unit_ids else ""
    right_id = right_anchor.target_unit_ids[0] if right_anchor.target_unit_ids else ""
    left_index = positions.get(left_id)
    right_index = positions.get(right_id)
    if left_index is None or right_index is None:
        return ((), ())
    before = _window_texts(ordered_units, left_index - _STYLE_WINDOW_UNITS, left_index)
    after = _window_texts(
        ordered_units, right_index + 1, right_index + 1 + _STYLE_WINDOW_UNITS
    )
    return before, after


def _window_texts(
    ordered_units: Sequence[SemanticUnit], start: int, stop: int
) -> tuple[str, ...]:
    """Collect unit texts in reading order, within the character budget."""
    texts: list[str] = []
    budget = _STYLE_WINDOW_CHARS
    for index in range(max(0, start), min(len(ordered_units), stop)):
        text = ordered_units[index].text.strip()
        if not text or len(text) > budget:
            continue
        texts.append(text)
        budget -= len(text)
    return tuple(texts)


def _patch_for(
    item: VerifiedCandidate,
    units: Mapping[str, SemanticUnit],
    model: dict,
    chapter_id: str,
    fragment: str,
) -> StructuralPatch | None:
    """Bind one verified candidate to an exact insertion point in the chapter."""
    left_anchor = item.candidate.left_anchor
    right_anchor = item.candidate.right_anchor
    if left_anchor is None or right_anchor is None:
        return None
    if not left_anchor.target_unit_ids or not right_anchor.target_unit_ids:
        return None
    left_unit = units.get(left_anchor.target_unit_ids[-1])
    right_unit = units.get(right_anchor.target_unit_ids[0])
    if left_unit is None or right_unit is None:
        return None
    identity = "\x1f".join((chapter_id, item.candidate.candidate_id, fragment))
    return StructuralPatch(
        patch_id="qa" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
        chapter_id=chapter_id,
        expected_fingerprint=document_fingerprint(model),
        left_anchor_unit_id=left_unit.unit_id,
        right_anchor_unit_id=right_unit.unit_id,
        parent_block_id=left_unit.block_id,
        translated_fragment=fragment,
    )


def chapter_fingerprint(path: Path | str) -> str:
    """Identify the chapter's text as it stands, or "" when it cannot be read.

    Recorded after a check so a later pass can tell "this is the same chapter I
    already answered for" from "this chapter has changed since".  An unreadable
    file yields no fingerprint, which reads as "unknown" and re-checks.
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _risk(
    verified: Sequence[VerifiedCandidate],
    repairs: Sequence[OmissionRepairOutcome],
    additions: Sequence[AdditionCandidate],
    coverage: CoverageAnalysis | None,
    warnings: Sequence[str],
) -> tuple[RiskLevel, bool]:
    """Classify the chapter and decide whether translation may continue."""
    fixed = {
        repair.candidate_id
        for repair in repairs
        if repair.decision is Decision.FIXED
    }
    unresolved = [
        item
        for item in verified
        if item.eligible_for_repair and item.candidate.candidate_id not in fixed
    ]
    blocking_additions = [addition for addition in additions if addition.blocks_gate]
    if unresolved or blocking_additions:
        return RiskLevel.HIGH, False
    if coverage is None:
        return RiskLevel.MEDIUM, True
    reported = [item for item in verified if item.verdict is not None]
    if coverage.mode != SEMANTIC_ALIGNMENT_MODE or warnings or reported or additions:
        return RiskLevel.MEDIUM, True
    return RiskLevel.LOW, True

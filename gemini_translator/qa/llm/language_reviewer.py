"""One diagnosis request per chunk, with external hints as evidence only."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from ..language_validation import (
    LanguageBlock,
    LanguageQaRequest,
    LanguageReviewError,
    LanguageRuleIssue,
    RussianNlpAnalysis,
)
from ..glossary_terms import glossary_named_in
from ..models import OmissionRepairerConfig
from .completion import QaCompletionClient
from .json_response import QaResponseSchemaError
from .prompts import (
    PromptConfigurationError,
    escaped,
    load_prompt_template,
    render_prompt,
)
from .schemas import LanguageIssue


DIAGNOSIS_PURPOSE = "language_diagnosis"


class LanguageQualityReviewer:
    """Diagnose the local language defects of one chunk in a single request.

    LanguageTool rules and Russian NLP evidence enter this one request as
    explicitly unconfirmed hints. They never trigger requests of their own and
    never authorize an edit the model did not itself diagnose.
    """

    def __init__(
        self,
        client: QaCompletionClient,
        config: OmissionRepairerConfig | None = None,
        cache=None,
    ) -> None:
        if not callable(getattr(client, "complete_json", None)):
            raise TypeError("client must implement complete_json")
        self._client = client
        # The ceiling QA allows, on purpose.  The answer lists every defect
        # found, so its length follows the number of defects, not the size of
        # the text — and a truncated answer costs a whole chunk.
        self._config = config or OmissionRepairerConfig(
            max_output_tokens=4096, prompt_version="language_diagnosis_v2"
        )
        # Diagnosis is the one QA request that is asked again about text nobody
        # touched: a deferred chapter retried, a manual re-check, a resumed
        # session.  The cache is optional and never required to be correct.
        self._cache = cache

    async def diagnose_chapter(
        self,
        request: LanguageQaRequest,
        blocks: Sequence[LanguageBlock],
        rule_candidates: Sequence[LanguageRuleIssue] = (),
        nlp_analysis: RussianNlpAnalysis | None = None,
    ) -> tuple[LanguageIssue, ...]:
        """Return every diagnosed issue of one chunk, or raise a typed refusal."""
        if not isinstance(request, LanguageQaRequest):
            raise TypeError("request must be a LanguageQaRequest")
        request.cancellation.raise_if_cancelled()
        try:
            template = load_prompt_template(
                self._config.prompt_path, self._config.prompt_version
            )
        except PromptConfigurationError:
            raise LanguageReviewError("prompt_configuration_unavailable") from None

        lines = _diagnosis_lines(request, blocks, rule_candidates, nlp_analysis)
        digest = self._digest(lines, request)
        payload = self._cached_answer(digest)
        from_cache = payload is not None
        if not from_cache:
            payload = await request_qa_json(
                self._client,
                render_prompt(template, lines),
                request,
                self._config.max_output_tokens,
                DIAGNOSIS_PURPOSE,
            )
        try:
            if not isinstance(payload, Mapping):
                raise QaResponseSchemaError("diagnosis result must be an object")
            unknown = set(payload) - {"issues", "metadata"}
            if unknown or "issues" not in payload:
                raise QaResponseSchemaError("diagnosis result must contain issues")
            raw_issues = payload["issues"]
            if not isinstance(raw_issues, (list, tuple)):
                raise QaResponseSchemaError("issues must be an array")
            issues = tuple(LanguageIssue.from_dict(item) for item in raw_issues)
        except (QaResponseSchemaError, TypeError, ValueError):
            raise LanguageReviewError("diagnosis_invalid_response") from None

        known_blocks = {block.block_id for block in blocks}
        if any(issue.block_id not in known_blocks for issue in issues):
            raise LanguageReviewError("diagnosis_block_mismatch")
        if len({issue.issue_id for issue in issues}) != len(issues):
            raise LanguageReviewError("diagnosis_duplicate_issue_ids")
        if not from_cache:
            # Stored only after it passed every check a fresh answer passes.
            self._store_answer(digest, payload)
        return issues

    def _digest(self, lines: Sequence[str], request: LanguageQaRequest) -> str:
        """Key on the data and the instruction version, never on the rendered prompt.

        The prompt carries a per-call random boundary tag, so two identical
        questions render to two different strings by design.
        """
        if self._cache is None:
            return ""
        from .answer_cache import answer_digest

        model = request.model
        return answer_digest(
            "\n".join(lines),
            self._config.prompt_version,
            model.provider,
            model.model,
        )

    def _cached_answer(self, digest: str) -> object | None:
        if self._cache is None or not digest:
            return None
        try:
            return self._cache.get(digest)
        except Exception:  # noqa: BLE001 - a broken cache is simply a miss
            return None

    def _store_answer(self, digest: str, payload: object) -> None:
        if self._cache is None or not digest:
            return
        try:
            self._cache.put(digest, payload)
        except Exception:  # noqa: BLE001 - storing never fails a check
            return


# Retrying is part of the contract, not a nicety.  Measured on a live book: one
# 429 on the diagnosis request left 33 of 40 chapters unchecked, and a chapter
# nobody could check reports no defects — exactly what a clean chapter reports.
# A transient failure therefore keeps the current request in work until it
# succeeds or the user cancels QA, just as translation keeps its current chunk.
RETRY_BASE_DELAY_SECONDS = 1.5
RETRY_MAX_DELAY_SECONDS = 20.0
# The longest a single pause may be when the service names its own delay.  An
# overloaded server asks for twenty seconds; asking again after one and a half
# only gets the same answer.
RETRY_MAX_WAIT_SECONDS = 90.0


def retry_delay(attempt: int) -> float:
    """Grow the pause with each attempt, up to a fixed ceiling."""
    delay = RETRY_BASE_DELAY_SECONDS
    for _ in range(max(0, attempt)):
        delay = min(delay * 2, RETRY_MAX_DELAY_SECONDS)
        if delay >= RETRY_MAX_DELAY_SECONDS:
            break
    return delay


def requested_delay(error: BaseException | None) -> float:
    """The longest pause anyone in the error's chain asked for, or zero."""
    longest = 0.0
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        value = getattr(current, "delay_seconds", None)
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            longest = max(longest, float(value))
        current = current.__cause__ or current.__context__
    return longest


def pause_before_retry(attempt: int, error: BaseException | None) -> float:
    """The backoff, or the delay the service itself asked for when it is longer."""
    requested = requested_delay(error)
    return min(max(retry_delay(attempt), requested), RETRY_MAX_WAIT_SECONDS)


def _asks_for_a_pause(error: BaseException) -> bool:
    delay = getattr(error, "delay_seconds", None)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        return False
    return delay > 0


def is_transient(error: BaseException) -> bool:
    """Report whether the service itself asked to be tried again later.

    Handlers state this by carrying the delay they want: a busy service and a
    dropped connection say ``delay_seconds``, an exhausted quota, a refused
    prompt and an unknown model do not.  Reading that attribute keeps this layer
    free of the handler exception hierarchy while still following its rules.

    The whole chain is read, not just the top error.  Measured on two finished
    books: a key pool that has run out of ready keys reports «сервис просит
    ждать дольше, чем может одна проверка» as its own error and keeps the
    original — which does carry the 59 seconds the service asked for — only as
    the cause.  Reading the top object alone called that permanent and threw
    away every edit of the batch, for a pause this layer would gladly have
    waited out.
    """
    if isinstance(error, TimeoutError):
        return True
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError) or _asks_for_a_pause(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_truncated(error: BaseException) -> bool:
    """Report whether the answer was cut off by the output limit.

    A handler says so with the text it did receive (``partial_text``), the way
    PartialGenerationError does for finishReason MAX_TOKENS.  Read by attribute
    for the same reason as ``is_transient``: this layer stays free of the
    handler exception hierarchy.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "partial_text", None) is not None:
            return True
        current = current.__cause__ or current.__context__
    return False


async def request_qa_json(
    client: QaCompletionClient,
    prompt: str,
    request: LanguageQaRequest,
    max_output_tokens: int,
    purpose: str,
    *,
    sleep=asyncio.sleep,
) -> object:
    """Send one QA request, keeping transient work queued until success or cancel."""
    attempt = 0
    while True:
        try:
            return await client.complete_json(
                prompt,
                model=request.model,
                max_output_tokens=max_output_tokens,
                cancellation=request.cancellation,
                purpose=purpose,
            )
        except asyncio.CancelledError:
            raise
        except QaResponseSchemaError:
            raise LanguageReviewError(f"{purpose}_invalid_response") from None
        except Exception as error:  # noqa: BLE001 - classified just below
            if is_truncated(error):
                # The answer ran into the output limit: the same «too big a
                # question» a broken JSON says, so the caller splits the chunk.
                raise LanguageReviewError(f"{purpose}_invalid_response") from None
            if not is_transient(error):
                detail = f"{type(error).__name__}: {error}"
                raise LanguageReviewError(f"{purpose}_failed", detail) from None
            failure = error
        # A cancelled check must not spend its last seconds sleeping.
        request.cancellation.raise_if_cancelled()
        await sleep(pause_before_retry(attempt, failure))
        attempt += 1


def _diagnosis_lines(
    request: LanguageQaRequest,
    blocks: Sequence[LanguageBlock],
    rule_candidates: Sequence[LanguageRuleIssue],
    nlp_analysis: RussianNlpAnalysis | None,
) -> list[str]:
    lines = [
        f"chapter_id: {escaped(request.chapter_id)}",
        f"source_language: {escaped(request.source_language)}",
        f"target_language: {escaped(request.target_language)}",
        "translated_blocks:",
    ]
    for block in blocks:
        lines.append(f"- block_id: {escaped(block.block_id)}")
        lines.append(f"  text: {escaped(block.text)}")
        source_text = request.source_text_by_block.get(block.block_id, "")
        if source_text:
            lines.append(f"  source_text: {escaped(source_text)}")
    _section(
        lines,
        "unconfirmed_rule_hints",
        [
            f"- block_id: {escaped(rule.block_id)} | rule: {escaped(rule.rule_id)}"
            f" | message: {escaped(rule.message)} | span: {escaped(rule.original_text)}"
            f" | suggestions: {escaped(', '.join(rule.replacements))}"
            for rule in rule_candidates
        ],
    )
    entities = nlp_analysis.entities if nlp_analysis else ()
    _section(
        lines,
        "protected_entities",
        [
            f"- block_id: {escaped(entity.block_id)} | {escaped(entity.category)}:"
            f" {escaped(entity.text)}"
            for entity in entities
        ],
    )
    candidates = nlp_analysis.syntax_candidates if nlp_analysis else ()
    _section(
        lines,
        "unconfirmed_syntax_hints",
        [
            f"- block_id: {escaped(candidate.block_id)} |"
            f" span: {escaped(candidate.original_text)} |"
            f" reason: {escaped(candidate.reason)}"
            for candidate in candidates
        ],
    )
    _section(
        lines,
        "glossary",
        [
            f"- {escaped(term.original_term)} → {escaped(term.canonical_translation)}"
            f" | policy={term.policy.value}"
            for term in glossary_named_in(
                request.glossary,
                " ".join(request.source_text_by_block.get(block.block_id, "") for block in blocks),
            )
        ],
    )
    return lines


def _section(lines: list[str], header: str, items: Sequence[str]) -> None:
    """Append one named evidence section, explicit about being empty."""
    lines.append(f"{header}:")
    lines.extend(items or ("- none supplied",))

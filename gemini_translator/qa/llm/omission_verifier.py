"""Conservative LLM verification for source-side semantic omission candidates."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from ..foreign_text_filter import ForeignTextFilter
from ..models import (
    CandidateContext,
    GapCandidate,
    GlossaryRule,
    OmissionVerifierConfig,
    QaModelValidationError,
    RelevantGlossaryTerm,
    VerifiedCandidate,
)
from .completion import CancellationToken, QaCompletionClient, QaModelSelection
from .json_response import QaResponseSchemaError
from .prompts import PromptConfigurationError, escaped, load_prompt_template, render_prompt
from .schemas import OmissionVerdict


VERIFICATION_PURPOSE = "omission_verification"
# Several candidates of one chapter go in one request.  Measured on a real book:
# 3.8 verification requests a chapter, 98 % of them answering «not an
# omission», with the same instruction repeated in each and taking 38 % of it.
BATCH_PROMPT_VERSION = "omission_verifier_batch_v1"
MAX_CANDIDATES_PER_REQUEST = 6
MAX_BATCH_OUTPUT_TOKENS = 4096


class OmissionVerifier:
    """Verify one bounded candidate without granting authority on weak evidence."""

    def __init__(
        self,
        client: QaCompletionClient,
        config: OmissionVerifierConfig | None = None,
        cache=None,
    ) -> None:
        if not callable(getattr(client, "complete_json", None)):
            raise TypeError("client must implement complete_json")
        if config is not None and not isinstance(config, OmissionVerifierConfig):
            raise TypeError("config must be an OmissionVerifierConfig")
        self._client = client
        self._config = config or OmissionVerifierConfig()
        # A re-check asks the same questions about an unchanged chapter; the
        # cache is optional and never required to be correct.
        self._cache = cache

    async def verify(
        self,
        candidate: GapCandidate,
        context: CandidateContext,
        glossary: Sequence[RelevantGlossaryTerm],
        model: QaModelSelection,
        cancellation: CancellationToken,
    ) -> VerifiedCandidate:
        self._validate_request(candidate, context, glossary, model, cancellation)
        cancellation.raise_if_cancelled()

        filter_decision = _foreign_text_decision(candidate, context, glossary)
        if filter_decision.action != "send_to_llm_verifier":
            return _filtered(candidate, context, filter_decision)

        try:
            template = load_prompt_template(
                self._config.prompt_path, self._config.prompt_version
            )
            prompt = _build_prompt(template, candidate, context, glossary)
        except PromptConfigurationError:
            return VerifiedCandidate(
                candidate=candidate,
                context=context,
                verdict=None,
                foreign_text_decision=filter_decision,
                eligible_for_repair=False,
                status="configuration_failed",
                warnings=("prompt_configuration_unavailable",),
            )

        cancellation.raise_if_cancelled()
        try:
            payload = await self._client.complete_json(
                prompt,
                model=model,
                max_output_tokens=self._config.max_output_tokens,
                cancellation=cancellation,
                purpose=VERIFICATION_PURPOSE,
            )
        except asyncio.CancelledError:
            raise
        except QaResponseSchemaError:
            return self._failure_result(
                candidate, context, filter_decision, "invalid_response"
            )
        except TimeoutError:
            return self._failure_result(
                candidate, context, filter_decision, "completion_timeout"
            )
        except Exception:
            return self._failure_result(
                candidate, context, filter_decision, "completion_failed"
            )

        try:
            if not isinstance(payload, Mapping):
                raise QaResponseSchemaError("completion result must be an object")
            verdict = OmissionVerdict.from_dict(payload)
        except (QaResponseSchemaError, TypeError, ValueError):
            return self._failure_result(
                candidate, context, filter_decision, "invalid_response"
            )

        return self._judged(candidate, context, filter_decision, verdict)

    async def verify_many(
        self,
        items: Sequence[
            tuple[GapCandidate, CandidateContext, Sequence[RelevantGlossaryTerm]]
        ],
        model: QaModelSelection,
        cancellation: CancellationToken,
    ) -> tuple[VerifiedCandidate, ...]:
        """Verify the candidates of one chapter, several to a request.

        Each candidate keeps its own evidence and its own glossary, and gets the
        same fail-closed verdict ``verify`` would give it: a candidate the answer
        leaves out is refused, never assumed covered.
        """
        results: dict[str, VerifiedCandidate] = {}
        asking = []
        for candidate, context, glossary in items:
            self._validate_request(candidate, context, glossary, model, cancellation)
            decision = _foreign_text_decision(candidate, context, glossary)
            if decision.action != "send_to_llm_verifier":
                results[candidate.candidate_id] = _filtered(candidate, context, decision)
            else:
                asking.append((candidate, context, glossary, decision))
        for start in range(0, len(asking), MAX_CANDIDATES_PER_REQUEST):
            batch = asking[start : start + MAX_CANDIDATES_PER_REQUEST]
            for result in await self._verify_batch(batch, model, cancellation):
                results[result.candidate.candidate_id] = result
        return tuple(results[candidate.candidate_id] for candidate, _, _ in items)

    async def _verify_batch(self, batch, model, cancellation) -> list[VerifiedCandidate]:
        cancellation.raise_if_cancelled()

        def everyone(status: str) -> list[VerifiedCandidate]:
            return [
                self._failure_result(candidate, context, decision, status)
                for candidate, context, _, decision in batch
            ]

        try:
            template = load_prompt_template(self._config.prompt_path, BATCH_PROMPT_VERSION)
        except PromptConfigurationError:
            return [
                VerifiedCandidate(
                    candidate=candidate,
                    context=context,
                    verdict=None,
                    foreign_text_decision=decision,
                    eligible_for_repair=False,
                    status="configuration_failed",
                    warnings=("prompt_configuration_unavailable",),
                )
                for candidate, context, _, decision in batch
            ]
        lines: list[str] = []
        for candidate, context, glossary, _ in batch:
            lines.append("- candidate:")
            lines.extend("  " + line for line in _candidate_lines(candidate, context, glossary))
        digest = self._digest(lines, model)
        payload = self._cached_answer(digest)
        from_cache = payload is not None
        if not from_cache:
            cancellation.raise_if_cancelled()
            try:
                payload = await self._client.complete_json(
                    render_prompt(template, lines),
                    model=model,
                    max_output_tokens=min(
                        MAX_BATCH_OUTPUT_TOKENS,
                        self._config.max_output_tokens * len(batch),
                    ),
                    cancellation=cancellation,
                    purpose=VERIFICATION_PURPOSE,
                )
            except asyncio.CancelledError:
                raise
            except QaResponseSchemaError:
                return everyone("invalid_response")
            except TimeoutError:
                return everyone("completion_timeout")
            except Exception:
                return everyone("completion_failed")

        verdicts = _batch_verdicts(payload)
        results: list[VerifiedCandidate] = []
        for candidate, context, _, decision in batch:
            verdict = verdicts.get(candidate.candidate_id)
            if verdict is None:
                results.append(
                    self._failure_result(candidate, context, decision, "invalid_response")
                )
            else:
                results.append(self._judged(candidate, context, decision, verdict))
        if not from_cache and len(verdicts) == len(batch):
            # Stored only when it answered every candidate it was asked about.
            self._store_answer(digest, payload)
        return results

    def _digest(self, lines: Sequence[str], model: QaModelSelection) -> str:
        if self._cache is None:
            return ""
        from .answer_cache import answer_digest

        return answer_digest("\n".join(lines), BATCH_PROMPT_VERSION, model.provider, model.model)

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

    def _judged(
        self,
        candidate: GapCandidate,
        context: CandidateContext,
        filter_decision,
        verdict: OmissionVerdict,
    ) -> VerifiedCandidate:
        ids_match = set(verdict.source_unit_ids).issubset(candidate.source_unit_ids)
        if not ids_match:
            return VerifiedCandidate(
                candidate=candidate,
                context=context,
                verdict=verdict,
                foreign_text_decision=filter_decision,
                eligible_for_repair=False,
                status="identity_mismatch",
                warnings=("verdict_source_unit_ids_mismatch",),
            )

        eligible = (
            candidate.repairable
            and candidate.side == "source"
            and candidate.left_anchor is not None
            and candidate.right_anchor is not None
            and candidate.signals == ("missing_in_target",)
            and filter_decision.action == "send_to_llm_verifier"
            and verdict.decision == "missing_content"
            and verdict.confidence >= self._config.high_confidence
            and bool(verdict.missing_facts)
            and ids_match
        )
        return VerifiedCandidate(
            candidate=candidate,
            context=context,
            verdict=verdict,
            foreign_text_decision=filter_decision,
            eligible_for_repair=eligible,
            status="verified",
        )

    @staticmethod
    def _validate_request(
        candidate: GapCandidate,
        context: CandidateContext,
        glossary: Sequence[RelevantGlossaryTerm],
        model: QaModelSelection,
        cancellation: CancellationToken,
    ) -> None:
        if not isinstance(candidate, GapCandidate):
            raise TypeError("candidate must be a GapCandidate")
        if not isinstance(context, CandidateContext):
            raise TypeError("context must be a CandidateContext")
        if context.candidate_id != candidate.candidate_id:
            raise QaModelValidationError("candidate and context identities must match")
        if isinstance(glossary, (str, bytes)) or not isinstance(glossary, Sequence):
            raise TypeError("glossary must be a sequence")
        if not all(isinstance(term, RelevantGlossaryTerm) for term in glossary):
            raise TypeError("glossary entries must be RelevantGlossaryTerm")
        if not isinstance(model, QaModelSelection):
            raise TypeError("model must be a QaModelSelection")
        if not callable(getattr(cancellation, "raise_if_cancelled", None)):
            raise TypeError("cancellation must implement raise_if_cancelled")

    @staticmethod
    def _failure_result(
        candidate: GapCandidate,
        context: CandidateContext,
        filter_decision,
        status: str,
    ) -> VerifiedCandidate:
        return VerifiedCandidate(
            candidate=candidate,
            context=context,
            verdict=None,
            foreign_text_decision=filter_decision,
            eligible_for_repair=False,
            status=status,
            warnings=(status,),
        )


def _foreign_text_decision(
    candidate: GapCandidate,
    context: CandidateContext,
    glossary: Sequence[RelevantGlossaryTerm],
):
    rules = tuple(GlossaryRule(term.original_term, term.policy) for term in glossary)
    return ForeignTextFilter(rules).classify(candidate, context)


def _filtered(candidate: GapCandidate, context: CandidateContext, decision) -> VerifiedCandidate:
    return VerifiedCandidate(
        candidate=candidate,
        context=context,
        verdict=None,
        foreign_text_decision=decision,
        eligible_for_repair=False,
        status="filtered",
        warnings=("foreign_text_filtered",),
    )


def _batch_verdicts(payload: object) -> dict[str, OmissionVerdict]:
    """Every well-formed verdict of a batch answer, by candidate.

    A malformed entry, or a candidate answered twice, simply has no verdict:
    that candidate is refused and the others keep theirs.
    """
    if not isinstance(payload, Mapping):
        return {}
    entries = payload.get("verdicts")
    if not isinstance(entries, (list, tuple)):
        return {}
    verdicts: dict[str, OmissionVerdict] = {}
    answered_twice: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        candidate_id = entry.get("candidate_id")
        if not isinstance(candidate_id, str):
            continue
        try:
            verdict = OmissionVerdict.from_dict(
                {key: value for key, value in entry.items() if key != "candidate_id"}
            )
        except (QaResponseSchemaError, TypeError, ValueError):
            continue
        if candidate_id in verdicts:
            answered_twice.add(candidate_id)
        verdicts[candidate_id] = verdict
    for candidate_id in answered_twice:
        verdicts.pop(candidate_id, None)
    return verdicts


def _build_prompt(
    template: str,
    candidate: GapCandidate,
    context: CandidateContext,
    glossary: Sequence[RelevantGlossaryTerm],
) -> str:
    return render_prompt(template, _candidate_lines(candidate, context, glossary))


def _candidate_lines(
    candidate: GapCandidate,
    context: CandidateContext,
    glossary: Sequence[RelevantGlossaryTerm],
) -> list[str]:
    source_ids = ", ".join(escaped(value) for value in candidate.source_unit_ids)
    signals = ", ".join(escaped(value) for value in candidate.signals)
    lines = [
        f"candidate_id: {escaped(candidate.candidate_id)}",
        f"side: {escaped(candidate.side)}",
        f"source_unit_ids: {source_ids}",
        f"signals: {signals}",
        f"source_language: {escaped(context.source_language)}",
        f"target_language: {escaped(context.target_language)}",
        f"candidate_language: {escaped(context.candidate_language)}",
        f"source_gap: {escaped(context.source_text)}",
        f"target_local_text: {escaped(context.target_text)}",
        f"left_source_anchor: {escaped(context.source_before)}",
        f"right_source_anchor: {escaped(context.source_after)}",
        f"left_target_anchor: {escaped(context.target_before)}",
        f"right_target_anchor: {escaped(context.target_after)}",
        "relevant_glossary:",
    ]
    if glossary:
        lines.extend(
            "- "
            + escaped(term.original_term)
            + " → "
            + escaped(term.canonical_translation)
            + f" | policy={term.policy.value}"
            + f" | occurrences={term.occurrences}"
            + f" | priority={term.priority}"
            for term in glossary
        )
    else:
        lines.append("- none supplied")
    return lines

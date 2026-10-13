"""Versioned JSON persistence for durable translation-QA history."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .models import (
    ChapterMetrics,
    GlossaryObservation,
    QaChapterState,
    QaJournalEntry,
    QaModelValidationError,
    QaSuggestion,
)

if TYPE_CHECKING:  # pragma: no cover - import kept out of the startup path
    import pandas as pd


class QaJournalError(ValueError):
    """Base error for journal data that cannot safely be used."""


class QaJournalCorruptedError(QaJournalError):
    """Raised for malformed or structurally invalid persisted JSON."""


class QaJournalUnsupportedVersionError(QaJournalError):
    """Raised when a journal uses a schema version this code cannot read."""


class QaJournal:
    SCHEMA_VERSION = 4
    _V1_ROOT_KEYS = frozenset(
        {
            "schema_version",
            "book_id",
            "updated_at",
            "metrics",
            "candidates",
            "repairs",
            "glossary_observations",
        }
    )
    # v2 adds the per-chapter check state the final book pass selects on.
    _V2_ROOT_KEYS = _V1_ROOT_KEYS | {"chapter_states"}
    # v3 keeps the language fixes a check refused, for a person to decide.
    _ROOT_KEYS = _V2_ROOT_KEYS | {"suggestions"}
    # v4 keeps v3's root; each suggestion gains CometKiwi's two scores.
    _ROOT_KEYS_BY_VERSION = {
        1: _V1_ROOT_KEYS,
        2: _V2_ROOT_KEYS,
        3: _ROOT_KEYS,
        4: _ROOT_KEYS,
    }

    def __init__(
        self,
        *,
        book_id: str,
        updated_at: str,
        metrics: Mapping[str, ChapterMetrics] | None = None,
        candidates: list[dict[str, Any]] | None = None,
        repairs: list[dict[str, Any]] | None = None,
        glossary_observations: Iterable[GlossaryObservation] | None = None,
        chapter_states: Mapping[str, QaChapterState] | None = None,
        suggestions: Iterable[QaSuggestion] | None = None,
    ) -> None:
        self.book_id = book_id
        self.updated_at = updated_at
        self.metrics = dict(metrics or {})
        self.candidates = list(candidates or [])
        self.repairs = list(repairs or [])
        self.chapter_states = dict(chapter_states or {})
        self.suggestions = list(suggestions or [])
        if any(not isinstance(item, QaSuggestion) for item in self.suggestions):
            raise QaJournalError("suggestions must use the typed schema")
        self.glossary_observations = list(glossary_observations or [])
        if any(
            not isinstance(observation, GlossaryObservation)
            for observation in self.glossary_observations
        ):
            raise QaJournalError("glossary observations must use the typed schema")

    @classmethod
    def empty(cls, *, book_id: str) -> "QaJournal":
        return cls(book_id=book_id, updated_at=datetime.now().astimezone().isoformat())

    @classmethod
    def load(cls, path: Path) -> "QaJournal":
        try:
            with Path(path).open("r", encoding="utf-8") as stream:
                payload = json.load(stream, parse_constant=_reject_json_constant)
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise QaJournalCorruptedError(f"Cannot read QA journal: {path}") from exc

        if not isinstance(payload, dict):
            raise QaJournalCorruptedError("QA journal root must be an object")
        if "schema_version" not in payload or isinstance(
            payload["schema_version"], bool
        ) or not isinstance(
            payload["schema_version"], int
        ):
            raise QaJournalCorruptedError("QA journal has no valid schema version")
        version = payload["schema_version"]
        expected_keys = cls._ROOT_KEYS_BY_VERSION.get(version)
        if expected_keys is None:
            raise QaJournalUnsupportedVersionError(
                f"Unsupported QA journal schema version: {version}"
            )
        if set(payload) != expected_keys:
            raise QaJournalCorruptedError("QA journal has an invalid root schema")

        required_lists = ["metrics", "candidates", "repairs", "glossary_observations"]
        if version >= 3:
            required_lists.append("suggestions")
        if not isinstance(payload.get("book_id"), str) or not isinstance(
            payload.get("updated_at"), str
        ):
            raise QaJournalCorruptedError("QA journal is missing book metadata")
        if any(not isinstance(payload.get(name), list) for name in required_lists):
            raise QaJournalCorruptedError("QA journal contains invalid collections")

        try:
            metrics = {
                item["chapter_id"]: ChapterMetrics.from_dict(item)
                for item in payload["metrics"]
                if isinstance(item, dict)
            }
            if len(metrics) != len(payload["metrics"]):
                raise ValueError("metrics entries must be objects")
            candidates = _validated_object_entries(payload["candidates"])
            repairs = _validated_object_entries(payload["repairs"])
            glossary_observations = [
                GlossaryObservation.from_dict(item)
                for item in payload["glossary_observations"]
            ]
            # A v1 journal simply has no recorded states: every chapter is then
            # treated as never checked by the final pass, which is the safe side.
            states = [
                QaChapterState.from_dict(item)
                for item in payload.get("chapter_states", [])
            ]
            # Versions 1 and 2 kept no suggestions; they simply start empty.
            # Version 3 kept them without scores, and they come back unscored.
            read_suggestion = (
                QaSuggestion.from_dict
                if version >= 4
                else QaSuggestion.from_version_3_dict
            )
            suggestions = [
                read_suggestion(item) for item in payload.get("suggestions", [])
            ]
        except (KeyError, ValueError, QaModelValidationError) as exc:
            raise QaJournalCorruptedError("QA journal metrics are invalid") from exc

        return cls(
            book_id=payload["book_id"],
            updated_at=payload["updated_at"],
            metrics=metrics,
            candidates=candidates,
            repairs=repairs,
            glossary_observations=glossary_observations,
            chapter_states={state.chapter_id: state for state in states},
            suggestions=suggestions,
        )

    def append(self, entry: QaJournalEntry) -> None:
        self.candidates.append(entry.to_dict())
        self._mark_updated()

    def upsert_metrics(self, metrics: ChapterMetrics) -> None:
        self.metrics[metrics.chapter_id] = metrics
        self._mark_updated()

    def append_glossary_observation(self, observation: GlossaryObservation) -> None:
        if not isinstance(observation, GlossaryObservation):
            raise QaJournalError("glossary observation must use the typed schema")
        self.glossary_observations.append(observation)
        self._mark_updated()

    def append_repair(self, record: Mapping[str, Any]) -> None:
        """Record one applied repair so a restart never repeats it."""
        if not isinstance(record, Mapping):
            raise QaJournalError("repair record must be a mapping")
        entry = {str(key): value for key, value in record.items()}
        if not isinstance(entry.get("patch_id"), str) or not entry["patch_id"].strip():
            raise QaJournalError("repair record needs a patch_id")
        if any(item.get("patch_id") == entry["patch_id"] for item in self.repairs):
            return
        self.repairs.append(entry)
        self._mark_updated()

    def record_chapter_state(self, state: QaChapterState) -> None:
        """Remember how and under what rules one chapter was last checked."""
        if not isinstance(state, QaChapterState):
            raise QaJournalError("chapter state must use the typed schema")
        self.chapter_states[state.chapter_id] = state
        self._mark_updated()

    def suggestion(self, suggestion_id: str) -> QaSuggestion | None:
        """Find one suggestion by its id, whatever its state."""
        return next(
            (item for item in self.suggestions if item.suggestion_id == suggestion_id),
            None,
        )

    def replace_suggestions(
        self, chapter_id: str, suggestions: Iterable[QaSuggestion]
    ) -> None:
        """Swap a chapter's undecided suggestions for a new pass's own.

        What a person already decided stays decided: an applied or dismissed
        suggestion never comes back, even when the new pass proposes it again.
        """
        fresh = list(suggestions)
        if any(
            not isinstance(item, QaSuggestion) or item.chapter_id != chapter_id
            for item in fresh
        ):
            raise QaJournalError("suggestions must be typed and belong to the chapter")
        decided = {
            item.suggestion_id
            for item in self.suggestions
            if item.chapter_id == chapter_id and not item.awaits_decision
        }
        kept = [
            item
            for item in self.suggestions
            if item.chapter_id != chapter_id or not item.awaits_decision
        ]
        seen: set[str] = set()
        for item in fresh:
            if item.suggestion_id in decided or item.suggestion_id in seen:
                continue
            seen.add(item.suggestion_id)
            kept.append(item)
        self.suggestions = kept
        self._mark_updated()

    def set_suggestion_status(
        self, suggestion_id: str, status: str, note: str = ""
    ) -> QaSuggestion:
        """Record a person's decision, or why a suggestion no longer applies."""
        for index, item in enumerate(self.suggestions):
            if item.suggestion_id == suggestion_id:
                updated = replace(item, status=status, status_note=note)
                self.suggestions[index] = updated
                self._mark_updated()
                return updated
        raise QaJournalError(f"unknown suggestion: {suggestion_id}")

    def set_suggestion_scores(
        self, suggestion_id: str, before: float, after: float
    ) -> QaSuggestion | None:
        """Add CometKiwi's two scores to a fix, whatever has been decided about it since.

        The scores arrive after the pass recorded the fix. A new pass may have
        replaced it meanwhile, and then there is nothing to add them to.
        """
        for index, item in enumerate(self.suggestions):
            if item.suggestion_id == suggestion_id:
                updated = replace(item, score_before=before, score_after=after)
                self.suggestions[index] = updated
                self._mark_updated()
                return updated
        return None

    def record_chapter_result(
        self,
        *,
        metrics: ChapterMetrics | None = None,
        entries: Iterable[QaJournalEntry] = (),
        repairs: Iterable[Mapping[str, Any]] = (),
        state: QaChapterState | None = None,
        suggestions: Iterable[QaSuggestion] | None = None,
    ) -> None:
        """Fold one chapter QA pass into the journal in a single step.

        ``suggestions=None`` means the pass produced no verdict on them, so the
        chapter keeps what it had; any other value replaces its undecided ones.
        """
        if suggestions is not None:
            if state is None:
                raise QaJournalError("suggestions are recorded with the chapter state")
            suggestions = tuple(suggestions)
            if any(not isinstance(item, QaSuggestion) for item in suggestions):
                raise QaJournalError("suggestions must use the typed schema")
        if metrics is not None:
            self.upsert_metrics(metrics)
        if state is not None:
            self.record_chapter_state(state)
        for entry in entries:
            if not isinstance(entry, QaJournalEntry):
                raise QaJournalError("journal entries must use the typed schema")
            self.append(entry)
        for repair in repairs:
            self.append_repair(repair)
        if suggestions is not None:
            self.replace_suggestions(state.chapter_id, suggestions)

    def metrics_frame(self) -> pd.DataFrame:
        # Imported lazily: a session that never exports a metrics report
        # never pays pandas's startup cost.
        import pandas as pd

        rows = [
            self.metrics[chapter_id].to_dict() for chapter_id in sorted(self.metrics)
        ]
        return pd.DataFrame(rows, columns=ChapterMetrics.dataframe_columns())

    def save(self, path: Path) -> None:
        target = Path(path)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = self._payload()
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(
                    payload,
                    stream,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.SCHEMA_VERSION,
            "book_id": self.book_id,
            "updated_at": self.updated_at,
            "metrics": [
                self.metrics[chapter_id].to_dict() for chapter_id in sorted(self.metrics)
            ],
            "candidates": deepcopy(self.candidates),
            "chapter_states": [
                self.chapter_states[chapter_id].to_dict()
                for chapter_id in sorted(self.chapter_states)
            ],
            "repairs": deepcopy(self.repairs),
            "suggestions": [
                item.to_dict()
                for item in sorted(
                    self.suggestions,
                    key=lambda item: (item.chapter_id, item.created_at, item.suggestion_id),
                )
            ],
            "glossary_observations": [
                observation.to_dict() for observation in self.glossary_observations
            ],
        }

    def _mark_updated(self) -> None:
        self.updated_at = datetime.now().astimezone().isoformat()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Unsupported JSON numeric constant: {value}")


def _validated_object_entries(entries: list[Any]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("QA journal collection entries must be objects")
        if set(entry) == {"entry_id", "chapter_id", "decision"}:
            validated.append(QaJournalEntry.from_dict(entry).to_dict())
        else:
            validated.append(deepcopy(entry))
    return validated

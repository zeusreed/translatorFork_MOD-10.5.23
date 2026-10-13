"""Token usage of a provider call, scoped to its current async context."""

from __future__ import annotations

import contextvars


class _AttemptTally:
    """Usage reported during one provider request."""

    __slots__ = ("usage",)

    def __init__(self) -> None:
        self.usage: dict | None = None


_current_attempt: contextvars.ContextVar[_AttemptTally | None] = contextvars.ContextVar(
    "token_usage_attempt", default=None
)


def begin_attempt() -> contextvars.Token:
    """Start a fresh usage tally for the current request."""
    return _current_attempt.set(_AttemptTally())


def end_attempt(token: contextvars.Token) -> None:
    """Close the tally opened by :func:`begin_attempt`."""
    _current_attempt.reset(token)


def remember(
    input_tokens: int,
    output_tokens: int,
    *,
    cached_tokens: int | None = None,
    thinking_tokens: int | None = None,
    total_tokens: int | None = None,
) -> None:
    """Save provider-reported counts for this request, when one is active."""
    tally = _current_attempt.get()
    if tally is None:
        return
    usage = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or (input_tokens + output_tokens),
    }
    if cached_tokens is not None:
        usage["cached_tokens"] = cached_tokens
    if thinking_tokens is not None:
        usage["thinking_tokens"] = thinking_tokens
    tally.usage = usage


def reported() -> dict | None:
    """Return a copy of this request's reported usage, if available."""
    tally = _current_attempt.get()
    if tally is None or tally.usage is None:
        return None
    return dict(tally.usage)


def publish_token_usage(usage: dict, poster=None) -> None:
    """Publish through the same callback used by the API handler."""
    if callable(poster):
        poster("token_usage_updated", usage)

"""Token usage of embedding requests, published the way chat calls publish theirs.

Embedding providers do not go through BaseApiHandler, so they publish on their
own, through ``token_usage.publish_token_usage`` looked up at call time: the
nolib bridge replaces that attribute to write every call to its usage log.
"""

from __future__ import annotations

from collections.abc import Iterable

from ...api import token_usage
from ...utils.helpers import estimate_gemini_tokens


def publish_embedding_usage(
    *,
    provider: str,
    model: str,
    texts: Iterable[str],
    reported_input_tokens: object,
) -> None:
    """Publish one embedding request: the provider's count, or an estimate from its texts.

    Embeddings have no output, so ``output_tokens`` is always 0.
    """
    try:
        if (
            isinstance(reported_input_tokens, int)
            and not isinstance(reported_input_tokens, bool)
            and reported_input_tokens >= 0
        ):
            input_tokens, estimated = reported_input_tokens, False
        else:
            input_tokens = sum(estimate_gemini_tokens(text) for text in texts)
            estimated = True
        token_usage.publish_token_usage(
            {
                "input_tokens": input_tokens,
                "output_tokens": 0,
                "total_tokens": input_tokens,
                "estimated": estimated,
                "model_id": model,
                "provider": provider,
                # Embeddings serve quality checks only, also inside translation sessions.
                "operation": "quality_check",
            }
        )
    except Exception:  # noqa: BLE001 - accounting never fails a check
        return

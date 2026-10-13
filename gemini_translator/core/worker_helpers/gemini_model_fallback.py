"""Small, sequential model fallback for failed Gemini translation requests."""

from __future__ import annotations

from contextlib import nullcontext

from gemini_translator.api.errors import ApiAccessError, ModelNotFoundError, NetworkError
from .provider_orchestrator import ProviderAttempt, _resolve_model, _run_attempt, _post_log


FALLBACK_MODEL_IDS = ("gemini-3.5-flash-lite", "gemini-2.5-flash-lite")


def should_try_gemini_model_fallback(worker, error: Exception) -> bool:
    """Authentication, quota, and content errors must not trigger model fallback."""
    if str(getattr(worker, "api_provider_name", "") or "").strip().lower() != "gemini":
        return False
    if not bool(getattr(worker, "gemini_model_fallback_enabled", True)):
        return False
    return isinstance(error, (ModelNotFoundError, NetworkError))


async def retry_with_older_gemini_model(
    worker, prompt, log_prefix, call_kwargs, original_error, *, operation_context=None
):
    """Retry the same prompt sequentially on configured older Flash-Lite models."""
    primary_id = str(
        getattr(worker, "model_id", "")
        or (getattr(worker, "model_config", {}) or {}).get("id", "")
    ).strip()
    api_key = str(getattr(worker, "api_key", "") or "").strip()
    if not primary_id or not api_key:
        raise original_error

    selected_model_id = str(
        getattr(worker, "gemini_fallback_model_id", FALLBACK_MODEL_IDS[0])
        or FALLBACK_MODEL_IDS[0]
    ).strip()
    if selected_model_id not in FALLBACK_MODEL_IDS or selected_model_id == primary_id:
        raise original_error

    last_error = original_error
    for model_id in (selected_model_id,):
        model_name, model_config = _resolve_model("gemini", model_id)
        if str(model_config.get("id") or "") != model_id:
            continue

        level_model = isinstance(model_config.get("min_thinking_budget"), str)
        attempt = ProviderAttempt(
            provider_id="gemini",
            model_name=model_name,
            model_config=model_config,
            api_key=api_key,
            label=f"automatic-model-fallback:{model_id}",
            # Gemini 3.x has rejected MINIMAL on real requests. Use its widely
            # supported LOW level for the fallback; budget models keep their
            # normal budget path.
            thinking_enabled=True if level_model else False,
            thinking_level="low" if level_model else None,
            thinking_budget=0 if not level_model else None,
        )
        _post_log(
            worker,
            f"[GEMINI] Основная модель недоступна; повторяю тот же фрагмент на {model_id}.",
        )
        context_factory = getattr(worker, "debug_operation_context", None)
        context = (
            context_factory(operation_context)
            if operation_context is not None and callable(context_factory)
            else nullcontext()
        )
        with context:
            result = await _run_attempt(worker, attempt, prompt, log_prefix, call_kwargs)
        if result.ok:
            _post_log(worker, f"[GEMINI] Резервная модель {model_id} перевела фрагмент.")
            return result.text

        error = result.exception
        if isinstance(error, ApiAccessError):
            # A bad key cannot be repaired by changing models.
            raise error
        if error is not None:
            last_error = error
        _post_log(
            worker,
            f"[GEMINI] Резервная модель {model_id} тоже не ответила ({type(error).__name__ if error else 'ошибка'}).",
        )

    raise last_error

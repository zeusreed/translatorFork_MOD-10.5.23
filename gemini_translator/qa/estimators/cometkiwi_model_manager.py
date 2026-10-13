"""Install COMETKiwi weights only after the user has read what they agree to.

The weights are large, licensed, and useless to anyone who did not ask for
them, so nothing here happens on its own: enabling the capability installs
nothing, and ``install`` refuses until the caller states that the licence was
accepted.  Verification, atomic replacement, and uninstall come from the shared
bundle manager, which never touches anything above its own model directory.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..model_bundle import (
    MANIFEST_NAME,
    ModelBundle,
    ModelBundleFile,
    ModelBundleManager,
)
from .cometkiwi_client import usable_endpoint


CometKiwiModelFile = ModelBundleFile

__all__ = (
    "MANIFEST_NAME",
    "describe_cometkiwi_setup",
    "describe_quality_score_status",
    "CometKiwiLicenseNotAccepted",
    "CometKiwiModelFile",
    "CometKiwiModelManager",
    "CometKiwiModelManifest",
    "CometKiwiModelStatus",
    "OptionalEstimatorDependencyMissing",
)


class OptionalEstimatorDependencyMissing(RuntimeError):
    """Raised when the estimator is asked for but cannot run on this machine."""


class CometKiwiLicenseNotAccepted(OptionalEstimatorDependencyMissing):
    """Raised when an install is attempted before the licence was accepted."""


@dataclass(frozen=True, slots=True)
class CometKiwiModelStatus:
    """What is installed, how big it is, and why it cannot be used if it cannot."""

    state: Literal["missing", "installing", "ready", "invalid"]
    model: str
    installed_size_bytes: int | None = None
    license_name: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"missing", "installing", "ready", "invalid"}:
            raise ValueError("unsupported model state")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be a nonempty string")
        if self.installed_size_bytes is not None and self.installed_size_bytes < 0:
            raise ValueError("installed_size_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class CometKiwiModelManifest:
    """One model version, named by everything a user must know before installing."""

    model: str
    source_url: str
    size_bytes: int
    sha256_by_file: Mapping[str, str]
    license_name: str
    license_url: str
    minimum_ram_bytes: int

    def __post_init__(self) -> None:
        for field_name in (
            "model",
            "source_url",
            "license_name",
            "license_url",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a nonempty string")
        for field_name in ("size_bytes", "minimum_ram_bytes"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")
        if not isinstance(self.sha256_by_file, Mapping) or not self.sha256_by_file:
            raise ValueError("sha256_by_file must be a nonempty mapping")
        for name, digest in self.sha256_by_file.items():
            if not isinstance(name, str) or "/" in name or name in {".", ".."}:
                raise ValueError("model file name must be a plain file name")
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError("sha256_by_file values must be sha256 digests")

    def bundle(self) -> ModelBundle:
        """The shared installer's view: one file list, each named by its digest."""
        files = tuple(
            ModelBundleFile(
                name=name,
                url=f"{self.source_url.rstrip('/')}/{name}",
                size_bytes=max(1, self.size_bytes // len(self.sha256_by_file)),
                sha256=digest,
            )
            for name, digest in sorted(self.sha256_by_file.items())
        )
        return ModelBundle(version=self.model, files=files)


class CometKiwiModelManager:
    """Own the COMETKiwi model directory, and refuse to fill it uninvited."""

    def __init__(
        self,
        root: Path | str,
        manifest: CometKiwiModelManifest,
        downloader=None,
    ) -> None:
        if not isinstance(manifest, CometKiwiModelManifest):
            raise TypeError("manifest must be a CometKiwiModelManifest")
        self.manifest = manifest
        self._bundle = ModelBundleManager(
            root,
            manifest.bundle(),
            downloader,
            error_type=OptionalEstimatorDependencyMissing,
            staging_prefix=".cometkiwi-",
        )

    @property
    def root(self) -> Path:
        return self._bundle.root

    @property
    def model_dir(self) -> Path:
        return self._bundle.model_dir

    def status(self) -> CometKiwiModelStatus:
        """Report what is on disk without reading a single weight byte."""
        return self._as_status(self._bundle.status())

    async def install(
        self,
        manifest: CometKiwiModelManifest | None = None,
        *,
        license_accepted: bool = False,
        progress=None,
        cancellation=None,
    ) -> CometKiwiModelStatus:
        """Download and verify the weights, once the licence has been accepted."""
        if manifest is not None and manifest != self.manifest:
            raise OptionalEstimatorDependencyMissing("manifest_mismatch")
        if not license_accepted:
            raise CometKiwiLicenseNotAccepted("license_not_accepted")
        return self._as_status(
            await self._bundle.install(progress=progress, cancellation=cancellation)
        )

    def uninstall(self) -> CometKiwiModelStatus:
        """Delete exactly this model directory and nothing around it."""
        return self._as_status(self._bundle.uninstall())

    def _as_status(self, status) -> CometKiwiModelStatus:
        return CometKiwiModelStatus(
            state=status.state,
            model=self.manifest.model,
            installed_size_bytes=status.installed_size_bytes,
            license_name=self.manifest.license_name,
            reason=status.reason,
        )


def describe_cometkiwi_setup(
    settings,
    status: CometKiwiModelStatus | None = None,
    last_duration_seconds: float | None = None,
) -> str:
    """One line saying what is set up, what is missing, and what it last cost.

    Ticking the checkbox is not the same as having the estimator: this text is
    what tells the user which of the parts — a runner or a usable address,
    weights, licence — they still owe, before anything is downloaded or started.
    """
    if not getattr(settings.capabilities, "cometkiwi_enabled", False):
        return ""
    missing: list[str] = []
    endpoint = str(getattr(settings, "cometkiwi_endpoint", "") or "").strip()
    remote = bool(endpoint)
    if remote and not usable_endpoint(endpoint.rstrip("/")):
        # The address stands in for the runner, and scoring refuses one it
        # cannot dial - as endpoint_invalid, for every chapter.
        missing.append("адрес вида http://host:port")
    if not remote and not str(getattr(settings, "cometkiwi_runner_path", "") or "").strip():
        missing.append("путь к runner")
    if not str(getattr(settings, "cometkiwi_model", "") or "").strip():
        missing.append("модель")
    if not getattr(settings, "cometkiwi_license_accepted", False):
        missing.append("принятая лицензия")
    if not remote and (status is None or status.state != "ready"):
        missing.append("установленные веса")
    if missing:
        return "COMETKiwi: требует настройки — не хватает: " + ", ".join(missing) + "."
    if status is None or status.state != "ready":
        # Remote scoring has no local install to describe: the weights live on
        # the other machine, so there is nothing further to add here.
        return ""

    parts = [
        f"COMETKiwi: {status.model}",
        f"устройство {getattr(settings, 'cometkiwi_device', 'cpu')}",
        f"лицензия {status.license_name}" if status.license_name else "",
        _size_text(status.installed_size_bytes),
        "нагрузка: high (cpu, memory)",
    ]
    if last_duration_seconds is not None:
        parts.append(f"последний запуск {last_duration_seconds:.1f} c")
    return " · ".join(part for part in parts if part) + "."


# What a stored reason means to the person reading the chapter card. The
# codes come from the client, the PC server and the runner (spec 2026-09-12,
# «Отказы и деградация»); a code missing here is still shown, by its name.
QUALITY_SCORE_REASONS = {
    "model_missing": "не указана модель",
    "endpoint_invalid": "адрес ПК записан неверно",
    "runner_missing": "не указан путь к программе оценки",
    "runner_not_found": "не найдена программа оценки",
    "weights_missing": "не найдены веса модели",
    "endpoint_unreachable": "ПК не отвечает",
    "timeout": "оценка не уложилась в отведённое время",
    "runner_not_started": "программа оценки не запустилась",
    "out_of_memory": "не хватило памяти",
    "runner_crashed": "программа оценки аварийно завершилась",
    "response_too_large": "ответ оценки слишком большой",
    "runner_failed": "запрос на оценку не удалось отправить",
    "invalid_response": "ответ оценки не разобран",
    "unsupported_schema_version": "версии приложения и программы оценки не совпадают",
    "request_id_mismatch": "ответ пришёл на другой запрос",
    "runner_error": "программа оценки сообщила об ошибке",
    "score_count_mismatch": "оценок пришло не столько, сколько фрагментов",
    "invalid_scores": "пришли негодные оценки",
    "invalid_reason": "причина сбоя не распознана",
    "capability_disabled": "оценка выключена",
    "license_not_accepted": "не принята лицензия модели",
}
# What the server or the runner itself names after "runner_error:".
RUNNER_ERROR_DETAILS = {
    "invalid_request": "программа оценки не приняла запрос",
    "unexpected_segment_field": "программа оценки не приняла запрос",
    "unsupported_schema_version": "версии приложения и программы оценки не совпадают",
    "too_many_segments": "в запросе слишком много фрагментов",
    "request_too_large": "запрос слишком большой",
    "score_count_mismatch": "оценок пришло не столько, сколько фрагментов",
    "checkpoint_missing": "не найдены веса модели",
    "weights_missing": "не найдены веса модели",
    "invalid_model_output": "модель вернула негодный ответ",
    "runner_environment_incomplete": "не установлены пакеты для оценки",
    "out_of_memory": "не хватило памяти",
    # The local runner's name for running out of video memory.
    "outofmemoryerror": "не хватило видеопамяти",
}
_NUMBERED_REASONS = (
    ("endpoint_status_", "ПК ответил ошибкой {}"),
    ("runner_exit_", "программа оценки завершилась с кодом {}"),
    ("runner_signal_", "программа оценки прервана сигналом {}"),
)
_STATUS_WITHOUT_REASON = {
    "unavailable": "оценка недоступна",
    "disabled": "оценка выключена",
}


def describe_quality_score_status(status: str) -> str:
    """Say why a chapter has no CometKiwi score, or nothing when there is no failure.

    The stored value is ``<status>:<reason>``, split on the first colon only:
    a failure the runner names itself keeps a second one,
    ``runner_error:<detail>``.
    """
    state, _, reason = str(status or "").partition(":")
    if state not in _STATUS_WITHOUT_REASON:
        return ""
    if not reason:
        return _STATUS_WITHOUT_REASON[state]
    if reason.startswith("runner_error:"):
        detail = reason.removeprefix("runner_error:")
        return RUNNER_ERROR_DETAILS.get(detail) or (
            f"{QUALITY_SCORE_REASONS['runner_error']} ({detail})"
        )
    if reason in QUALITY_SCORE_REASONS:
        return QUALITY_SCORE_REASONS[reason]
    for prefix, text in _NUMBERED_REASONS:
        number = reason.removeprefix(prefix)
        if number != reason and number.isdigit():
            return text.format(number)
    return f"{_STATUS_WITHOUT_REASON[state]} ({reason})"


def _size_text(size_bytes: int | None) -> str:
    if not size_bytes:
        return ""
    return f"{size_bytes / 1024 ** 3:.1f} ГБ на диске"

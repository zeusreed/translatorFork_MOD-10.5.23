"""Talk to a COMETKiwi runner that lives in its own process and environment.

Nothing here imports ``torch`` or ``comet``: the weights and the deep-learning
stack stay behind a separate interpreter the user configures explicitly, and
this module only writes one JSON request to it and reads one JSON answer back.
Every failure of that process — a timeout, a crash, an out-of-memory marker,
noise on stderr — becomes ``status="unavailable"`` and the session continues.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from urllib.parse import urlsplit

from .base import (
    QualityEstimate,
    QualityEstimateError,
    QualityEstimateRequest,
    aggregate,
    unavailable,
)


ESTIMATOR_NAME = "cometkiwi"
SCHEMA_VERSION = 1
# One answer for at most 512 windows of one chapter: a megabyte is already far
# more than a list of floats needs, and an unbounded read is a way to be hanged.
MAX_RESPONSE_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 900.0
# The transport's own bound on the TCP handshake alone, separate from the
# total budget above.  A live PC on a home LAN accepts in milliseconds; a
# sleeping one whose address the router still remembers must not make a
# chapter wait anywhere near the full timeout just to learn that.
REMOTE_CONNECT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class CometKiwiRunnerConfig:
    """Exactly what the user configured, and nothing derived from a request."""

    runner_path: str
    model_dir: str
    model: str
    device: str = "cpu"
    python_executable: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    # Where the runner lives.  Empty means this machine, in a subprocess, which
    # is what every existing installation does.  Set means another machine on
    # the local network answers the same protocol over HTTP: the runner script
    # and the weights are on that machine, and asking for them here would
    # switch the whole capability off for a setup that is perfectly valid.
    endpoint: str = ""

    def __post_init__(self) -> None:
        for field_name in ("runner_path", "model_dir", "model", "device", "endpoint"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise QualityEstimateError(f"{field_name} must be a string")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise QualityEstimateError("timeout_seconds must be a number")
        if self.timeout_seconds <= 0:
            raise QualityEstimateError("timeout_seconds must be positive")

    @property
    def is_remote(self) -> bool:
        """Report whether scoring happens on another machine."""
        return bool(self.endpoint.strip())

    def _base_url(self) -> str:
        return self.endpoint.strip().rstrip("/")

    def score_url(self) -> str:
        """The address one scoring request is sent to."""
        return f"{self._base_url()}/score"

    def setup_problem(self) -> str:
        """Name the one thing that is missing, or an empty string when ready."""
        if not self.model.strip():
            return "model_missing"
        if self.is_remote:
            return "" if usable_endpoint(self._base_url()) else "endpoint_invalid"
        if not self.runner_path.strip():
            return "runner_missing"
        if not Path(self.runner_path).is_file():
            return "runner_not_found"
        if not self.model_dir.strip() or not Path(self.model_dir).is_dir():
            return "weights_missing"
        return ""

    def command(self) -> tuple[str, ...]:
        """The exact argv to run: no shell, no arguments from the chapter."""
        interpreter = self.python_executable.strip()
        if interpreter:
            return (interpreter, self.runner_path)
        return (self.runner_path,)


def usable_endpoint(url: str) -> bool:
    """Report whether the address can be dialled at all, without dialling it.

    Public because the settings dialog's connection check and its setup line
    apply the same rule: an address scoring refuses as ``endpoint_invalid``
    must not look like a server that is merely switched off.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "http" or not parts.hostname:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    return port is None or 1 <= port <= 65535


class CometKiwiEstimator:
    """Score a chapter's windows with a reference-free model, or say why it could not."""

    def __init__(
        self,
        config: CometKiwiRunnerConfig,
        *,
        enabled: bool = True,
        license_accepted: bool = False,
        run_process=None,
        request_id_factory=None,
    ) -> None:
        if not isinstance(config, CometKiwiRunnerConfig):
            raise QualityEstimateError("config must be a CometKiwiRunnerConfig")
        self._config = config
        self._enabled = bool(enabled)
        self._license_accepted = bool(license_accepted)
        self._run_process = run_process or (
            remote_transport(config) if config.is_remote else run_runner_process
        )
        self._request_id_factory = request_id_factory or _default_request_id

    async def estimate(
        self, request: QualityEstimateRequest, cancellation=None
    ) -> QualityEstimate:
        if not isinstance(request, QualityEstimateRequest):
            raise QualityEstimateError("request must be a QualityEstimateRequest")
        config = self._config
        if not self._enabled:
            from .base import disabled

            return disabled(ESTIMATOR_NAME, config.model, "capability_disabled")
        if not self._license_accepted:
            from .base import disabled

            return disabled(ESTIMATOR_NAME, config.model, "license_not_accepted")
        problem = config.setup_problem()
        if problem:
            return unavailable(ESTIMATOR_NAME, config.model, problem)

        request_id = self._request_id_factory(request)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "model": config.model,
            "model_dir": config.model_dir,
            "device": config.device,
            "source_language": request.source_language,
            "target_language": request.target_language,
            "segments": [
                {"source": window.source, "translation": window.translation}
                for window in request.windows
            ],
        }
        try:
            answer = await self._run_process(
                config.command(),
                json.dumps(payload, ensure_ascii=False),
                timeout=config.timeout_seconds,
                cancellation=cancellation,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return unavailable(ESTIMATOR_NAME, config.model, "timeout")
        except RunnerProcessError as error:
            return unavailable(ESTIMATOR_NAME, config.model, error.reason)
        except Exception:  # noqa: BLE001 - an estimate never breaks the session
            return unavailable(ESTIMATOR_NAME, config.model, "runner_failed")

        try:
            scores, metadata = _read_answer(answer, request_id, len(request.windows))
        except QualityEstimateError as error:
            return unavailable(ESTIMATOR_NAME, config.model, str(error))
        try:
            return aggregate(
                ESTIMATOR_NAME,
                config.model,
                request,
                scores,
                {"device": config.device, **metadata},
            )
        except QualityEstimateError:
            # aggregate() explains a bad score in free text, which the bound on
            # a persisted reason could only replace with invalid_reason.
            return unavailable(ESTIMATOR_NAME, config.model, "invalid_scores")


class RunnerProcessError(RuntimeError):
    """A runner process that failed in a way worth naming in the journal."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def run_runner_process(
    command: tuple[str, ...],
    payload: str,
    *,
    timeout: float,
    cancellation=None,
) -> str:
    """Run the configured runner once, with no shell and a bounded answer."""
    if not command:
        raise RunnerProcessError("runner_missing")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        raise RunnerProcessError("runner_not_started") from None
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload.encode("utf-8")), timeout=timeout
        )
    except (TimeoutError, asyncio.TimeoutError):
        _terminate(process)
        raise TimeoutError("cometkiwi runner timed out") from None
    except asyncio.CancelledError:
        _terminate(process)
        raise
    if process.returncode != 0:
        raise RunnerProcessError(_exit_reason(process.returncode, stderr))
    if len(stdout) > MAX_RESPONSE_BYTES:
        raise RunnerProcessError("response_too_large")
    return stdout.decode("utf-8", errors="replace")


def remote_transport(config: CometKiwiRunnerConfig):
    """Send one request to a runner on another machine, over the local network.

    The signature matches ``run_runner_process`` on purpose: the estimator does
    not know, and must not know, which side of the network answered it.  The
    ``command`` argument is unused here and accepted only to keep that shape.
    """

    url = config.score_url()

    async def send(command, payload, *, timeout, cancellation=None) -> str:
        import aiohttp  # noqa: PLC0415 - only a remote estimate pays for it

        # Resolved after the deferred import above: aiohttp gained this
        # class in 3.10, and the project does not pin aiohttp's version.
        connection_timeout_error = getattr(
            aiohttp, "ConnectionTimeoutError", aiohttp.ServerTimeoutError
        )
        session_timeout = aiohttp.ClientTimeout(
            total=timeout, sock_connect=REMOTE_CONNECT_TIMEOUT_SECONDS
        )
        try:
            async with aiohttp.ClientSession(timeout=session_timeout) as session:
                async with session.post(
                    url,
                    data=payload.encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status != 200:
                        raise RunnerProcessError(
                            f"endpoint_status_{response.status}"
                        )
                    # Read in bounded chunks rather than one read(N+1) call: a
                    # single read only returns whatever is already buffered
                    # and can come back well under N+1 even when the sender
                    # has far more queued, so it cannot be trusted alone to
                    # catch an oversized answer.
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise RunnerProcessError("response_too_large")
        except asyncio.CancelledError:
            raise
        except RunnerProcessError:
            raise
        except connection_timeout_error:
            # The connection itself never came up - a sleeping or absent
            # machine, not a slow one, so this must not become "timeout".
            raise RunnerProcessError("endpoint_unreachable") from None
        except (TimeoutError, asyncio.TimeoutError):
            raise TimeoutError("cometkiwi endpoint timed out") from None
        except Exception:  # noqa: BLE001 - every network fault is one reason
            raise RunnerProcessError("endpoint_unreachable") from None
        return body.decode("utf-8", errors="replace")

    return send


def _terminate(process) -> None:
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        return


def _exit_reason(returncode: int | None, stderr: bytes) -> str:
    """Name the failure without ever copying the chapter text into a log."""
    text = stderr.decode("utf-8", errors="replace").lower()
    if "out of memory" in text or "oom" in text:
        return "out_of_memory"
    if returncode is None:
        return "runner_crashed"
    if returncode < 0:
        # asyncio reports a process killed by a POSIX signal as the negated
        # signal number, and "runner_exit_-9" would fail the persisted bound.
        return f"runner_signal_{-returncode}"
    return f"runner_exit_{returncode}"


def _read_answer(
    answer: str, request_id: str, expected: int
) -> tuple[tuple[float, ...], Mapping[str, str]]:
    """Read exactly one JSON object, from a stream that may also carry noise."""
    line = _last_json_line(answer)
    if line is None:
        raise QualityEstimateError("invalid_response")
    try:
        payload = json.loads(line)
    except ValueError:
        raise QualityEstimateError("invalid_response") from None
    if not isinstance(payload, dict):
        raise QualityEstimateError("invalid_response")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise QualityEstimateError("unsupported_schema_version")
    if payload.get("request_id") != request_id:
        raise QualityEstimateError("request_id_mismatch")
    error = payload.get("error")
    if error:
        raise QualityEstimateError(_safe_reason(error))
    scores = payload.get("scores")
    if not isinstance(scores, list) or len(scores) != expected:
        raise QualityEstimateError("score_count_mismatch")
    metadata = {
        key: str(payload[key])
        for key in ("model", "device", "runtime_seconds")
        if isinstance(payload.get(key), (str, int, float))
    }
    return tuple(scores), metadata


def _last_json_line(answer: str) -> str | None:
    """The runner may print warnings; only the last JSON object is the answer."""
    for candidate in reversed(answer.splitlines()):
        stripped = candidate.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return stripped
    return None


def _safe_reason(value: object) -> str:
    """Keep a runner-supplied reason short and free of chapter text.

    ASCII only on purpose: ``str.isalnum`` is true for the very characters a
    chapter is written in, so a runner that quotes the text it choked on would
    otherwise copy it straight into the journal.
    """
    text = str(value).strip().lower().replace(" ", "_")
    allowed = "".join(
        character
        for character in text
        if character == "_" or ("a" <= character <= "z") or character.isdigit()
    )
    return f"runner_error:{allowed[:40]}" if allowed else "runner_error"


def _default_request_id(request: QualityEstimateRequest) -> str:
    from hashlib import sha256

    identity = "\x1f".join(
        (request.chapter_id, *(window.window_id for window in request.windows))
    )
    return sha256(identity.encode("utf-8")).hexdigest()[:20]

import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GT_DISABLE_LOCAL_MODEL_DISCOVERY": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"),
    }
    return subprocess.run(
        [sys.executable, "-m", "gemini_translator.cli", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def _stdout_json(completed: subprocess.CompletedProcess[str]) -> dict:
    stdout = completed.stdout.strip()
    assert stdout
    assert stdout.startswith("{")
    assert stdout.endswith("}")
    assert "\n" not in stdout
    return json.loads(stdout)


def test_cli_module_providers_writes_compact_json_to_stdout():
    completed = _run_cli("--compact", "providers", "--no-discovery")

    payload = _stdout_json(completed)
    assert completed.returncode == 0
    assert payload["ok"] is True
    assert payload["diagnose"] is False
    assert isinstance(payload["providers"], list)
    assert "CONFIG INFO" not in completed.stdout


def test_cli_module_error_writes_json_payload_to_stdout():
    completed = _run_cli("--compact", "models", "--provider", "__missing__")

    payload = _stdout_json(completed)
    assert completed.returncode == 2
    assert payload["ok"] is False
    assert "Unknown provider: __missing__" in payload["error"]
    assert "available_providers" in payload
    assert not completed.stderr.lstrip().startswith("{")

from __future__ import annotations

import pytest

from gemini_translator.mcp.ai_bridge import (
    GuiAiTaskTimeout,
    claim_gui_ai_task,
    complete_gui_ai_task,
    create_gui_ai_task,
    fail_gui_ai_task,
    list_gui_ai_tasks,
    load_gui_ai_task,
    wait_for_gui_ai_task,
)


def test_gui_ai_task_lifecycle_complete(tmp_path):
    task = create_gui_ai_task(
        tmp_path,
        {
            "prompt": "Translate this",
            "system_instruction": "Be concise",
            "metadata": {"operation": "translation"},
        },
    )

    assert task.status == "pending"
    assert task.prompt == "Translate this"
    assert task.system_instruction == "Be concise"
    assert task.metadata["operation"] == "translation"
    assert list_gui_ai_tasks(tmp_path)[0].id == task.id

    claimed = claim_gui_ai_task(tmp_path, task.id, "Gemini")
    assert claimed.status == "claimed"
    assert claimed.claimed_by == "Gemini"

    completed = complete_gui_ai_task(tmp_path, task.id, "Готовый ответ")
    assert completed.status == "completed"
    assert completed.result_text == "Готовый ответ"

    result = wait_for_gui_ai_task(tmp_path, task.id, timeout_sec=0.1)
    assert result["ok"] is True
    assert result["text"] == "Готовый ответ"


def test_gui_ai_task_fail_returns_error(tmp_path):
    task = create_gui_ai_task(tmp_path, {"prompt": "Fix me"})

    fail_gui_ai_task(tmp_path, task.id, "model refused")

    result = wait_for_gui_ai_task(tmp_path, task.id, timeout_sec=0.1)
    assert result["ok"] is False
    assert result["error"] == "model refused"


def test_gui_ai_task_claim_rejects_completed_task(tmp_path):
    task = create_gui_ai_task(tmp_path, {"prompt": "Done soon"})
    complete_gui_ai_task(tmp_path, task.id, "done")

    with pytest.raises(ValueError, match="cannot claim"):
        claim_gui_ai_task(tmp_path, task.id, "Gemini")


def test_gui_ai_task_wait_times_out(tmp_path):
    task = create_gui_ai_task(tmp_path, {"prompt": "Wait"})

    with pytest.raises(GuiAiTaskTimeout):
        wait_for_gui_ai_task(tmp_path, task.id, timeout_sec=0.01, poll_interval=0.001)

    assert load_gui_ai_task(tmp_path, task.id).status == "pending"

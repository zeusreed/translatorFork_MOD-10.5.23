import sys
import types

from gemini_translator.mcp.server import McpStdioServer, TOOL_NAMES


class FakeClient:
    def __init__(self):
        self.enqueued = []
        self.failed_gui_ai_tasks = []

    def status(self):
        return {"ok": True, "daemon": {"pid": 1}, "queue": {}}

    def enqueue(self, payload):
        self.enqueued.append(payload)
        return {"ok": True, "job": {"id": "job_1", "status": "queued", "type": payload["job_type"]}}

    def get_job(self, job_id):
        return {"ok": True, "job": {"id": job_id, "status": "succeeded"}}

    def list_jobs(self):
        return {"ok": True, "jobs": []}

    def cancel_job(self, job_id):
        return {"ok": True, "job": {"id": job_id, "status": "cancelled"}}

    def list_gui_ai_tasks(self):
        return {"ok": True, "tasks": [{"id": "gui_ai_1", "status": "pending"}]}

    def claim_gui_ai_task(self, task_id, client_name=""):
        return {
            "ok": True,
            "task": {
                "id": task_id,
                "status": "claimed",
                "claimed_by": client_name,
                "prompt": "PROMPT FOR AI",
            },
        }

    def submit_gui_ai_task_result(self, task_id, text):
        return {"ok": True, "task": {"id": task_id, "status": "completed", "result_text": text}}

    def fail_gui_ai_task(self, task_id, error, **details):
        self.failed_gui_ai_tasks.append((task_id, error, details))
        return {"ok": True, "task": {"id": task_id, "status": "failed", "error": error}}


class FailingStatusClient(FakeClient):
    def status(self):
        raise RuntimeError("boom")


def test_tools_list_contains_translation_tools():
    server = McpStdioServer(client_factory=lambda: FakeClient())
    result = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tool_names = [tool["name"] for tool in result["result"]["tools"]]

    assert "start_translation" in tool_names
    assert "get_job_status" in tool_names
    assert tool_names == TOOL_NAMES


def test_initialize_response_uses_mcp_protocol_version():
    server = McpStdioServer(client_factory=lambda: FakeClient())
    result = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})

    assert result["result"]["protocolVersion"] == "2025-06-18"
    assert result["result"]["serverInfo"]["name"] == "translatorFork"


def test_start_translation_enqueues_daemon_job():
    fake = FakeClient()
    server = McpStdioServer(client_factory=lambda: fake)
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "start_translation",
                "arguments": {"epub": "/book.epub", "project": "/project"},
            },
        }
    )

    assert response["result"]["isError"] is False
    assert fake.enqueued[0]["job_type"] == "translation"
    assert fake.enqueued[0]["project"] == "/project"


def test_glossary_correction_returns_structured_unsupported_response():
    server = McpStdioServer(client_factory=lambda: FakeClient())
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "start_glossary_review_or_correction",
                "arguments": {"epub": "/book.epub", "project": "/project"},
            },
        }
    )

    assert response["result"]["isError"] is True
    assert "unsupported_in_this_build" in response["result"]["content"][0]["text"]


def test_get_job_status_calls_client():
    server = McpStdioServer(client_factory=lambda: FakeClient())
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_job_status", "arguments": {"job_id": "job_1"}},
        }
    )

    assert response["result"]["isError"] is False
    assert "job_1" in response["result"]["content"][0]["text"]


def test_translator_status_client_failure_returns_tool_error():
    server = McpStdioServer(client_factory=lambda: FailingStatusClient())
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "translator_status", "arguments": {}},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is True
    assert "boom" in response["result"]["content"][0]["text"]


def test_print_mcp_config_returns_config_snippet():
    server = McpStdioServer(client_factory=lambda: FakeClient())
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "print_mcp_config", "arguments": {"client": "codex"}},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is False
    assert "content" in response["result"]
    assert "[mcp_servers.translatorFork]" in response["result"]["content"][0]["text"]
    assert "gemini_translator.mcp" in response["result"]["content"][0]["text"]


def test_print_mcp_config_wraps_available_client_install_payload(monkeypatch):
    module = types.SimpleNamespace(
        handle_install_tool=lambda name, arguments: {
            "ok": True,
            "tool": name,
            "arguments": arguments,
        }
    )
    monkeypatch.setitem(sys.modules, "gemini_translator.mcp.client_install", module)
    server = McpStdioServer(client_factory=lambda: FakeClient())
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "print_mcp_config", "arguments": {"client": "codex"}},
        }
    )

    assert "error" not in response
    assert response["result"]["isError"] is False
    assert "content" in response["result"]
    assert "print_mcp_config" in response["result"]["content"][0]["text"]
    assert "codex" in response["result"]["content"][0]["text"]


def test_installer_tool_schemas_expose_supported_arguments():
    server = McpStdioServer(client_factory=lambda: FakeClient())
    response = server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}

    install_props = tools["install_mcp_client"]["inputSchema"]["properties"]
    assert {"client", "mode", "config_path", "server_name", "state_dir"} <= set(install_props)

    config_props = tools["print_mcp_config"]["inputSchema"]["properties"]
    assert {"client", "server_name", "state_dir"} <= set(config_props)


def test_gui_ai_inbox_tools_are_exposed_and_call_client():
    fake = FakeClient()
    server = McpStdioServer(client_factory=lambda: fake)
    tools_response = server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}

    assert "list_gui_ai_tasks" in tools
    assert "claim_gui_ai_task" in tools
    assert "submit_gui_ai_task_result" in tools
    assert "fail_gui_ai_task" in tools

    claim_response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "claim_gui_ai_task",
                "arguments": {"task_id": "gui_ai_1", "client_name": "Gemini"},
            },
        }
    )
    submit_response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "submit_gui_ai_task_result",
                "arguments": {"task_id": "gui_ai_1", "text": "готово"},
            },
        }
    )

    assert claim_response["result"]["isError"] is False
    assert '"claimed_by": "Gemini"' in claim_response["result"]["content"][0]["text"]
    assert "PROMPT FOR AI" in claim_response["result"]["content"][0]["text"]
    assert submit_response["result"]["isError"] is False
    assert '"status": "completed"' in submit_response["result"]["content"][0]["text"]


def test_fail_gui_ai_task_forwards_limit_details_to_client():
    fake = FakeClient()
    server = McpStdioServer(client_factory=lambda: fake)
    tools_response = server.handle_request({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in tools_response["result"]["tools"]}

    fail_props = tools["fail_gui_ai_task"]["inputSchema"]["properties"]
    assert {"reset_after_seconds", "limit_window_seconds"} <= set(fail_props)

    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "fail_gui_ai_task",
                "arguments": {
                    "task_id": "gui_ai_1",
                    "error": "usage limit reached",
                    "reset_after_seconds": 120,
                    "limit_window_seconds": 5 * 60 * 60,
                },
            },
        }
    )

    assert response["result"]["isError"] is False
    assert fake.failed_gui_ai_tasks == [
        (
            "gui_ai_1",
            "usage limit reached",
            {"reset_after_seconds": 120, "limit_window_seconds": 5 * 60 * 60},
        )
    ]


def test_notification_without_id_returns_none():
    server = McpStdioServer(client_factory=lambda: FakeClient())

    assert server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_stdio_server_marks_client_session_on_requests_and_notifications():
    class RecordingSession:
        def __init__(self):
            self.methods = []

        def touch(self, method=None):
            self.methods.append(method)

    session = RecordingSession()
    server = McpStdioServer(client_factory=lambda: FakeClient(), client_session=session)

    server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert session.methods == ["initialize", "notifications/initialized"]


def test_start_full_pipeline_enqueues_pipeline_job():
    fake = FakeClient()
    server = McpStdioServer(client_factory=lambda: fake)
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "start_full_pipeline",
                "arguments": {
                    "epub": "/book.epub",
                    "project": "/project",
                    "steps": ["glossary", "translation", "untranslated_fix", "consistency", "epub_build"],
                },
            },
        }
    )

    assert response["result"]["isError"] is False
    assert fake.enqueued[0]["job_type"] == "pipeline"
    assert fake.enqueued[0]["metadata"]["steps"][0]["tool"] == "start_glossary_generation"

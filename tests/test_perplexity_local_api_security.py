import importlib
import json
import os
from unittest.mock import Mock

import pytest


@pytest.fixture
def perplexity_server(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    module_name = "gemini_translator.api.servers.perplexity"
    return importlib.import_module(module_name)


def test_local_api_routes_reject_requests_without_the_local_token(perplexity_server):
    client = perplexity_server.app.test_client()

    assert client.get("/status").status_code == 401
    assert client.post("/v1/chat/completions", json={"messages": [{"content": "hi"}]}).status_code == 401
    assert client.post("/validate_batch", json={"tokens": ["credential"]}).status_code == 401
    assert client.post("/auth", json={"token": "credential"}).status_code == 401


def test_authenticated_local_api_accepts_status_and_rejects_oversized_batch(perplexity_server):
    client = perplexity_server.app.test_client()
    headers = {"X-Translator-Local-Token": perplexity_server.LOCAL_API_TOKEN}

    assert client.get("/status", headers=headers).status_code == 200
    response = client.post("/validate_batch", json={"tokens": ["x"] * 101}, headers=headers)
    assert response.status_code == 400


def test_authenticated_validation_passes_tokens_to_backend(perplexity_server, monkeypatch):
    client = perplexity_server.app.test_client()
    headers = {"X-Translator-Local-Token": perplexity_server.LOCAL_API_TOKEN}
    validate_token = Mock(return_value={"valid": True, "email": "test@example.com"})
    monkeypatch.setattr(perplexity_server.backend, "validate_token", validate_token)

    response = client.post("/validate_batch", json={"tokens": ["credential"]}, headers=headers)

    assert response.status_code == 200
    assert validate_token.call_args.args == ("credential",)


def test_endpoint_info_contains_local_token_and_is_written_atomically(perplexity_server, tmp_path, monkeypatch):
    endpoint_path = tmp_path / "endpoint.json"
    monkeypatch.setattr(perplexity_server, "ENDPOINT_FILE", str(endpoint_path))

    perplexity_server.save_endpoint_info(12345)

    payload = json.loads(endpoint_path.read_text(encoding="utf-8"))
    assert payload["port"] == 12345
    assert payload["local_api_token"] == perplexity_server.LOCAL_API_TOKEN
    assert not list(tmp_path.glob("*.tmp"))
    if os.name != "nt":
        assert endpoint_path.stat().st_mode & 0o777 == 0o600

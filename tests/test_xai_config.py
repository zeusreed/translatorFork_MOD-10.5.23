import json
from pathlib import Path


def test_xai_grok_provider_is_configured():
    providers = json.loads(
        (Path(__file__).resolve().parents[1] / "config" / "api_providers.json").read_text(encoding="utf-8")
    )

    provider = providers["xai"]

    assert provider["display_name"] == "xAI Grok API"
    assert provider["handler_class"] == "OpenRouterApiHandler"
    assert provider["base_url"] == "https://api.x.ai/v1/chat/completions"
    assert provider["visible"] is True
    assert provider["file_suffix"] == "_translated_xai.html"

    models = provider["models"]
    assert models["Grok 4.3"]["id"] == "grok-4.3"
    assert models["Grok 4.3"]["context_length"] == 1000000
    assert models["Grok 4.3"]["reasoning_effort"] == "low"
    assert models["Grok Build 0.1"]["id"] == "grok-build-0.1"
    assert models["Grok Build 0.1"]["context_length"] == 256000

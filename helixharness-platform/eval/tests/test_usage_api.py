"""API analysis uses deployment-provided endpoint, model and credential."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from eval.usage_ai import _ai_config, _call_api


def test_direct_api_passes_model_and_returns_answer_without_credentials(monkeypatch):
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_MODEL", "demo-model")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_BASE_URL", "https://model.example.test/v1")
    config = _ai_config({})
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Synthetic insight"))], usage=None
    )
    with patch("openai.OpenAI") as constructor:
        constructor.return_value.__enter__.return_value = client
        result = _call_api(config["endpoint"], "Synthetic facts", config["token"], model=config["model"])
    assert constructor.call_args.kwargs["base_url"] == "https://model.example.test/v1"
    assert constructor.call_args.kwargs["api_key"] == "synthetic-test-key"
    assert client.chat.completions.create.call_args.kwargs == {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "Synthetic facts"}],
    }
    assert result == {"answer": "Synthetic insight", "stats": None, "mode": "direct_api"}
    assert "synthetic-test-key" not in str(result)


@pytest.mark.parametrize("key,model", [("", "demo"), ("fake", "")])
def test_missing_api_configuration_fails_before_network(key, model):
    with patch("openai.OpenAI") as constructor:
        with pytest.raises(ValueError, match="requires"):
            _call_api("https://model.example.test/v1", "facts", key, model=model)
        constructor.assert_not_called()

import sys

import pytest
from pydantic import ValidationError

from app.config import Settings
from app import smoke


@pytest.mark.parametrize("model", ["gemini-test", "gemini-3.8-flash", "gemini-3.5-flash-lite"])
def test_explicit_model_and_key_configuration(monkeypatch, model):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    monkeypatch.setenv("GEMINI_MODEL", model)
    monkeypatch.setenv("GEMINI_FREE_TIER_CONFIRMED", "true")
    monkeypatch.setenv("AI_MAX_TOOL_CALLS", "2")
    result = Settings.from_env()
    assert result.gemini_model == model and result.ai_max_tool_calls == 2
    assert result.gemini_free_tier_confirmed and "dummy-key" not in repr(result)
    assert Settings().gemini_model is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("gemini_model", "https://evil.example"),
        ("gemini_model", "models/gemini-test"),
        ("ai_max_tool_calls", 4),
        ("ai_max_llm_requests", 5),
        ("ai_max_candidates", 11),
        ("ai_max_context_chars", 20001),
        ("ai_max_tool_lines", 201),
        ("ai_review_deadline_seconds", 0),
    ],
)
def test_configuration_limits(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_smoke_defaults_to_no_network(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke", "--pr-url", "https://github.com/owner/repo/pull/1"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings()))

    def forbidden(*args, **kwargs):
        pytest.fail("Dry run invoked live review")

    monkeypatch.setattr(smoke, "execute", forbidden)
    assert smoke.main() == 0
    output = capsys.readouterr().out
    assert "no network requests" in output and '"max_gemini_requests": 4' in output
    assert '"max_initial_context_characters": 20000' in output


@pytest.mark.parametrize("thinking", [None, "low"])
def test_smoke_previews_thinking_and_all_deadlines_without_network(monkeypatch, capsys, thinking):
    import json

    monkeypatch.setattr(sys, "argv", ["smoke", "--pr-url", "https://github.com/owner/repo/pull/1"])
    monkeypatch.setattr(
        Settings, "from_env", classmethod(lambda cls: Settings(ai_thinking_level=thinking))
    )
    assert smoke.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["thinking_level"] == (thinking or "provider_default")
    assert output["model_timeout_seconds"] == 30
    assert output["sdk_server_timeout_seconds"] == 30
    assert output["review_deadline_seconds"] == 120
    assert output["max_gemini_requests"] == 4 and output["max_tool_calls"] == 3

"""Verify actual SDK timeout headers and keep upstream/local deadlines distinct."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from app.ai_models import ReviewFailure
from app.config import Settings
from app.gemini_provider import GeminiSession


def config(**overrides):
    return Settings(
        **(
            {
                "gemini_api_key": "fake-key",
                "gemini_model": "gemini-3.8-flash",
                "gemini_free_tier_confirmed": True,
            }
            | overrides
        )
    )


def response():
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": "Ready"}]}, "finishReason": "STOP"}
        ]
    }


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
@pytest.mark.parametrize(
    "level,expected", [(None, None), ("low", "LOW"), ("medium", "MEDIUM"), ("high", "HIGH")]
)
async def test_production_sdk_wire_configuration(monkeypatch, level, expected, model):
    requests = []

    async def send(client, request, **kwargs):
        requests.append(request)
        return httpx.Response(200, request=request, json=response())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    session = GeminiSession(config(ai_thinking_level=level, gemini_model=model))
    try:
        await session.request(initial_context="untrusted context")
        await session.request(final=True)
        assert len(requests) == 2
        for request in requests:
            assert request.url.path.endswith(f"/models/{model}:generateContent")
            assert request.headers["X-Server-Timeout"] == "30"
            assert request.extensions["timeout"] == {
                "connect": 30.0,
                "read": 30.0,
                "write": 30.0,
                "pool": 30.0,
            }
            generation = json.loads(request.content)["generationConfig"]
            assert generation["maxOutputTokens"] == 4096
            assert "candidateCount" not in generation
            if expected is None:
                assert "thinkingConfig" not in generation
            else:
                # google-genai 1.75 uses the protobuf field name inside this object.
                assert generation["thinkingConfig"] == {"thinking_level": expected}
                assert "thinkingBudget" not in generation["thinkingConfig"]
        assert "tools" in json.loads(requests[0].content)
        assert "tools" not in json.loads(requests[1].content)
        assert (
            json.loads(requests[1].content)["generationConfig"]["responseMimeType"]
            == "application/json"
        )
    finally:
        await session.aclose()


async def test_google_504_is_not_a_local_transport_timeout(monkeypatch):
    calls = []

    async def send(client, request, **kwargs):
        calls.append(request)
        return httpx.Response(
            504,
            request=request,
            json={
                "error": {
                    "code": 504,
                    "status": "DEADLINE_EXCEEDED",
                    "message": "private server details",
                }
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    session = GeminiSession(config())
    try:
        with pytest.raises(ReviewFailure) as caught:
            await session.request(initial_context="context")
        failure = caught.value
        assert failure.http_status == 502 and failure.code == "gemini_upstream_error"
        assert failure.provider_error.http_status == 504
        assert failure.provider_error.api_status == "DEADLINE_EXCEEDED"
        assert failure.provider_error.sdk_exception == "ServerError"
        assert "upstream deadline" in failure.message
        assert "private server details" not in failure.message
        assert len(calls) == 1
    finally:
        await session.aclose()


@pytest.mark.parametrize("kind", ["read", "wall"])
async def test_local_timeout_has_no_invented_google_status(monkeypatch, kind):
    calls = []

    async def send(client, request, **kwargs):
        calls.append(request)
        if kind == "read":
            raise httpx.ReadTimeout("private data", request=request)
        await asyncio.sleep(0.1)
        return httpx.Response(200, request=request, json=response())

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    session = GeminiSession(config(ai_model_timeout_seconds=0.01))
    try:
        with pytest.raises(ReviewFailure) as caught:
            await session.request(initial_context="context")
        failure = caught.value
        assert failure.http_status == 504 and failure.code == "gemini_timeout"
        assert failure.provider_error is None
        assert "private data" not in failure.message
        assert len(calls) == 1
    finally:
        await session.aclose()


@pytest.mark.parametrize("level", ["minimal", "off", "disabled", "LOW", "", 0, False])
def test_invalid_thinking_configuration(level):
    with pytest.raises(ValidationError):
        config(ai_thinking_level=level)


def test_optional_thinking_environment(monkeypatch):
    monkeypatch.setenv("AI_THINKING_LEVEL", "low")
    assert Settings.from_env().ai_thinking_level == "low"
    monkeypatch.setenv("AI_THINKING_LEVEL", "")
    assert Settings.from_env().ai_thinking_level is None

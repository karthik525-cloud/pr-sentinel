import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from google import genai
from google.genai import types

from app.config import Settings
from app.gemini_provider import GeminiSession
from app.main import create_app


def settings(**overrides):
    return Settings(
        **(
            {
                "gemini_api_key": "fake-api-key",
                "gemini_model": "gemini-test",
                "gemini_free_tier_confirmed": True,
            }
            | overrides
        )
    )


def github_response(request):
    if "/contents/" in request.url.path:
        return httpx.Response(
            200,
            json={
                "type": "file",
                "path": "src/a.py",
                "size": 4,
                "encoding": "base64",
                "content": base64.b64encode(b"a\nb\n").decode(),
            },
        )
    if request.url.path.endswith("/files"):
        return httpx.Response(
            200,
            json=[
                {
                    "filename": "src/a.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 1,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ],
        )
    return httpx.Response(
        200,
        json={
            "number": 1,
            "title": "Fix",
            "body": None,
            "changed_files": 1,
            "base": {"sha": "a" * 40, "repo": {"private": False}},
            "head": {"sha": "b" * 40},
        },
    )


def sdk_payload(parts):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}


@pytest.mark.parametrize("model", ["gemini-test", "gemini-3.8-flash", "gemini-3.5-flash-lite"])
def test_ai_endpoint_end_to_end_with_real_sdk_and_mock_http(model):
    model_requests = []
    github_requests = []

    def github(request):
        github_requests.append(request)
        return github_response(request)

    def gemini(request):
        assert request.url.path.endswith(f"/models/{model}:generateContent")
        model_requests.append(json.loads(request.content))
        if len(model_requests) == 1:
            return httpx.Response(
                200,
                json=sdk_payload(
                    [
                        {
                            "functionCall": {
                                "name": "get_file_context",
                                "id": "context-id",
                                "args": {"path": "src/a.py", "start_line": 1, "end_line": 2},
                            },
                            "thoughtSignature": "c2ln",
                        }
                    ]
                ),
            )
        if len(model_requests) == 2:
            return httpx.Response(200, json=sdk_payload([{"text": "Ready"}]))
        return httpx.Response(
            200,
            json=sdk_payload(
                [
                    {
                        "text": json.dumps(
                            {
                                "complete": True,
                                "findings": [
                                    {
                                        "file_path": "src/a.py",
                                        "line_number": 1,
                                        "severity": "medium",
                                        "category": "bug",
                                        "title": "Example issue",
                                        "explanation": "Example consequence.",
                                        "suggested_fix": "Example fix.",
                                    }
                                ],
                            }
                        )
                    }
                ]
            ),
        )

    def factory(config):
        sdk = genai.Client(
            api_key="fake-api-key",
            enterprise=False,
            vertexai=False,
            http_options=types.HttpOptions(
                httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(gemini)),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        return GeminiSession(config, sdk_client=sdk)

    http = httpx.AsyncClient(transport=httpx.MockTransport(github))
    try:
        with TestClient(
            create_app(settings(gemini_model=model), http, session_factory=factory)
        ) as client:
            response = client.post(
                "/review/ai", json={"pr_url": "https://github.com/owner/repo/pull/1"}
            )
            assert response.status_code == 200, response.json()
            result = response.json()
            assert result["model"] == model
            assert result["status"] == "completed" and len(result["report"]["findings"]) == 1
            assert result["usage"]["llm_requests"] == 3 and result["usage"]["tool_calls"] == 1
            assert result["report"]["head_sha"] == "b" * 40
            assert "fake-api-key" not in response.text
            paths = client.get("/openapi.json").json()["paths"]
            assert set(paths) == {"/health", "/review", "/review/ai"}
    finally:
        asyncio.run(http.aclose())
    content_request = next(
        request for request in github_requests if "/contents/" in request.url.path
    )
    assert content_request.url.params["ref"] == "b" * 40
    assert model_requests[1]["contents"][1]["parts"][0]["thoughtSignature"] == "c2ln"
    assert model_requests[1]["contents"][2]["parts"][0]["functionResponse"]["id"] == "context-id"


def test_stage1_operational_without_key_and_ai_disabled():
    http = httpx.AsyncClient(transport=httpx.MockTransport(github_response))
    try:
        with TestClient(create_app(Settings(), http)) as client:
            assert client.get("/health").status_code == 200
            assert (
                client.post(
                    "/review", json={"pr_url": "https://github.com/owner/repo/pull/1"}
                ).status_code
                == 200
            )
            response = client.post(
                "/review/ai", json={"pr_url": "https://github.com/owner/repo/pull/1"}
            )
            assert response.status_code == 503
            assert response.json()["status"] == "error"
            assert response.json()["issues"][0]["code"] == "gemini_not_configured"
    finally:
        asyncio.run(http.aclose())


def test_ai_invalid_url_rejected_before_any_network():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("Network called")))
    try:
        with TestClient(create_app(settings(), http)) as client:
            assert (
                client.post(
                    "/review/ai", json={"pr_url": "https://evil.example/a/b/pull/1"}
                ).status_code
                == 422
            )
    finally:
        asyncio.run(http.aclose())


def test_ai_quota_error_through_api():
    calls = []

    def quota(request):
        calls.append(request)
        return httpx.Response(
            429, json={"error": {"code": 429, "message": "fake-api-key secret error"}}
        )

    def factory(config):
        sdk = genai.Client(
            api_key="fake-api-key",
            enterprise=False,
            vertexai=False,
            http_options=types.HttpOptions(
                httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(quota)),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        return GeminiSession(config, sdk_client=sdk)

    http = httpx.AsyncClient(transport=httpx.MockTransport(github_response))
    try:
        with TestClient(create_app(settings(), http, session_factory=factory)) as client:
            response = client.post(
                "/review/ai", json={"pr_url": "https://github.com/owner/repo/pull/1"}
            )
            assert response.status_code == 429
            assert response.json()["issues"][-1]["code"] == "gemini_quota_exhausted"
            assert "fake-api-key" not in response.text and len(calls) == 1
    finally:
        asyncio.run(http.aclose())

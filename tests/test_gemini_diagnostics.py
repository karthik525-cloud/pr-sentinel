"""Exercise the actual SDK error classes over mocked HTTP, never live Gemini."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from google import genai
from google.genai import errors, types

from app import smoke
from app.ai_models import ReviewFailure
from app.config import Settings
from app.gemini_errors import api_error_diagnostic
from app.gemini_provider import GeminiSession
from app.main import create_app

CASES = [
    (400, "INVALID_ARGUMENT", "gemini_request_rejected", "ClientError"),
    (401, "UNAUTHENTICATED", "gemini_access_denied", "ClientError"),
    (403, "PERMISSION_DENIED", "gemini_access_denied", "ClientError"),
    (404, "NOT_FOUND", "gemini_model_unavailable", "ClientError"),
    (429, "RESOURCE_EXHAUSTED", "gemini_quota_exhausted", "ClientError"),
    (500, "INTERNAL", "gemini_upstream_error", "ServerError"),
    (502, "UNKNOWN", "gemini_upstream_error", "ServerError"),
    (503, "UNAVAILABLE", "gemini_upstream_error", "ServerError"),
    (504, "DEADLINE_EXCEEDED", "gemini_upstream_error", "ServerError"),
]


def settings():
    return Settings(
        gemini_api_key="dummy-gemini-key",
        github_token="dummy-github-token",
        gemini_model="gemini-3.8-flash",
        gemini_free_tier_confirmed=True,
    )


def session_for(handler):
    sdk = genai.Client(
        api_key="dummy-gemini-key",
        enterprise=False,
        vertexai=False,
        http_options=types.HttpOptions(
            httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    return GeminiSession(settings(), sdk_client=sdk)


@pytest.mark.parametrize("status,category,code,exception", CASES)
async def test_sdk_status_category_and_safe_message(status, category, code, exception, caplog):
    caplog.set_level("DEBUG")
    requests = []
    sensitive = "dummy-gemini-key dummy-github-token Authorization: Bearer arbitrary-secret PRIVATE SOURCE CODE"

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={
                "error": {
                    # Deliberately disagree: diagnostics must prefer the actual HTTP status.
                    "code": 418,
                    "status": category,
                    "message": sensitive,
                    "details": [
                        {
                            "request": "private request payload",
                            "headers": {"x-goog-api-key": "dummy-gemini-key"},
                        }
                    ],
                }
            },
        )

    session = session_for(handler)
    try:
        with pytest.raises(ReviewFailure) as caught:
            await session.request(initial_context="PRIVATE SOURCE CODE")
        failure = caught.value
        diagnostic = failure.provider_error
        assert failure.code == code
        assert failure.http_status == (429 if status == 429 else 502)
        assert diagnostic.http_status == status
        assert diagnostic.api_status == category
        assert diagnostic.sdk_exception == exception
        assert diagnostic.message == failure.message
        assert len(requests) == 1
        output = diagnostic.model_dump_json() + str(failure) + caplog.text
        for value in (
            "dummy-gemini-key",
            "dummy-github-token",
            "arbitrary-secret",
            "PRIVATE SOURCE CODE",
            "private request payload",
        ):
            assert value not in output
    finally:
        await session.aclose()


async def test_reproduce_unsupported_parameter_error_and_updated_wire_config():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path.endswith("/models/gemini-3.8-flash:generateContent")
        assert body["generationConfig"]["maxOutputTokens"] == 4096
        assert "candidateCount" not in body["generationConfig"]
        assert body["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "candidate_count is not supported for this model; prompt: PRIVATE CODE; key=dummy-gemini-key",
                }
            },
        )

    session = session_for(handler)
    try:
        with pytest.raises(ReviewFailure) as caught:
            await session.request(initial_context="PRIVATE CODE")
        diagnostic = caught.value.provider_error
        assert diagnostic.message == "Google rejected unsupported parameter candidate_count."
        assert diagnostic.http_status == 400 and diagnostic.api_status == "INVALID_ARGUMENT"
        assert len(requests) == 1
        assert "PRIVATE CODE" not in diagnostic.model_dump_json()
    finally:
        await session.aclose()


def test_allowlisted_reason_but_not_error_metadata():
    error = errors.ClientError(
        400,
        {
            "error": {
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid: private-key",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "metadata": {"key": "private-key"},
                    }
                ],
            }
        },
    )
    diagnostic = api_error_diagnostic(error)
    assert diagnostic.reason == "API_KEY_INVALID"
    assert "private-key" not in diagnostic.model_dump_json()


@pytest.mark.parametrize(
    "message",
    [
        "Authorization: Bearer arbitrary-secret",
        "<html>PRIVATE SOURCE</html>",
        'request={"contents":[{"text":"PRIVATE SOURCE"}]}',
        "https://generativelanguage.googleapis.com?key=dummy-gemini-key",
        "dummy%2Dgemini%2Dkey",
        "PRIVATE SOURCE\n\x1b[31msecret",
        None,
    ],
)
def test_unrecognized_messages_never_echo_raw_text(message):
    diagnostic = api_error_diagnostic(errors.ClientError(400, {"error": {"message": message}}))
    assert diagnostic.message == "Google rejected the request parameters or configuration."
    assert diagnostic.api_status is None


def test_unknown_categories_and_reasons_do_not_echo_input():
    diagnostic = api_error_diagnostic(
        errors.ClientError(
            403,
            {
                "error": {
                    "status": "private-api-key",
                    "message": "private-api-key",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "private-api-key",
                        }
                    ],
                }
            },
        )
    )
    assert diagnostic.api_status is None and diagnostic.reason is None
    assert "private-api-key" not in diagnostic.model_dump_json()


def test_base_api_error_and_newer_category_format():
    diagnostic = api_error_diagnostic(errors.APIError(302, {"message": "redirect secret"}))
    assert diagnostic.http_status == 302 and diagnostic.sdk_exception == "APIError"
    diagnostic = api_error_diagnostic(
        errors.ClientError(
            400,
            {
                "error": {
                    "code": "parameter_unknown",
                    "message": "Unknown parameter temperature",
                }
            },
        )
    )
    assert diagnostic.api_status == "parameter_unknown"
    assert diagnostic.message == "Google rejected unsupported parameter temperature."


@pytest.mark.parametrize("status,category,code,exception", CASES)
def test_diagnostics_survive_agent_and_fastapi(status, category, code, exception):
    calls = []

    def google(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": {"code": status, "status": category, "message": "private request data"}},
        )

    def github(request):
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "a.py",
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
                "title": "PR",
                "body": None,
                "changed_files": 1,
                "base": {"sha": "a" * 40, "repo": {"private": False}},
                "head": {"sha": "b" * 40},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(github))
    try:
        with TestClient(
            create_app(settings(), http, session_factory=lambda _: session_for(google))
        ) as client:
            result = client.post(
                "/review/ai", json={"pr_url": "https://github.com/owner/repo/pull/1"}
            )
            assert result.status_code == (429 if status == 429 else 502)
            body = result.json()
            assert body["status"] == "error" and body["usage"]["llm_requests"] == 1
            issue = body["issues"][-1]
            assert issue["code"] == code
            assert issue["provider_error"]["http_status"] == status
            assert issue["provider_error"]["api_status"] == category
            assert issue["provider_error"]["sdk_exception"] == exception
            assert "private request data" not in result.text
            assert len(calls) == 1
    finally:
        asyncio.run(http.aclose())


async def test_smoke_prints_local_and_upstream_status_without_network(monkeypatch, capsys):
    from app.ai_models import AIReviewResponse, ExecutionIssue
    from app.models import ReviewRequest
    from app.review_agent import ReviewAgent

    diagnostic = api_error_diagnostic(errors.ClientError(404, {"error": {"status": "NOT_FOUND"}}))

    async def run(self, request):
        return 502, AIReviewResponse(
            status="error",
            issues=(
                ExecutionIssue(
                    code="gemini_model_unavailable",
                    message=diagnostic.message,
                    provider_error=diagnostic,
                ),
            ),
        )

    monkeypatch.setattr(ReviewAgent, "run", run)
    code = await smoke.execute(
        settings(), ReviewRequest(pr_url="https://github.com/owner/repo/pull/1")
    )
    output = json.loads(capsys.readouterr().out)
    assert code == 1 and output["http_status"] == 502
    assert output["issues"][0]["provider_error"]["http_status"] == 404
    assert "dummy-gemini-key" not in json.dumps(output)


async def test_single_response_candidate_limit_still_enforced():
    def handler(request):
        candidate = {
            "content": {"role": "model", "parts": [{"text": "Ready"}]},
            "finishReason": "STOP",
        }
        return httpx.Response(200, json={"candidates": [candidate, candidate]})

    session = session_for(handler)
    try:
        with pytest.raises(ReviewFailure) as caught:
            await session.request(initial_context="context")
        assert caught.value.code == "gemini_invalid_response"
    finally:
        await session.aclose()

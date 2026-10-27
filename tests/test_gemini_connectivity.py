import asyncio
import json
import sys

import httpx
import pytest
from pydantic import ValidationError

from app import gemini_connectivity as diagnostic
from app.config import Settings


def decode_records(output):
    decoder = json.JSONDecoder()
    result = []
    remaining = output.strip()
    while remaining:
        record, end = decoder.raw_decode(remaining)
        result.append(record)
        remaining = remaining[end:].lstrip()
    return result


def config(**overrides):
    return Settings(
        **(
            {
                "gemini_api_key": "fake-diagnostic-key",
                "gemini_model": "gemini-3.8-flash",
                "gemini_free_tier_confirmed": True,
            }
            | overrides
        )
    )


def reply(text="OK", reason="STOP"):
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": reason}
        ]
    }


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-3.5-flash-lite"])
@pytest.mark.parametrize("thinking", [None, "low", "medium", "high"])
async def test_exactly_one_minimal_request_and_success(monkeypatch, thinking, model):
    requests = []

    async def transport(self, request):
        requests.append(request)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    ticks = iter((100.0, 100.125))
    monkeypatch.setattr(diagnostic, "perf_counter", lambda: next(ticks))
    result = await diagnostic.execute(config(ai_thinking_level=thinking, gemini_model=model))
    assert result["generation_succeeded"] and result["reply_is_ok"]
    assert result["http_status"] == 200 and result["api_status"] is None
    assert result["elapsed_seconds"] == 0.125 and result["requests_attempted"] == 1
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "generativelanguage.googleapis.com"
    assert request.url.path.endswith(f"/models/{model}:generateContent")
    assert request.headers["X-Server-Timeout"] == "30"
    assert request.extensions["timeout"]["read"] == 30
    body = json.loads(request.content)
    assert set(body) == {"contents", "generationConfig"}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Reply with OK."}]}]
    generation = body["generationConfig"]
    assert generation["maxOutputTokens"] == 4096
    if thinking is None:
        assert set(generation) == {"maxOutputTokens"}
    else:
        assert set(generation) == {"maxOutputTokens", "thinkingConfig"}
        assert generation["thinkingConfig"] == {"thinking_level": thinking.upper()}
    assert "fake-diagnostic-key" not in json.dumps(body)
    assert "fake-diagnostic-key" not in json.dumps(result)


@pytest.mark.parametrize("status,category", [(503, "UNAVAILABLE"), (504, "DEADLINE_EXCEEDED")])
async def test_upstream_error_is_sanitized_and_never_retried(monkeypatch, status, category, caplog):
    caplog.set_level("DEBUG")
    calls = []

    async def transport(self, request):
        calls.append(request)
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": status,
                    "status": category,
                    "message": "fake-diagnostic-key private request Authorization: Bearer private-token",
                    "details": [{"sensitive": "PRIVATE DATA"}],
                }
            },
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    result = await diagnostic.execute(config())
    assert not result["generation_succeeded"] and result["http_status"] == status
    assert result["api_status"] == category and result["sdk_exception"] == "ServerError"
    assert result["error_code"] == "google_api_error" and result["elapsed_seconds"] >= 0
    assert len(calls) == 1
    output = json.dumps(result) + caplog.text
    for value in ("fake-diagnostic-key", "private request", "private-token", "PRIVATE DATA"):
        assert value not in output


@pytest.mark.parametrize("kind", ["httpx", "wall"])
async def test_local_timeouts_have_no_fabricated_http_status(monkeypatch, kind):
    calls = []

    async def transport(self, request):
        calls.append(request)
        if kind == "httpx":
            raise httpx.ReadTimeout("fake-diagnostic-key private error", request=request)
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    result = await diagnostic.execute(config(ai_model_timeout_seconds=0.01))
    assert result["error_code"] == "local_timeout" and not result["generation_succeeded"]
    assert result["http_status"] is None and result["api_status"] is None
    assert len(calls) == 1 and result["elapsed_seconds"] >= 0
    assert "fake-diagnostic-key" not in json.dumps(result)


@pytest.mark.parametrize(
    "settings", [Settings(), config(gemini_model=None), config(gemini_free_tier_confirmed=False)]
)
async def test_invalid_configuration_cannot_send_even_with_execute(settings, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Client created before configuration checks")

    monkeypatch.setattr(diagnostic, "create_gemini_client", forbidden)
    result = await diagnostic.execute(settings)
    assert result["error_code"] == "configuration_error"
    assert result["requests_attempted"] == 0


def test_dry_run_never_constructs_client(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostic"])
    monkeypatch.setattr(
        Settings, "from_env", classmethod(lambda cls: config(ai_thinking_level="low"))
    )
    monkeypatch.setattr(
        diagnostic, "create_gemini_client", lambda *a, **kw: pytest.fail("Dry run created client")
    )
    assert diagnostic.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["phase"] == "local_checks" and plan["status"] == "ready"
    assert plan["mode"] == "dry_run" and plan["requests_sent"] == 0
    assert plan["max_requests"] == 1 and plan["automatic_retries"] is False
    assert plan["thinking_level"] == "low" and "fake-diagnostic-key" not in json.dumps(plan)


def test_execute_cli_reports_plan_before_single_request(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--execute"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: config()))
    calls = []

    async def transport(self, request):
        # Local checks must already have been printed before sending.
        records = decode_records(capsys.readouterr().out)
        assert [record["phase"] for record in records] == ["local_checks", "generation_starting"]
        assert records[0]["will_execute"] is True
        calls.append(request)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    assert diagnostic.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["phase"] == "generation" and result["generation_succeeded"]
    assert result["http_status"] == 200 and len(calls) == 1


@pytest.mark.parametrize("execute", [False, True])
def test_cli_configuration_failure_is_safe_and_network_free(monkeypatch, capsys, execute):
    monkeypatch.setattr(sys, "argv", ["diagnostic"] + (["--execute"] if execute else []))

    def invalid(cls):
        raise ValidationError.from_exception_data(
            "Settings",
            [{"type": "string_type", "loc": ("gemini_model",), "input": "fake-diagnostic-key"}],
        )

    monkeypatch.setattr(Settings, "from_env", classmethod(invalid))
    assert diagnostic.main() == 2
    output = capsys.readouterr().out
    assert json.loads(output)["requests_sent"] == 0
    assert "fake-diagnostic-key" not in output


@pytest.mark.parametrize(
    "payload,success,ok",
    [
        (reply("unexpected fake-diagnostic-key"), True, False),
        (reply("", "MAX_TOKENS"), False, False),
        ({}, False, False),
    ],
)
async def test_generated_text_is_never_printed(monkeypatch, payload, success, ok):
    async def transport(self, request):
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    result = await diagnostic.execute(config())
    assert result["generation_succeeded"] is success and result["reply_is_ok"] is ok
    assert result["http_status"] == 200
    assert "fake-diagnostic-key" not in json.dumps(result)


async def test_sdk_error_never_prints_raw_exception(monkeypatch):
    async def transport(self, request):
        raise RuntimeError("fake-diagnostic-key Authentication: PRIVATE HEADER")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    result = await diagnostic.execute(config())
    assert result["error_code"] == "sdk_error" and result["http_status"] is None
    assert "PRIVATE HEADER" not in json.dumps(result)


@pytest.mark.parametrize(
    "status,category,exit_code",
    [(200, None, 0), (503, "UNAVAILABLE", 1), (504, "DEADLINE_EXCEEDED", 1)],
)
def test_cli_outputs_final_result_for_success_and_google_errors(
    monkeypatch, capsys, status, category, exit_code
):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--execute"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: config()))
    calls = []

    async def transport(self, request):
        calls.append(request)
        payload = (
            reply()
            if status == 200
            else {"error": {"code": status, "status": category, "message": "private-key"}}
        )
        return httpx.Response(status, json=payload)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    assert diagnostic.main() == exit_code
    records = decode_records(capsys.readouterr().out)
    assert [record["phase"] for record in records] == [
        "local_checks",
        "generation_starting",
        "generation",
    ]
    assert records[0]["requests_sent"] == 0 and records[0]["will_execute"] is True
    assert records[-1]["requests_attempted"] == 1 and records[-1]["http_status"] == status
    assert records[-1]["api_status"] == category
    assert "private-key" not in json.dumps(records)
    assert len(calls) == 1


def test_cli_local_timeout_always_prints_final_result(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--execute"])
    monkeypatch.setattr(
        Settings, "from_env", classmethod(lambda cls: config(ai_model_timeout_seconds=0.01))
    )
    calls = []

    async def transport(self, request):
        calls.append(request)
        # The start record must be visible even while the request is awaiting.
        records = decode_records(capsys.readouterr().out)
        assert records[-1]["phase"] == "generation_starting"
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    assert diagnostic.main() == 1
    result = decode_records(capsys.readouterr().out)[-1]
    assert result["phase"] == "generation" and result["error_code"] == "local_timeout"
    assert result["http_status"] is None and len(calls) == 1


@pytest.mark.parametrize(
    "settings",
    [
        config(gemini_api_key=None),
        config(gemini_model=None),
        config(gemini_free_tier_confirmed=False),
    ],
)
def test_execute_flag_explains_configuration_early_exit(monkeypatch, capsys, settings):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--execute"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(
        diagnostic, "create_gemini_client", lambda *a, **kw: pytest.fail("Must not create client")
    )
    assert diagnostic.main() == 2
    records = decode_records(capsys.readouterr().out)
    assert len(records) == 1
    assert records[0]["execution_requested"] is True and records[0]["will_execute"] is False
    assert records[0]["skip_reason"] == "configuration_error"


@pytest.mark.parametrize(
    "exception,code",
    [
        (KeyboardInterrupt, "interrupted"),
        (asyncio.CancelledError, "interrupted"),
        (RuntimeError, "cli_execution_error"),
    ],
)
def test_cli_boundary_failures_print_safe_final_json(monkeypatch, capsys, exception, code):
    monkeypatch.setattr(sys, "argv", ["diagnostic", "--execute"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: config()))

    async def fail(settings):
        raise exception("fake-diagnostic-key private details")

    monkeypatch.setattr(diagnostic, "execute", fail)
    assert diagnostic.main() == (130 if code == "interrupted" else 1)
    output = capsys.readouterr().out
    records = decode_records(output)
    assert records[-1]["phase"] == "generation" and records[-1]["error_code"] == code
    assert records[-1]["requests_attempted"] is None
    assert "fake-diagnostic-key" not in output and "private details" not in output


def test_final_result_explicitly_flushed(monkeypatch):
    import builtins

    monkeypatch.setattr(sys, "argv", ["diagnostic", "--execute"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: config()))

    async def success(settings):
        return {"phase": "generation", "generation_succeeded": True, "reply_is_ok": True}

    monkeypatch.setattr(diagnostic, "execute", success)
    writes = []
    monkeypatch.setattr(
        builtins, "print", lambda value, **kwargs: writes.append((json.loads(value), kwargs))
    )
    assert diagnostic.main() == 0
    assert writes[-1][0]["phase"] == "generation" and writes[-1][1]["flush"] is True


def test_module_entrypoint_executes_and_prints_result_with_mock_http(monkeypatch, capsys):
    import runpy

    monkeypatch.setattr(sys, "argv", ["app.gemini_connectivity", "--execute"])
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: config()))
    # Exercise the same module entrypoint mechanism as `python -m`, without a
    # pre-import warning or any subprocess that could bypass the HTTP mocks.
    monkeypatch.delitem(sys.modules, "app.gemini_connectivity", raising=False)
    calls = []

    async def transport(self, request):
        calls.append(request)
        return httpx.Response(200, json=reply())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("app.gemini_connectivity", run_name="__main__", alter_sys=True)
    assert caught.value.code == 0 and len(calls) == 1
    records = decode_records(capsys.readouterr().out)
    assert [record["phase"] for record in records] == [
        "local_checks",
        "generation_starting",
        "generation",
    ]
    assert records[-1]["generation_succeeded"] is True

import asyncio
import json

import pytest
from pydantic import ValidationError

from app.ai_models import AIReviewResponse, ToolTrace
from app.config import Settings
from app.evaluation.dataset import load_dataset
from app.evaluation.live import context_evidence
from app.llm import ModelTurn, ToolCall
from app.tool_observability import traced_tool
from test_ai_agent import CALL, final_turn, run_case as agent_fixture

run_case = agent_fixture


async def test_successful_tool_trace(run_case):
    status, result, _, _ = await run_case(
        [ModelTurn(calls=(CALL,)), ModelTurn("Ready"), final_turn()]
    )
    assert status == 200
    trace = result.tool_traces[0]
    assert trace.tool_name == "get_file_context" and trace.requested_path == "src/helper.py"
    assert (trace.requested_start_line, trace.requested_end_line) == (1, 2)
    assert (trace.returned_start_line, trace.returned_end_line) == (1, 2)
    assert trace.success and trace.error_code is None and trace.elapsed_seconds >= 0
    assert trace.context_chars == result.usage.tool_context_chars
    assert '"text"' not in trace.model_dump_json()


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "../private", "start_line": 1, "end_line": 2},
        {"path": ".env", "start_line": 1, "end_line": 2},
        {"path": "src/test-gemini-key.py", "start_line": 1, "end_line": 2},
        {"path": "x" * 1025, "start_line": 1, "end_line": 2},
        {"path": "src/a.py", "start_line": True, "end_line": 2},
        {"path": "src/a.py", "start_line": 1, "end_line": 2, "extra": "secret"},
        "test-gemini-key",
    ],
)
async def test_trace_arguments_are_sanitized(arguments):
    traces = []

    async def invoke():
        return {"ok": False, "error": {"code": "invalid_tool_arguments"}}

    await traced_tool(
        ToolCall("get_file_context", arguments),
        Settings(gemini_api_key="test-gemini-key"),
        traces,
        invoke,
    )
    assert traces[0].requested_path is None
    assert traces[0].requested_start_line is None
    assert "test-gemini-key" not in traces[0].model_dump_json()


async def test_unknown_tool_and_unknown_error_never_echo_values():
    traces = []

    async def invoke():
        return {"ok": False, "error": {"code": "secret-value", "message": "secret-message"}}

    await traced_tool(
        ToolCall("secret-tool-name", {"path": "secret-path"}), Settings(), traces, invoke
    )
    assert traces[0].tool_name == "unknown_tool"
    assert traces[0].error_code == "tool_execution_error"
    assert "secret" not in traces[0].model_dump_json()


@pytest.mark.parametrize(
    "error", [RuntimeError("credential"), TimeoutError("credential"), asyncio.CancelledError()]
)
async def test_exception_and_cancellation_leave_safe_trace(error):
    traces = []

    async def invoke():
        raise error

    with pytest.raises(type(error)):
        await traced_tool(CALL, Settings(), traces, invoke)
    assert len(traces) == 1 and traces[0].success is False
    assert traces[0].context_chars == 0
    assert "credential" not in traces[0].model_dump_json()


async def test_rate_limit_trace_survives_agent_error(run_case):
    status, result, _, _ = await run_case([ModelTurn(calls=(CALL,))], tool_status=429)
    assert status == 429 and not result.tool_traces[0].success
    assert result.tool_traces[0].error_code == "github_rate_limited"


async def test_failure_trace_and_budget_rejection(run_case):
    status, result, _, _ = await run_case(
        [ModelTurn(calls=(CALL,)), ModelTurn("Ready"), final_turn()], tool_status=404
    )
    assert status == 422 and not result.tool_traces[0].success
    assert result.tool_traces[0].context_chars == 0
    _, result, _, _ = await run_case([ModelTurn(calls=(CALL,) * 4)])
    assert result.tool_traces == () and result.usage.tool_calls == 0


async def test_trace_bound():
    traces = []

    async def invoke():
        return {"ok": False, "error": {"code": "file_not_found"}}

    for _ in range(10):
        await traced_tool(CALL, Settings(), traces, invoke)
    assert len(traces) == 3
    with pytest.raises(ValidationError):
        AIReviewResponse(status="completed", tool_traces=tuple(traces * 2))


def test_legacy_and_new_context_evidence():
    case = next(c for c in load_dataset().cases if c.id == "clean-context")
    old = AIReviewResponse.model_validate_json('{"status":"completed"}')
    assert context_evidence(case, old.model_dump())["all_required_files_retrieved"] is None
    assert context_evidence(case, {"tool_traces": []})["all_required_files_retrieved"] is False
    trace = ToolTrace(
        tool_name="get_file_context",
        requested_path="src/roles.py",
        requested_start_line=1,
        requested_end_line=2,
        returned_start_line=1,
        returned_end_line=2,
        success=True,
        context_chars=100,
        elapsed_seconds=0.01,
    )
    assert (
        context_evidence(case, {"tool_traces": [trace.model_dump()]})[
            "all_required_files_retrieved"
        ]
        is True
    )
    assert (
        context_evidence(
            case, {"tool_traces": [trace.model_copy(update={"success": False}).model_dump()]}
        )["all_required_files_retrieved"]
        is False
    )
    assert (
        context_evidence(
            case,
            {
                "tool_traces": [
                    trace.model_copy(update={"requested_path": "src/service.py"}).model_dump()
                ]
            },
        )["all_required_files_retrieved"]
        is False
    )


async def test_traces_reach_evaluation_record(tmp_path):
    from test_evaluation import Session, settings
    from app.evaluation.live import execute

    dataset = load_dataset()
    call = ToolCall("get_file_context", {"path": "src/roles.py", "start_line": 1, "end_line": 2})
    session = Session([ModelTurn(calls=(call,)), ModelTurn("Ready"), final_turn()])
    result = await execute(
        dataset,
        settings(),
        ["clean-context"],
        tmp_path / "run",
        approved=True,
        session_factory=lambda _: session,
    )
    record = result["records"][0]
    assert record["context_evidence"]["all_required_files_retrieved"] is True
    traces = record["raw_review"]["tool_traces"]
    assert traces[0]["requested_path"] == "src/roles.py"
    assert "return bool(user.admin_flag)" not in json.dumps(traces)

"""Bounded tool metadata only: never persist source, arbitrary arguments, or exception text."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.ai_models import ToolTrace
from app.config import Settings
from app.context_security import contains_secret, sensitive_path
from app.context_tool import FileContextArguments
from app.llm import ToolCall

ERROR_CODES = frozenset(
    {
        "unknown_tool",
        "invalid_tool_arguments",
        "sensitive_path",
        "too_many_lines",
        "sensitive_content",
        "line_out_of_range",
        "context_too_large",
        "file_not_found",
        "file_too_large",
        "unsupported_file_type",
        "binary_file",
        "invalid_utf8",
        "github_rate_limited",
        "github_access_denied",
        "github_authentication_failed",
        "invalid_upstream_response",
        "github_timeout",
        "github_unavailable",
    }
)


async def traced_tool(
    call: ToolCall,
    settings: Settings,
    traces: list[ToolTrace],
    invoke: Callable[[], Awaitable[dict[str, object]]],
) -> dict[str, object]:
    path = start = end = None
    name = "get_file_context" if call.name == "get_file_context" else "unknown_tool"
    if name == "get_file_context":
        try:
            args = FileContextArguments.model_validate(call.arguments)
            if not sensitive_path(args.path) and not contains_secret(args.path, settings):
                path, start, end = args.path, args.start_line, args.end_line
        except ValidationError:
            pass  # Do not echo even partly valid arguments from malformed requests.
    began = time.perf_counter()
    success = False
    chars = 0
    code = "tool_execution_error"
    returned_start = returned_end = None
    try:
        payload = await invoke()
        success = payload.get("ok") is True
        if success:
            code = None
            chars = len(json.dumps(payload, ensure_ascii=False))
            returned_start, returned_end = payload["start_line"], payload["end_line"]
        else:
            candidate = payload.get("error", {}).get("code")
            code = candidate if candidate in ERROR_CODES else "tool_execution_error"
        return payload
    except (asyncio.CancelledError, TimeoutError):
        code = "tool_cancelled_or_timeout"
        raise
    except Exception as exc:
        candidate = getattr(exc, "code", None)
        code = candidate if candidate in ERROR_CODES else "tool_execution_error"
        raise
    finally:
        # Agent budgets prevent >3 attempts. Defense in depth bounds retained metadata.
        if len(traces) < min(settings.ai_max_tool_calls, 3):
            traces.append(
                ToolTrace(
                    tool_name=name,
                    requested_path=path,
                    requested_start_line=start,
                    requested_end_line=end,
                    success=success,
                    context_chars=chars,
                    elapsed_seconds=max(0.0, time.perf_counter() - began),
                    error_code=code,
                    returned_start_line=returned_start,
                    returned_end_line=returned_end,
                )
            )

"""Dry-run-first connectivity check: one fixed prompt, no review workflow."""

import argparse
import asyncio
import json
import math
from time import perf_counter

import httpx
from google.genai import errors, types
from pydantic import ValidationError

from app.ai_models import ReviewFailure
from app.config import Settings
from app.gemini_errors import api_error_diagnostic
from app.gemini_provider import create_gemini_client, require_ai_configuration

PROMPT = "Reply with OK."


def check_configuration(settings: Settings) -> dict[str, object]:
    checks = {
        "settings_valid": True,
        "api_key_configured": bool(
            settings.gemini_api_key and settings.gemini_api_key.get_secret_value().strip()
        ),
        "model_configured": settings.gemini_model is not None,
        "free_tier_confirmed": settings.gemini_free_tier_confirmed,
    }
    result = {
        "phase": "local_checks",
        "status": "ready",
        "checks": checks,
        "model": settings.gemini_model,
        "thinking_level": settings.ai_thinking_level or "provider_default",
        "prompt": PROMPT,
        "max_requests": 1,
        "requests_sent": 0,
        "timeout_seconds": settings.ai_model_timeout_seconds,
        "sdk_server_timeout_seconds": math.ceil(
            int(settings.ai_model_timeout_seconds * 1000) / 1000
        ),
        "max_output_tokens": settings.ai_max_output_tokens,
        "automatic_retries": False,
    }
    try:
        require_ai_configuration(settings)
    except ReviewFailure as exc:
        result.update(status="configuration_error", error_code=exc.code, message=exc.message)
    return result


async def execute(settings: Settings) -> dict[str, object]:
    """One SDK generate call, no retries, repair, GitHub lookup or follow-up."""
    result: dict[str, object] = {
        "phase": "generation",
        "status": "error",
        "generation_succeeded": False,
        "reply_is_ok": False,
        "requests_attempted": 0,
        "http_status": None,
        "api_status": None,
        "elapsed_seconds": 0.0,
    }
    if check_configuration(settings)["status"] != "ready":
        result.update(
            error_code="configuration_error", message="Local configuration checks failed."
        )
        return result
    client = None
    started = perf_counter()

    async def capture_status(response: httpx.Response) -> None:
        result["http_status"] = response.status_code  # No headers/body retained.

    try:
        client = create_gemini_client(settings, response_hook=capture_status)
        config = types.GenerateContentConfig(
            max_output_tokens=settings.ai_max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if settings.ai_thinking_level is not None:
            config.thinking_config = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(settings.ai_thinking_level.upper()),
            )
        async with asyncio.timeout(settings.ai_model_timeout_seconds):
            result["requests_attempted"] = 1
            response = await client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=PROMPT,
                config=config,
            )
        candidates = response.candidates or []
        parts = (
            candidates[0].content.parts if len(candidates) == 1 and candidates[0].content else []
        )
        text = "".join(part.text for part in parts or [] if part.text and not part.thought)
        success = (
            len(candidates) == 1
            and candidates[0].finish_reason == types.FinishReason.STOP
            and bool(text.strip())
            and not any(part.function_call for part in parts or [])
        )
        result.update(
            status="success" if success else "error",
            generation_succeeded=success,
            reply_is_ok=success and text.strip() == "OK",
        )
        if not success:
            result.update(
                error_code="generation_incomplete",
                message="Google returned no complete text generation.",
            )
        # Never print generated text, even on an unexpected response.
    except (TimeoutError, httpx.TimeoutException):
        result.update(
            error_code="local_timeout",
            message="Local model-request timeout expired; no retry was attempted.",
        )
    except errors.APIError as exc:
        diagnostic = api_error_diagnostic(exc)
        result.update(
            http_status=diagnostic.http_status,
            api_status=diagnostic.api_status,
            sdk_exception=diagnostic.sdk_exception,
            reason=diagnostic.reason,
            error_code="google_api_error",
            message=diagnostic.message,
        )
    except Exception:
        result.update(
            error_code="sdk_error",
            message="SDK or transport processing failed; sensitive details were withheld.",
        )
    finally:
        # Measure setup/request/response processing, excluding resource cleanup.
        result["elapsed_seconds"] = round(perf_counter() - started, 4)
        if client is not None:
            try:
                await asyncio.wait_for(client.aio.aclose(), timeout=2)
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check Gemini configuration locally; --execute approves exactly one generation attempt."
    )
    parser.add_argument(
        "--execute", action="store_true", help="Send the fixed prompt once; no retry or follow-up."
    )
    args = parser.parse_args()
    try:
        settings = Settings.from_env()  # Same environment loading as the application.
    except (ValidationError, ValueError):
        # Pydantic exception strings may include original environment values.
        print(
            json.dumps(
                {
                    "phase": "local_checks",
                    "status": "configuration_error",
                    "requests_sent": 0,
                    "error_code": "invalid_configuration",
                    "message": "Invalid project environment configuration; values withheld.",
                },
                indent=2,
            )
        )
        return 2
    plan = check_configuration(settings)
    plan["mode"] = "execute" if args.execute else "dry_run"
    plan["execution_requested"] = args.execute
    plan["will_execute"] = args.execute and plan["status"] == "ready"
    if not plan["will_execute"]:
        plan["skip_reason"] = "configuration_error" if plan["status"] != "ready" else "dry_run"
    print(json.dumps(plan, indent=2), flush=True)
    if plan["status"] != "ready":
        return 2
    if not args.execute:
        return 0
    print(
        json.dumps(
            {
                "phase": "generation_starting",
                "status": "starting",
                "message": "Starting one generation attempt; wait for the final generation result.",
                "timeout_seconds": settings.ai_model_timeout_seconds,
            },
            indent=2,
        ),
        flush=True,
    )
    try:
        result = asyncio.run(execute(settings))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Cancellation does not establish whether Google received the request.
        print(
            json.dumps(
                {
                    "phase": "generation",
                    "status": "interrupted",
                    "generation_succeeded": False,
                    "requests_attempted": None,
                    "http_status": None,
                    "api_status": None,
                    "error_code": "interrupted",
                    "message": "Connectivity check interrupted; upstream completion is unknown. No retry was attempted.",
                },
                indent=2,
            ),
            flush=True,
        )
        return 130
    except Exception:
        print(
            json.dumps(
                {
                    "phase": "generation",
                    "status": "error",
                    "generation_succeeded": False,
                    "requests_attempted": None,
                    "http_status": None,
                    "api_status": None,
                    "error_code": "cli_execution_error",
                    "message": "Connectivity execution failed outside normal request handling; sensitive details were withheld.",
                },
                indent=2,
            ),
            flush=True,
        )
        return 1
    print(json.dumps(result, indent=2), flush=True)
    return 0 if result["generation_succeeded"] and result["reply_is_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""One bounded agent; deterministic validation remains the acceptance boundary."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict

from pydantic import ValidationError

from app.ai_models import (
    AIReviewResponse,
    CandidateEnvelope,
    ExecutionIssue,
    ReviewFailure,
    ReviewUsage,
    ToolTrace,
)
from app.config import Settings
from app.context_security import contains_secret, contains_secret_data, sensitive_path
from app.context_tool import get_file_context, tool_error
from app.diff_parser import ParsedReview, parse_review
from app.finding_validator import validate_findings
from app.github import GitHubClient, GitHubError
from app.tool_observability import traced_tool
from app.llm import ReviewSession, ToolResult
from app.models import ReviewRequest, ReviewResponse

SessionFactory = Callable[[Settings], ReviewSession]


def build_review_context(review: ReviewResponse, parsed: ParsedReview, settings: Settings) -> str:
    # Entire context is accepted or rejected, never silently sliced by characters.
    if any(sensitive_path(file.path) for file in review.files):
        raise ReviewFailure(
            422,
            "sensitive_path",
            "A changed credential/configuration path is excluded from AI context.",
            incomplete=True,
        )
    raw_fields = [review.title, review.description or ""]
    for file in review.files:
        raw_fields.extend((file.path, file.previous_path or "", file.patch or ""))
    if any(contains_secret(text, settings) for text in raw_fields):
        raise ReviewFailure(
            422,
            "sensitive_content",
            "Potential credentials detected; repository context was withheld.",
            incomplete=True,
        )
    context = json.dumps(
        {
            "untrusted_repository_data": {
                "owner": review.owner,
                "repository": review.repository,
                "number": review.number,
                "title": review.title,
                "description": review.description,
                "base_sha": review.base_sha,
                "head_sha": review.head_sha,
                "files": [asdict(file) for file in parsed.files],
            },
            "limits": {
                "tool_calls": settings.ai_max_tool_calls,
                "tool_lines": settings.ai_max_tool_lines,
                "candidate_findings": settings.ai_max_candidates,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(context) > settings.ai_max_context_chars:
        raise ReviewFailure(
            413,
            "initial_context_too_large",
            "Complete PR context exceeds the character limit; no partial review was started.",
            incomplete=True,
        )
    if contains_secret(context, settings):
        raise ReviewFailure(
            422,
            "sensitive_content",
            "Potential credentials detected; repository context was withheld.",
            incomplete=True,
        )
    return context


def _strict_json(text: str) -> object:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)


class ReviewAgent:
    def __init__(self, github: GitHubClient, settings: Settings, session_factory: SessionFactory):
        self.github = github
        self.settings = settings
        self.session_factory = session_factory

    async def run(self, request: ReviewRequest) -> tuple[int, AIReviewResponse]:
        usage = ReviewUsage()
        traces: list[ToolTrace] = []
        session: ReviewSession | None = None
        issues: list[ExecutionIssue] = []
        try:
            async with asyncio.timeout(self.settings.ai_review_deadline_seconds):
                # Provider construction validates configuration but performs no network I/O.
                session = self.session_factory(self.settings)
                retrieved = await self.github.retrieve(request)
                parsed = parse_review(retrieved)
                issues = [
                    ExecutionIssue(
                        code=file.issue.code if file.issue else "patch_unavailable",
                        message="A changed file has no reliable complete diff mapping.",
                        file_path=None if contains_secret(file.path, self.settings) else file.path,
                    )
                    for file in parsed.files
                    if file.patch_state != "parsed"
                ]
                if issues:
                    return 422, AIReviewResponse(
                        status="incomplete",
                        model=self.settings.gemini_model,
                        issues=tuple(issues),
                        usage=usage,
                        tool_traces=tuple(traces),
                    )
                context = build_review_context(retrieved, parsed, self.settings)
                usage = usage.model_copy(update={"initial_context_chars": len(context)})
                if self.settings.ai_max_llm_requests < 2:
                    raise ReviewFailure(
                        409,
                        "llm_request_limit",
                        "Analysis and final structured output require at least two model requests.",
                        incomplete=True,
                    )
                pending: tuple[ToolResult, ...] = ()
                initial: str | None = context
                # Reserve one request for structured output. Any model-returned
                # tool batch is budgeted atomically before a single call executes.
                while usage.llm_requests < self.settings.ai_max_llm_requests - 1:
                    usage = usage.model_copy(update={"llm_requests": usage.llm_requests + 1})
                    turn = await session.request(initial_context=initial, tool_results=pending)
                    initial = None
                    pending = ()
                    if not turn.calls:
                        break
                    if len(turn.calls) > self.settings.ai_max_tool_calls - usage.tool_calls:
                        raise ReviewFailure(
                            409,
                            "tool_call_limit",
                            "Model requested more tools than the remaining budget; the batch was not executed.",
                            incomplete=True,
                        )
                    results: list[ToolResult] = []
                    for call in turn.calls:
                        usage = usage.model_copy(update={"tool_calls": usage.tool_calls + 1})

                        async def invoke():
                            if call.name != "get_file_context":
                                return tool_error(
                                    "unknown_tool", "Only get_file_context is allowed."
                                )
                            return await get_file_context(
                                self.github, retrieved, self.settings, call.arguments
                            )

                        payload = await traced_tool(call, self.settings, traces, invoke)
                        if payload.get("ok") is not True:
                            error = payload["error"]
                            issues.append(
                                ExecutionIssue(code=error["code"], message=error["message"])
                            )
                        size = len(json.dumps(payload, ensure_ascii=False))
                        usage = usage.model_copy(
                            update={"tool_context_chars": usage.tool_context_chars + size}
                        )
                        results.append(ToolResult(call, payload))
                    pending = tuple(results)
                    if usage.tool_calls == self.settings.ai_max_tool_calls:
                        break
                usage = usage.model_copy(update={"llm_requests": usage.llm_requests + 1})
                final = await session.request(tool_results=pending, final=True)
                if final.calls:
                    raise ReviewFailure(
                        502, "unexpected_final_tool_call", "Tools are disabled during final output."
                    )
                if len(final.text) > self.settings.ai_max_response_chars:
                    raise ReviewFailure(
                        413,
                        "model_response_too_large",
                        "Final output exceeds the character budget.",
                        incomplete=True,
                    )
                if contains_secret(final.text, self.settings):
                    raise ReviewFailure(
                        502,
                        "sensitive_model_output",
                        "Potential credentials detected in model output; output was withheld.",
                    )
                try:
                    raw = _strict_json(final.text)
                    if (
                        isinstance(raw, dict)
                        and isinstance(raw.get("findings"), list)
                        and len(raw["findings"]) > self.settings.ai_max_candidates
                    ):
                        raise ReviewFailure(
                            413,
                            "candidate_limit",
                            "Gemini exceeded the candidate finding limit.",
                            incomplete=True,
                        )
                    envelope = CandidateEnvelope.model_validate(raw)
                except (ValueError, ValidationError, RecursionError):
                    raise ReviewFailure(
                        502,
                        "invalid_model_json",
                        "Gemini did not return the required complete JSON envelope.",
                    ) from None
                if contains_secret_data(envelope.findings, self.settings):
                    raise ReviewFailure(
                        502,
                        "sensitive_model_output",
                        "Potential credentials detected in model output; output was withheld.",
                    )
                report = validate_findings(parsed, envelope.findings)
                if not envelope.complete:
                    issues.append(
                        ExecutionIssue(
                            code="model_review_incomplete",
                            message="Gemini reported that the review could not be completed.",
                        )
                    )
                if issues:
                    return 422, AIReviewResponse(
                        status="incomplete",
                        model=self.settings.gemini_model,
                        report=report,
                        issues=tuple(issues),
                        usage=usage,
                        tool_traces=tuple(traces),
                    )
                return 200, AIReviewResponse(
                    status="completed",
                    model=self.settings.gemini_model,
                    report=report,
                    usage=usage,
                    tool_traces=tuple(traces),
                )
        except TimeoutError:
            failure = ReviewFailure(
                504,
                "review_deadline_exceeded",
                "The overall review deadline expired.",
                incomplete=True,
            )
        except GitHubError as exc:
            failure = ReviewFailure(exc.status, exc.code, exc.message)
        except ReviewFailure as exc:
            failure = exc
        except Exception:
            failure = ReviewFailure(
                502,
                "review_execution_error",
                "Review execution failed; internal details were withheld.",
            )
        finally:
            if session is not None:
                try:
                    await asyncio.wait_for(session.aclose(), timeout=2)
                except Exception:
                    pass  # Cleanup must not replace the safe review result.
        issues.append(
            ExecutionIssue(
                code=failure.code,
                message=failure.message,
                provider_error=failure.provider_error,
            )
        )
        return failure.http_status, AIReviewResponse(
            status="incomplete" if failure.incomplete else "error",
            model=self.settings.gemini_model,
            issues=tuple(issues),
            usage=usage,
            tool_traces=tuple(traces),
        )

"""The only agent tool. Repository identity and head SHA never come from the model."""

import json

from pydantic import Field, ValidationError, field_validator, model_validator

from app.config import Settings
from app.context_security import contains_secret, safe_repository_path, sensitive_path
from app.findings import StrictModel
from app.github import GitHubClient, GitHubError
from app.models import ReviewResponse


class FileContextArguments(StrictModel):
    path: str = Field(min_length=1, max_length=1024)
    start_line: int = Field(ge=1, le=2_147_483_647)
    end_line: int = Field(ge=1, le=2_147_483_647)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        if not safe_repository_path(value):
            raise ValueError("Unsafe repository path")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "FileContextArguments":
        if self.end_line < self.start_line:
            raise ValueError("End line precedes start line")
        return self


def tool_error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


async def get_file_context(
    github: GitHubClient,
    review: ReviewResponse,
    settings: Settings,
    arguments: object,
) -> dict[str, object]:
    try:
        args = FileContextArguments.model_validate(arguments)
    except ValidationError:
        return tool_error(
            "invalid_tool_arguments",
            "Provide a safe relative path and strict positive integer line range.",
        )
    if sensitive_path(args.path):
        return tool_error(
            "sensitive_path", "Credential/configuration paths are excluded from AI context."
        )
    if args.end_line - args.start_line + 1 > settings.ai_max_tool_lines:
        return tool_error("too_many_lines", "Requested range exceeds the tool line limit.")
    try:
        text = await github.get_file_at_head(review, args.path)
    except GitHubError as exc:
        # Rate limits/timeouts are execution failures, not reasons to spend more Gemini requests.
        if exc.status in {429, 502, 504}:
            raise
        return tool_error(exc.code, exc.message)
    if contains_secret(text, settings):
        return tool_error(
            "sensitive_content", "Potential credentials detected; file context was withheld."
        )
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if args.start_line > len(lines):
        return tool_error(
            "line_out_of_range", "Requested start line is beyond the end of the file."
        )
    end = min(args.end_line, len(lines))
    payload = {
        "ok": True,
        "path": args.path,
        "head_sha": review.head_sha,
        "start_line": args.start_line,
        "end_line": end,
        "total_lines": len(lines),
        "lines": [
            {"number": number, "text": lines[number - 1]}
            for number in range(args.start_line, end + 1)
        ],
        "untrusted_data": True,
    }
    if len(json.dumps(payload, ensure_ascii=False)) > settings.ai_max_tool_context_chars:
        return tool_error(
            "context_too_large", "Requested source context exceeds the character limit."
        )
    return payload

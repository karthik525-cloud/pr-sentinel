"""Strict candidate and deterministic validation result schemas for a future agent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import SHA

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["bug", "security", "performance", "reliability"]
RejectionCode = Literal[
    "schema_invalid",
    "file_not_in_pr",
    "deleted_file",
    "patch_unavailable",
    "patch_malformed",
    "line_not_in_diff",
    "line_not_added",
    "duplicate_finding",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class ReviewFinding(StrictModel):
    file_path: str = Field(min_length=1, max_length=4096)
    line_number: int = Field(ge=1, le=2_147_483_647)
    severity: Severity
    category: Category
    title: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=4000)
    suggested_fix: str = Field(min_length=1, max_length=4000)

    @field_validator("file_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        # Keep exact repository paths: do not normalize, case-fold or alias renames.
        if not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError("File path must be nonblank and contain no NUL or line breaks")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("Title must be a single line")
        return value

    @field_validator("title", "explanation", "suggested_fix")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Text must be nonblank and contain no NUL")
        return value


class SchemaIssue(StrictModel):
    location: tuple[str | int, ...]
    code: str
    message: str


class RejectedFinding(StrictModel):
    candidate_index: Annotated[int, Field(ge=0)]
    code: RejectionCode
    message: str
    schema_issues: tuple[SchemaIssue, ...] = ()
    # Index in the original input, not in the accepted findings array.
    duplicate_of: Annotated[int, Field(ge=0)] | None = None


class ReviewReport(StrictModel):
    owner: str
    repository: str
    number: Annotated[int, Field(ge=1)]
    base_sha: SHA
    head_sha: SHA
    validation_scope: Literal["schema_and_diff_location"] = "schema_and_diff_location"
    findings: tuple[ReviewFinding, ...]
    rejected: tuple[RejectedFinding, ...]

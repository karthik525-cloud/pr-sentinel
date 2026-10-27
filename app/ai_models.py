"""AI execution status is separate from structural finding validation."""

from typing import Literal

from pydantic import Field

from app.findings import ReviewFinding, ReviewReport, StrictModel


class CandidateEnvelope(StrictModel):
    """Transport shape; individual candidates are validated by Stage 2."""

    complete: bool
    findings: list[object] = Field(max_length=10)


class StructuredFindings(StrictModel):
    """Schema advertised to Gemini; still independently validated after generation."""

    complete: bool
    findings: list[ReviewFinding] = Field(max_length=10)


class ProviderErrorDiagnostic(StrictModel):
    http_status: int | None = Field(default=None, ge=100, le=599)
    api_status: str | None = None
    reason: str | None = None
    sdk_exception: Literal["APIError", "ClientError", "ServerError"]
    message: str = Field(max_length=600)


class ExecutionIssue(StrictModel):
    code: str
    message: str
    file_path: str | None = None
    provider_error: ProviderErrorDiagnostic | None = None


class ReviewUsage(StrictModel):
    llm_requests: int = 0
    tool_calls: int = 0
    initial_context_chars: int = 0
    tool_context_chars: int = 0


class ToolTrace(StrictModel):
    tool_name: Literal["get_file_context", "unknown_tool"]
    requested_path: str | None = Field(default=None, max_length=1024)
    requested_start_line: int | None = Field(default=None, ge=1, le=2_147_483_647)
    requested_end_line: int | None = Field(default=None, ge=1, le=2_147_483_647)
    success: bool
    context_chars: int = Field(ge=0, le=20000)
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    error_code: str | None = Field(default=None, max_length=64)
    returned_start_line: int | None = Field(default=None, ge=1, le=2_147_483_647)
    returned_end_line: int | None = Field(default=None, ge=1, le=2_147_483_647)


class AIReviewResponse(StrictModel):
    status: Literal["completed", "incomplete", "error"]
    model: str | None = None
    report: ReviewReport | None = None
    issues: tuple[ExecutionIssue, ...] = ()
    usage: ReviewUsage = Field(default_factory=ReviewUsage)
    # None means legacy/unrecorded; an empty tuple means no tools were attempted.
    tool_traces: tuple[ToolTrace, ...] | None = Field(default=None, max_length=3)


class ReviewFailure(Exception):
    def __init__(
        self,
        http_status: int,
        code: str,
        message: str,
        *,
        incomplete: bool = False,
        provider_error: ProviderErrorDiagnostic | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message
        self.incomplete = incomplete
        self.provider_error = provider_error

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Accept only a canonical public github.com PR route, with optional trailing slash.
PR_PATTERN = re.compile(
    r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9_.-]{1,100})/pull/([1-9][0-9]{0,9})/?",
    flags=re.ASCII,
)
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
SHA = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{40}$")]


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pr_url: str = Field(strict=True, max_length=250)

    @field_validator("pr_url")
    @classmethod
    def validate_pr_url(cls, value: str) -> str:
        match = PR_PATTERN.fullmatch(value)
        if not match or match[2] in {".", ".."}:
            raise ValueError("Expected https://github.com/{owner}/{repo}/pull/{number}")
        return value.rstrip("/")


class CommitRef(BaseModel):
    sha: SHA


class Repository(BaseModel):
    private: Literal[False]


class BaseRef(CommitRef):
    repo: Repository


class PullMetadata(BaseModel):
    model_config = ConfigDict(strict=True)
    number: int = Field(ge=1)
    title: str
    body: str | None
    base: BaseRef
    head: CommitRef
    changed_files: NonNegativeInt


class GitHubFile(BaseModel):
    model_config = ConfigDict(strict=True)
    filename: str = Field(min_length=1)
    status: Literal["added", "removed", "modified", "renamed", "copied", "changed", "unchanged"]
    additions: NonNegativeInt
    deletions: NonNegativeInt
    patch: str | None = None
    previous_filename: str | None = None


class ChangedFile(BaseModel):
    path: str
    status: str
    additions: int
    deletions: int
    patch: str | None
    patch_available: bool
    previous_path: str | None = None


class ReviewResponse(BaseModel):
    owner: str
    repository: str
    number: int
    title: str
    description: str | None
    base_sha: str
    head_sha: str
    changed_files: int
    total_diff_bytes: int
    files: list[ChangedFile]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail

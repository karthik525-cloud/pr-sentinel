import os
from typing import Literal

from pydantic import BaseModel, Field, SecretStr


class Settings(BaseModel):
    github_token: SecretStr | None = None
    max_changed_files: int = Field(default=500, ge=1, le=3000)
    max_diff_bytes: int = Field(default=1_000_000, ge=1)
    max_upstream_bytes: int = Field(default=8_000_000, ge=1)

    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = Field(default=None, pattern=r"^gemini-[a-z0-9][a-z0-9.-]{0,100}$")
    gemini_free_tier_confirmed: bool = False
    ai_max_tool_calls: int = Field(default=3, ge=0, le=3)
    ai_max_llm_requests: int = Field(default=4, ge=1, le=4)
    ai_max_context_chars: int = Field(default=20_000, ge=1, le=20_000)
    ai_max_tool_lines: int = Field(default=200, ge=1, le=200)
    ai_max_candidates: int = Field(default=10, ge=1, le=10)
    ai_thinking_level: Literal["low", "medium", "high"] | None = None
    ai_max_output_tokens: int = Field(default=4096, ge=128, le=8192)
    ai_model_timeout_seconds: float = Field(default=30, gt=0, le=60)
    ai_review_deadline_seconds: float = Field(default=120, gt=0, le=180)
    ai_max_file_bytes: int = Field(default=262_144, ge=1, le=1_000_000)
    ai_max_tool_context_chars: int = Field(default=10_000, ge=1, le=20_000)
    ai_max_request_chars: int = Field(default=160_000, ge=1, le=200_000)
    ai_max_response_chars: int = Field(default=32_000, ge=1, le=64_000)

    @classmethod
    def from_env(cls) -> "Settings":
        values = {
            field: os.environ[env]
            for field, env in {
                "max_changed_files": "MAX_CHANGED_FILES",
                "max_diff_bytes": "MAX_DIFF_BYTES",
                "max_upstream_bytes": "MAX_UPSTREAM_BYTES",
            }.items()
            if env in os.environ
        }
        for field in cls.model_fields:
            if field.startswith("ai_") or field == "gemini_free_tier_confirmed":
                if field.upper() in os.environ:
                    value = os.environ[field.upper()]
                    values[field] = (
                        None if field == "ai_thinking_level" and not value.strip() else value
                    )
        return cls(
            github_token=os.getenv("GITHUB_TOKEN") or None,
            gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
            gemini_model=os.getenv("GEMINI_MODEL") or None,
            **values,
        )

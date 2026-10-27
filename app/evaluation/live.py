"""Opt-in benchmark execution through the unchanged production ReviewAgent."""

import hashlib
import json
import platform
from importlib.metadata import version
import time
from pathlib import Path

from app.config import Settings
from app.context_security import contains_secret_data
from app.evaluation.dataset import Case, Dataset, dataset_digest, review_for
from app.evaluation.scoring import PredictionCase, Predictions
from app.gemini_provider import GeminiSession, require_ai_configuration
from app.github import GitHubError
from app.models import ReviewRequest, ReviewResponse
from app.llm import ModelTurn, ReviewSession
from app.review_agent import SessionFactory
from app.review_agent import ReviewAgent, _strict_json

MAX_LIVE_CASES = 3
# Pinned author-reviewed synthetic fixture. Custom datasets are offline-only.
TRUSTED_DATASET_SHA256 = "efa2e42a4554b6ef4a9892728f32f07b60ca03e6c2a75d8cfb617f12c2112c7f"


class FixtureGitHub:
    """No HTTP client and no filesystem resolution of model-supplied paths."""

    def __init__(self, case: Case, settings: Settings):
        self.case = case
        self.settings = settings
        self.review = review_for(case)

    async def retrieve(self, request: ReviewRequest) -> ReviewResponse:
        if (
            len(self.review.files) > self.settings.max_changed_files
            or self.review.total_diff_bytes > self.settings.max_diff_bytes
            or len(self.review.model_dump_json().encode()) > self.settings.max_upstream_bytes
        ):
            raise GitHubError(413, "fixture_budget", "Fixture exceeds retrieval budgets.")
        return self.review

    async def get_file_at_head(self, review: ReviewResponse, path: str) -> str:
        if review.head_sha != self.review.head_sha or review.repository != self.review.repository:
            raise GitHubError(422, "fixture_identity", "Fixture identity mismatch.")
        source = next((f.head for f in self.case.files if f.path == path), None)
        if source is None:
            raise GitHubError(404, "file_not_found", "Fixture file not found.")
        if len(source.encode()) > min(
            self.settings.ai_max_file_bytes, self.settings.max_upstream_bytes
        ):
            raise GitHubError(413, "file_too_large", "Fixture exceeds file budget.")
        return source


def plan(dataset: Dataset, settings: Settings, ids: list[str]) -> dict:
    if not ids or len(ids) > MAX_LIVE_CASES or len(set(ids)) != len(ids):
        raise ValueError("Select one to three distinct cases")
    cases = {c.id: c for c in dataset.cases}
    if any(i not in cases or cases[i].excluded_reason for i in ids):
        raise ValueError("Unknown or excluded live case")
    if dataset_digest(dataset) != TRUSTED_DATASET_SHA256:
        raise ValueError("Live mode accepts only the pinned synthetic dataset")
    if contains_secret_data(dataset.model_dump(), settings):
        raise ValueError("Potential credentials in fixture")
    limits = {
        k: v
        for k, v in settings.model_dump(mode="json").items()
        if k.startswith("ai_") or k.startswith("max_")
    }
    return dict(
        mode="live-preview",
        dataset_version=dataset.version,
        dataset_sha256=dataset_digest(dataset),
        model=settings.gemini_model,
        thinking_level=settings.ai_thinking_level,
        cases=ids,
        case_details=[
            dict(
                id=i,
                title=cases[i].title,
                context_required=cases[i].context_required,
                changed_files=[f.path for f in cases[i].files if f.patch],
            )
            for i in ids
        ],
        runtime=dict(
            python=platform.python_version(),
            google_genai=version("google-genai"),
            pydantic=version("pydantic"),
            source_sha256=hashlib.sha256(
                b"".join(
                    p.name.encode() + p.read_bytes()
                    for p in sorted(Path(__file__).parents[1].rglob("*.py"))
                )
            ).hexdigest(),
        ),
        limits=limits,
        maximum_cases=MAX_LIVE_CASES,
        maximum_llm_requests=len(ids) * settings.ai_max_llm_requests,
        maximum_tool_calls=len(ids) * settings.ai_max_tool_calls,
        automatic_retries=False,
        github_network_requests=0,
        token_usage="unavailable: production session does not expose SDK token metadata",
    )


class RecordingSession:
    """Delegate every call unchanged; capture only the final structured text."""

    def __init__(self, session: ReviewSession):
        self.session = session
        self.final: str | None = None

    async def request(self, **kwargs) -> ModelTurn:
        turn = await self.session.request(**kwargs)
        if kwargs.get("final"):
            self.final = turn.text
        return turn

    async def aclose(self) -> None:
        await self.session.aclose()


def write_json(path: Path, data: object) -> None:
    # Files live in a newly reserved run directory; checkpoint via atomic replacement.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


async def execute(
    dataset: Dataset,
    settings: Settings,
    ids: list[str],
    output: Path,
    *,
    approved: bool = False,
    session_factory: SessionFactory = GeminiSession,
) -> dict:
    if not approved:
        raise ValueError("Explicit execution approval required")
    preview = plan(dataset, settings, ids)
    require_ai_configuration(settings)
    # Refuse overwrite before constructing a client or making any request.
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", preview)
    predictions = []
    records = []
    upstream_streak = 0
    stop_reason = None
    for id in ids:
        case = next(c for c in dataset.cases if c.id == id)
        captured = []

        def factory(config):
            session = RecordingSession(session_factory(config))
            captured.append(session)
            return session

        start = time.perf_counter()
        status, response = await ReviewAgent(FixtureGitHub(case, settings), settings, factory).run(
            ReviewRequest(pr_url=f"https://github.com/pr-sentinel-benchmark/{id}/pull/1")
        )
        elapsed = time.perf_counter() - start
        raw = response.model_dump(mode="json")
        candidates = []
        # The production agent must first accept the envelope and secret screen.
        if response.report is not None and captured and captured[0].final is not None:
            candidates = _strict_json(captured[0].final)["findings"]
        if contains_secret_data(raw, settings) or contains_secret_data(candidates, settings):
            raise ValueError("Potential credentials in output; saving refused")
        predictions.append(PredictionCase(case_id=id, findings=candidates, status=response.status))
        records.append(
            dict(
                case_id=id,
                http_status=status,
                elapsed_seconds=elapsed,
                input_tokens=None,
                output_tokens=None,
                token_usage_available=False,
                raw_review=raw,
                context_evidence=context_evidence(case, raw),
            )
        )
        codes = {issue.code for issue in response.issues}
        upstream_streak = (
            upstream_streak + 1
            if codes & {"gemini_upstream_error", "gemini_timeout", "gemini_sdk_error"}
            else 0
        )
        if "gemini_quota_exhausted" in codes:
            stop_reason = "quota_exhausted"
        elif upstream_streak >= 2:
            stop_reason = "repeated_upstream_errors"
        elif response.status != "completed" and not upstream_streak:
            stop_reason = "review_error_or_budget_limit"
        result = dict(
            plan=preview,
            records=records,
            measurements=dict(
                elapsed_seconds=sum(r["elapsed_seconds"] for r in records),
                llm_requests=sum(r["raw_review"]["usage"]["llm_requests"] for r in records),
                tool_calls=sum(r["raw_review"]["usage"]["tool_calls"] for r in records),
                initial_context_chars=sum(
                    r["raw_review"]["usage"]["initial_context_chars"] for r in records
                ),
                tool_context_chars=sum(
                    r["raw_review"]["usage"]["tool_context_chars"] for r in records
                ),
                input_tokens=None,
                output_tokens=None,
            ),
            stop_reason=stop_reason,
            attempted_cases=[p.case_id for p in predictions],
            unattempted_cases=ids[len(predictions) :],
        )
        saved = Predictions(
            format_version="1.0",
            dataset_version=dataset.version,
            dataset_sha256=dataset_digest(dataset),
            provenance="recorded-live",
            model=settings.gemini_model,
            cases=predictions,
        )
        write_json(output / "predictions.json", saved.model_dump(mode="json"))
        write_json(output / "run.json", result)
        print(
            json.dumps(dict(case_id=id, status=response.status, elapsed_seconds=elapsed)),
            flush=True,
        )
        if stop_reason:
            break
    return result


def context_evidence(case: Case, response: dict) -> dict:
    """Legacy traces are unknown; successful retrieval is not proof of model reasoning."""
    traces = response.get("tool_traces")
    required = case.context_paths
    if traces is None:
        return dict(
            trace_available=False,
            required_paths=required,
            retrieved_required_paths=None,
            missing_required_paths=None,
            all_required_files_retrieved=None,
        )
    retrieved = sorted(
        {
            t["requested_path"]
            for t in traces
            if t.get("tool_name") == "get_file_context"
            and t.get("success") is True
            and t.get("requested_path") in required
            and t.get("context_chars", 0) > 0
        }
    )
    missing = sorted(set(required) - set(retrieved))
    return dict(
        trace_available=True,
        required_paths=required,
        retrieved_required_paths=retrieved,
        missing_required_paths=missing,
        all_required_files_retrieved=not missing if required else None,
    )

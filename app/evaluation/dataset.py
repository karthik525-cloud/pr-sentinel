"""Strict fixture contracts and snapshot/diff/label consistency checks."""

import ast
import difflib
import hashlib
import json
from pathlib import Path

from pydantic import Field

from app.config import Settings
from app.context_security import contains_secret_data, safe_repository_path, sensitive_path
from app.diff_parser import parse_review
from app.findings import Category, Severity, StrictModel
from app.models import ChangedFile, ReviewResponse
from app.review_agent import _strict_json

DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "benchmarks" / "v1.json"


class Defect(StrictModel):
    id: str = Field(min_length=1)
    file_path: str
    line: int = Field(ge=1)
    start: int = Field(ge=1)
    end: int = Field(ge=1)
    category: Category
    severity: Severity
    explanation: str = Field(min_length=20)
    # Every group must match at least one phrase in title + explanation.
    # Literal case-insensitive substrings, not executable regex or a model judge.
    concepts: list[list[str]] = Field(min_length=2)


class SourceFile(StrictModel):
    path: str
    base: str
    head: str
    patch: str | None


class Case(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    provenance: str = Field(min_length=1)
    context_required: bool
    context_paths: list[str]
    excluded_reason: str | None = None
    files: list[SourceFile] = Field(min_length=1)
    expected: list[Defect] = Field(max_length=10)


class Dataset(StrictModel):
    version: str
    provenance: str
    cases: list[Case] = Field(min_length=1)


def patch_for(base: str, head: str, path: str) -> str | None:
    if base == head:
        return None
    return "".join(
        list(
            difflib.unified_diff(
                base.splitlines(keepends=True),
                head.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=2,
            )
        )[2:]
    ).rstrip("\n")


def review_for(case: Case) -> ReviewResponse:
    files = [
        ChangedFile(
            path=f.path,
            status="modified" if f.base else "added",
            additions=sum(line.startswith("+") for line in f.patch.splitlines()),
            deletions=sum(line.startswith("-") for line in f.patch.splitlines()),
            patch=f.patch,
            patch_available=True,
        )
        for f in case.files
        if f.patch is not None
    ]

    def digest(side):
        return hashlib.sha1(
            json.dumps(
                {f.path: getattr(f, side) for f in case.files},
                sort_keys=True,
            ).encode()
        ).hexdigest()

    return ReviewResponse(
        owner="pr-sentinel-benchmark",
        repository=case.id,
        number=1,
        title=case.title,
        description=case.description,
        base_sha=digest("base"),
        head_sha=digest("head"),
        changed_files=len(files),
        total_diff_bytes=sum(len(f.patch.encode()) for f in files),
        files=files,
    )


def validate_dataset(dataset: Dataset) -> None:
    if dataset.version != "1.0":
        raise ValueError("Unsupported dataset version")
    if len({c.id for c in dataset.cases}) != len(dataset.cases):
        raise ValueError("Duplicate case IDs")
    for case in dataset.cases:
        paths = [f.path for f in case.files]
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate source paths")
        for f in case.files:
            if not safe_repository_path(f.path) or sensitive_path(f.path):
                raise ValueError("Unsafe fixture path")
            for source in (f.base, f.head):
                if source and not source.endswith("\n"):
                    raise ValueError("Snapshots must end with LF")
                ast.parse(source)  # Parse only; never import or execute fixture code.
            if f.patch != patch_for(f.base, f.head, f.path):
                raise ValueError("Patch disagrees with snapshots")
        parsed = parse_review(review_for(case))
        if not parsed.files or any(f.patch_state != "parsed" for f in parsed.files):
            raise ValueError("Invalid changed-file mapping")
        added = {
            f.path: {line.new_line for h in f.hunks for line in h.lines if line.kind == "added"}
            for f in parsed.files
        }
        if len({d.id for d in case.expected}) != len(case.expected):
            raise ValueError("Duplicate defect IDs")
        for d in case.expected:
            if not d.start <= d.line <= d.end or d.end - d.start > 20:
                raise ValueError("Invalid label location range")
            if not set(range(d.start, d.end + 1)) <= added.get(d.file_path, set()):
                raise ValueError("Labels must reference added lines")
            if any(
                not group or any(not phrase.strip() for phrase in group) for group in d.concepts
            ):
                raise ValueError("Empty semantic rubric")
        if case.context_required != bool(case.context_paths):
            raise ValueError("Context declaration is inconsistent")
        if any(path not in paths or path in added for path in case.context_paths):
            raise ValueError("Context paths must identify unchanged snapshot files")
        if case.excluded_reason is not None and not case.excluded_reason.strip():
            raise ValueError("Missing exclusion rationale")
    if contains_secret_data(dataset.model_dump(), Settings()):
        raise ValueError("Potential credentials in fixture")


def load_dataset(path: Path = DEFAULT_DATASET) -> Dataset:
    if path.stat().st_size > 2_000_000:
        raise ValueError("Dataset too large")
    dataset = Dataset.model_validate(_strict_json(path.read_text()))
    validate_dataset(dataset)
    return dataset


def dataset_digest(dataset: Dataset) -> str:
    return hashlib.sha256(json.dumps(dataset.model_dump(), sort_keys=True).encode()).hexdigest()

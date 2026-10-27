"""Validate candidate schemas and precise added-line locations; no factual judgments."""

from collections.abc import Iterable

from pydantic import ValidationError

from app.diff_parser import ParsedReview
from app.findings import RejectedFinding, RejectionCode, ReviewFinding, ReviewReport, SchemaIssue


def finding_key(finding: ReviewFinding) -> tuple[str, int, str, str]:
    """Issue identity: exact path + line + category + normalized title.

    Case-fold and collapse whitespace in the title only. Severity, prose, and fix
    wording do not create a new issue. This is not semantic duplicate detection.
    """
    return (
        finding.file_path,
        finding.line_number,
        finding.category,
        " ".join(finding.title.split()).casefold(),
    )


def validate_findings(review: ParsedReview, candidates: Iterable[object]) -> ReviewReport:
    """Validate each raw candidate independently, preserving rejection input indices.

    Raw dictionaries are the boundary for future tool/JSON responses. Instances
    are revalidated too, preventing model_construct/model_copy from bypassing the
    schema. Only accepted findings participate in duplicate detection.
    """
    files = {file.path: file for file in review.files}
    if len(files) != len(review.files):
        raise ValueError("Duplicate PR file paths cannot be mapped unambiguously.")
    # Index actual source records, never entire hunk ranges.
    new_lines = {
        file.path: {
            line.new_line: line.kind
            for hunk in file.hunks
            for line in hunk.lines
            if line.new_line is not None
        }
        for file in review.files
        if file.patch_state == "parsed"
    }
    accepted: list[ReviewFinding] = []
    rejected: list[RejectedFinding] = []
    seen: dict[tuple[str, int, str, str], int] = {}
    for index, candidate in enumerate(candidates):
        if isinstance(candidate, ReviewFinding):
            # Read fields directly: dumping an invalid constructed model can emit
            # serialization warnings and should not be the schema-validation path.
            candidate = {
                name: getattr(candidate, name, None) for name in ReviewFinding.model_fields
            } | (candidate.model_extra or {})
        try:
            finding = ReviewFinding.model_validate(candidate)
        except ValidationError as exc:
            issues = tuple(
                SchemaIssue(
                    location=tuple(error["loc"]),
                    code=error["type"],
                    message=error["msg"],
                )
                for error in exc.errors(
                    include_input=False, include_context=False, include_url=False
                )
            )
            rejected.append(
                RejectedFinding(
                    candidate_index=index,
                    code="schema_invalid",
                    message="Candidate does not satisfy the review finding schema.",
                    schema_issues=issues,
                )
            )
            continue

        code: RejectionCode
        file = files.get(finding.file_path)
        if file is None:
            code, message = "file_not_in_pr", "File path is not a current path in this PR."
        elif file.status == "removed":
            code, message = "deleted_file", "Deleted files have no new-side review location."
        elif file.patch_state == "unavailable":
            code, message = "patch_unavailable", "No patch is available to validate the location."
        elif file.patch_state == "malformed":
            code, message = "patch_malformed", "Patch cannot provide a reliable line mapping."
        elif finding.line_number not in new_lines[file.path]:
            code, message = (
                "line_not_in_diff",
                "Line is not present on the new side of the provided diff.",
            )
        elif new_lines[file.path][finding.line_number] != "added":
            code, message = (
                "line_not_added",
                "Line is context rather than an added or replacement line.",
            )
        else:
            key = finding_key(finding)
            if key in seen:
                rejected.append(
                    RejectedFinding(
                        candidate_index=index,
                        code="duplicate_finding",
                        message="An accepted finding already has this issue identity.",
                        duplicate_of=seen[key],
                    )
                )
            else:
                seen[key] = index
                accepted.append(finding)
            continue
        rejected.append(RejectedFinding(candidate_index=index, code=code, message=message))

    return ReviewReport(
        owner=review.owner,
        repository=review.repository,
        number=review.number,
        base_sha=review.base_sha,
        head_sha=review.head_sha,
        findings=tuple(accepted),
        rejected=tuple(rejected),
    )

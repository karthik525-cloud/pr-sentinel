"""Parse GitHub per-file unified patches, without fetching or executing source code."""

import re
from dataclasses import dataclass, replace
from typing import Literal

from app.models import ChangedFile, ReviewResponse

_HEADER = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?", re.ASCII)
_NO_NEWLINE = "\\ No newline at end of file"


@dataclass(frozen=True)
class DiffLine:
    kind: Literal["added", "removed", "context"]
    content: str
    old_line: int | None
    new_line: int | None
    no_newline: bool = False


@dataclass(frozen=True)
class DiffHunk:
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DiffLine, ...]


class PatchParseError(ValueError):
    """Machine-readable parse error; patch_line is a 1-based patch record number."""

    def __init__(self, code: str, patch_line: int, message: str):
        self.code = code
        self.patch_line = patch_line
        self.message = message
        super().__init__(message)


def parse_patch(patch: str) -> tuple[DiffHunk, ...]:
    """Return all hunks or raise; never return a partial mapping on malformed input.

    Input is GitHub's per-file `patch`, not a complete `git diff` with file headers.
    Split on LF only: other Unicode line separators and CR belong to source text.
    """
    records = patch.split("\n")
    if records[-1] == "":
        records.pop()  # A final framing LF is optional, not an empty diff record.
    if not records or not any(records):
        raise PatchParseError("empty_patch", 1, "Patch contains no hunks.")

    hunks: list[DiffHunk] = []
    cursor = 0
    previous_old_end: int | None = None
    previous_new_end: int | None = None
    old_eof = new_eof = False
    while cursor < len(records):
        header_line = cursor + 1
        header = records[cursor]
        match = _HEADER.fullmatch(header)
        if match is None:
            raise PatchParseError(
                "invalid_hunk_header", header_line, "Expected a unified diff hunk header."
            )
        try:
            old_start, old_count, new_start, new_count = (
                int(match[1]),
                int(match[2]) if match[2] is not None else 1,
                int(match[3]),
                int(match[4]) if match[4] is not None else 1,
            )
        except ValueError:
            raise PatchParseError(
                "invalid_range", header_line, "Hunk range cannot be represented."
            ) from None
        if (old_count and old_start == 0) or (new_count and new_start == 0):
            raise PatchParseError(
                "invalid_range", header_line, "Nonempty ranges must start at line 1 or later."
            )

        # Zero-count ranges are boundaries *after* start, not source line numbers.
        old_boundary = old_start - 1 if old_count else old_start
        new_boundary = new_start - 1 if new_count else new_start
        if previous_old_end is not None and previous_new_end is not None:
            old_gap = old_boundary - previous_old_end
            new_gap = new_boundary - previous_new_end
            if old_gap < 0 or new_gap < 0:
                raise PatchParseError(
                    "overlapping_hunks", header_line, "Hunk ranges overlap or are out of order."
                )
            if (old_eof and old_gap > 0) or (new_eof and new_gap > 0):
                raise PatchParseError(
                    "line_after_eof",
                    header_line,
                    "Hunk gap implies source lines after a no-newline EOF marker.",
                )
            if old_gap != new_gap:
                raise PatchParseError(
                    "inconsistent_hunk_gap",
                    header_line,
                    "Unchanged gaps must span the same number of old and new lines.",
                )

        cursor += 1
        old_used = new_used = 0
        lines: list[DiffLine] = []
        while cursor < len(records) and not records[cursor].startswith("@@"):
            record = records[cursor]
            record_number = cursor + 1
            if record == _NO_NEWLINE:
                if not lines or lines[-1].no_newline:
                    raise PatchParseError(
                        "invalid_no_newline_marker",
                        record_number,
                        "Marker must immediately follow an unmarked source line.",
                    )
                last = lines[-1]
                lines[-1] = replace(last, no_newline=True)
                old_eof = old_eof or last.old_line is not None
                new_eof = new_eof or last.new_line is not None
                cursor += 1
                continue
            if not record or record[0] not in " +-":
                raise PatchParseError(
                    "invalid_diff_line",
                    record_number,
                    "Diff source lines require a space, plus, or minus prefix.",
                )
            prefix, content = record[0], record[1:]
            consumes_old, consumes_new = prefix != "+", prefix != "-"
            if (consumes_old and old_eof) or (consumes_new and new_eof):
                raise PatchParseError(
                    "line_after_eof",
                    record_number,
                    "Source line follows a no-newline EOF marker on the same side.",
                )
            if old_used + consumes_old > old_count or new_used + consumes_new > new_count:
                raise PatchParseError(
                    "hunk_count_exceeded",
                    record_number,
                    "Hunk body exceeds its declared line counts.",
                )
            lines.append(
                DiffLine(
                    kind="added" if prefix == "+" else "removed" if prefix == "-" else "context",
                    content=content,
                    old_line=old_start + old_used if consumes_old else None,
                    new_line=new_start + new_used if consumes_new else None,
                )
            )
            old_used += consumes_old
            new_used += consumes_new
            cursor += 1
        if old_used != old_count or new_used != new_count:
            raise PatchParseError(
                "truncated_hunk", cursor + 1, "Hunk body does not satisfy its declared line counts."
            )
        hunks.append(DiffHunk(header, old_start, old_count, new_start, new_count, tuple(lines)))
        previous_old_end = old_boundary + old_count
        previous_new_end = new_boundary + new_count
    return tuple(hunks)


@dataclass(frozen=True)
class PatchIssue:
    code: str
    message: str
    patch_line: int | None = None


@dataclass(frozen=True)
class ParsedFile:
    path: str
    previous_path: str | None
    status: str
    patch_state: Literal["parsed", "unavailable", "malformed"]
    hunks: tuple[DiffHunk, ...] = ()
    issue: PatchIssue | None = None


def parse_file(file: ChangedFile) -> ParsedFile:
    """Preserve file identity even when no reliable line mapping is available."""
    identity = ParsedFile(file.path, file.previous_path, file.status, "unavailable")
    if file.patch is None:
        return replace(
            identity,
            patch_state="unavailable",
            issue=PatchIssue(
                "patch_unavailable",
                "GitHub did not provide a patch.",
            ),
        )
    if not file.patch_available:
        return replace(
            identity,
            patch_state="malformed",
            issue=PatchIssue(
                "inconsistent_patch_metadata",
                "Patch text conflicts with its availability flag.",
            ),
        )
    try:
        hunks = parse_patch(file.patch)
        additions = sum(line.kind == "added" for hunk in hunks for line in hunk.lines)
        deletions = sum(line.kind == "removed" for hunk in hunks for line in hunk.lines)
        if additions != file.additions or deletions != file.deletions:
            return replace(
                identity,
                patch_state="malformed",
                issue=PatchIssue(
                    "change_count_mismatch",
                    "Patch additions/deletions differ from GitHub file totals.",
                ),
            )
        if file.status == "removed" and any(
            line.new_line is not None for hunk in hunks for line in hunk.lines
        ):
            return replace(
                identity,
                patch_state="malformed",
                issue=PatchIssue(
                    "deleted_file_has_new_lines",
                    "A deleted file cannot contain new-side lines.",
                ),
            )
        if file.status == "added" and any(
            line.old_line is not None for hunk in hunks for line in hunk.lines
        ):
            return replace(
                identity,
                patch_state="malformed",
                issue=PatchIssue(
                    "added_file_has_old_lines",
                    "An added file cannot contain old-side lines.",
                ),
            )
        return replace(identity, patch_state="parsed", hunks=hunks)
    except PatchParseError as exc:
        return replace(
            identity,
            patch_state="malformed",
            issue=PatchIssue(
                exc.code,
                exc.message,
                exc.patch_line,
            ),
        )


@dataclass(frozen=True)
class ParsedReview:
    owner: str
    repository: str
    number: int
    base_sha: str
    head_sha: str
    files: tuple[ParsedFile, ...]


def parse_review(review: ReviewResponse) -> ParsedReview:
    """Adapt the Stage 1 result without changing it or performing network I/O."""
    paths = [file.path for file in review.files]
    if len(set(paths)) != len(paths):
        raise ValueError("Duplicate PR file paths cannot be mapped unambiguously.")
    return ParsedReview(
        owner=review.owner,
        repository=review.repository,
        number=review.number,
        base_sha=review.base_sha,
        head_sha=review.head_sha,
        files=tuple(parse_file(file) for file in review.files),
    )

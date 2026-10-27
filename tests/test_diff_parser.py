import difflib
from dataclasses import FrozenInstanceError
from itertools import product

import pytest

from app.diff_parser import PatchParseError, parse_file, parse_patch, parse_review


def locations(hunks):
    return [
        (line.kind, line.old_line, line.new_line, line.content)
        for hunk in hunks
        for line in hunk.lines
    ]


def test_standard_mapping_and_hunk_metadata():
    hunks = parse_patch("@@ -10,3 +12,4 @@ function name\n keep\n-old\n+new\n+extra\n tail\n")
    assert len(hunks) == 1
    hunk = hunks[0]
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (10, 3, 12, 4)
    assert hunk.header.endswith("function name")
    assert locations(hunks) == [
        ("context", 10, 12, "keep"),
        ("removed", 11, None, "old"),
        ("added", None, 13, "new"),
        ("added", None, 14, "extra"),
        ("context", 12, 15, "tail"),
    ]


def test_multiple_hunks_and_unchanged_gap():
    hunks = parse_patch("@@ -1 +1,2 @@\n-old\n+first\n+second\n@@ -5,2 +6 @@\n-gone\n last")
    assert len(hunks) == 2
    assert locations(hunks)[-2:] == [("removed", 5, None, "gone"), ("context", 6, 6, "last")]


def test_adjacent_additions_deletions_and_omitted_counts():
    assert locations(parse_patch("@@ -7,2 +7,2 @@\n-a\n-b\n+c\n+d")) == [
        ("removed", 7, None, "a"),
        ("removed", 8, None, "b"),
        ("added", None, 7, "c"),
        ("added", None, 8, "d"),
    ]
    hunk = parse_patch("@@ -1 +1 @@\n-a\n+b")[0]
    assert hunk.old_count == hunk.new_count == 1


@pytest.mark.parametrize(
    "patch,expected",
    [
        ("@@ -0,0 +1,2 @@\n+a\n+b", [("added", None, 1, "a"), ("added", None, 2, "b")]),
        ("@@ -5,0 +6 @@\n+x", [("added", None, 6, "x")]),
        ("@@ -1,2 +0,0 @@\n-a\n-b", [("removed", 1, None, "a"), ("removed", 2, None, "b")]),
        ("@@ -9,2 +8,0 @@\n-a\n-b", [("removed", 9, None, "a"), ("removed", 10, None, "b")]),
        ("@@ -0,0 +0,0 @@", []),
    ],
)
def test_zero_ranges_pure_additions_and_deletions(patch, expected):
    assert locations(parse_patch(patch)) == expected


def test_consecutive_zero_count_anchors():
    hunks = parse_patch("@@ -0,0 +1 @@\n+a\n@@ -0,0 +2 @@\n+b")
    assert [line.new_line for hunk in hunks for line in hunk.lines] == [1, 2]


def test_blank_source_lines_and_preserved_whitespace():
    assert locations(parse_patch("@@ -1,2 +1,2 @@\n \n-\n+\t  ")) == [
        ("context", 1, 1, ""),
        ("removed", 2, None, ""),
        ("added", None, 2, "\t  "),
    ]


def test_source_unicode_separators_and_cr_are_not_split():
    hunk = parse_patch("@@ -0,0 +1 @@\n+a\u2028b\v\r")[0]
    assert hunk.lines[0].content == "a\u2028b\v\r"


def test_no_newline_markers_track_both_sides():
    hunk = parse_patch(
        "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    )[0]
    assert [line.no_newline for line in hunk.lines] == [True, True]
    assert locations((hunk,)) == [("removed", 1, None, "old"), ("added", None, 1, "new")]


def test_context_no_newline_marker():
    hunk = parse_patch("@@ -1,2 +1,2 @@\n-old\n+new\n last\n\\ No newline at end of file")[0]
    assert hunk.lines[-1].kind == "context" and hunk.lines[-1].no_newline


@pytest.mark.parametrize(
    "patch,code",
    [
        ("", "empty_patch"),
        ("\n", "empty_patch"),
        ("not a diff", "invalid_hunk_header"),
        ("@@ -1 +1 @\n-a\n+b", "invalid_hunk_header"),
        ("@@@ -1 +1 @@@", "invalid_hunk_header"),
        ("@@ -1,-2 +1 @@", "invalid_hunk_header"),
        ("@@ -0 +1 @@\n-a\n+b", "invalid_range"),
        ("@@ -1 +0 @@\n-a\n+b", "invalid_range"),
        ("@@ -1,2 +1,2 @@\n-old\n+new", "truncated_hunk"),
        ("@@ -1 +1 @@\n-a\n+b\n+c", "hunk_count_exceeded"),
        ("@@ -1 +1 @@\n-a\n+b\n", None),
        ("@@ -1 +1 @@\n-a\n+b\n\n", "invalid_diff_line"),
        ("@@ -1 +1 @@\nold", "invalid_diff_line"),
        ("@@ -1 +1 @@\n\\ No newline at end of file", "invalid_no_newline_marker"),
        (
            "@@ -0,0 +1 @@\n+a\n\\ No newline at end of file\n\\ No newline at end of file",
            "invalid_no_newline_marker",
        ),
        ("@@ -0,0 +1,2 @@\n+a\n\\ No newline at end of file\n+b", "line_after_eof"),
        ("@@ -1,2 +0,0 @@\n-a\n\\ No newline at end of file\n-b", "line_after_eof"),
        ("@@ -1 +1 @@\n a\n\\ No newline at end of file\n@@ -2 +2 @@\n b", "line_after_eof"),
        ("@@ -1 +1 @@\n-a\n+b\n@@ -1 +2 @@\n-c\n+d", "overlapping_hunks"),
        ("@@ -5 +5 @@\n-a\n+b\n@@ -2 +2 @@\n-c\n+d", "overlapping_hunks"),
        ("@@ -1 +1 @@\n-a\n+b\n@@ -4 +5 @@\n-c\n+d", "inconsistent_hunk_gap"),
        ("@@ -1 +1 @@\n-a\n@@ -3 +3 @@\n-b\n+c", "truncated_hunk"),
        ("diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b", "invalid_hunk_header"),
    ],
)
def test_fail_safe_malformed_input(patch, code):
    if code is None:
        assert parse_patch(patch)
        return
    with pytest.raises(PatchParseError) as caught:
        parse_patch(patch)
    assert caught.value.code == code
    assert caught.value.patch_line >= 1
    assert caught.value.message


def test_malformed_later_hunk_discards_all_locations(make_file):
    file = make_file(patch="@@ -1 +1 @@\n-a\n+b\n@@ -3,2 +3,2 @@\n c")
    result = parse_file(file)
    assert result.patch_state == "malformed"
    assert result.hunks == ()
    assert result.issue.code == "truncated_hunk"


def test_whole_hunk_omission_detected_against_file_totals(make_file):
    result = parse_file(make_file(patch="@@ -1 +1 @@\n-a\n+b"))
    assert result.patch_state == "malformed"
    assert result.issue.code == "change_count_mismatch"


@pytest.mark.parametrize(
    "status,patch,additions,deletions,state",
    [
        ("renamed", None, 0, 0, "unavailable"),
        ("modified", None, 2, 1, "unavailable"),
        ("modified", "", 0, 0, "malformed"),
        ("removed", "@@ -1 +0,0 @@\n-x", 0, 1, "parsed"),
        ("removed", "@@ -1 +1 @@\n-x\n+y", 1, 1, "malformed"),
        ("added", "@@ -0,0 +1 @@\n+x", 1, 0, "parsed"),
        ("modified", "@@ -0,0 +0,0 @@", 0, 0, "parsed"),
    ],
)
def test_file_states(make_file, status, patch, additions, deletions, state):
    file = make_file(status=status, patch=patch, additions=additions, deletions=deletions)
    result = parse_file(file)
    assert result.patch_state == state
    assert result.path == file.path
    if state != "parsed":
        assert result.hunks == () and result.issue is not None


def test_inconsistent_availability_is_not_usable(make_file):
    result = parse_file(make_file(patch_available=False))
    assert result.patch_state == "malformed"
    assert result.issue.code == "inconsistent_patch_metadata"


def test_renamed_path_and_review_identity(make_file, make_review):
    review = make_review(make_file(path="new.py", previous_path="old.py", status="renamed"))
    before = review.model_dump()
    parsed = parse_review(review)
    assert (parsed.base_sha, parsed.head_sha) == (review.base_sha, review.head_sha)
    assert parsed.files[0].path == "new.py"
    assert parsed.files[0].previous_path == "old.py"
    assert review.model_dump() == before
    with pytest.raises(FrozenInstanceError):
        parsed.files[0].path = "changed.py"


def test_duplicate_review_paths_rejected(make_file, make_review):
    with pytest.raises(ValueError, match="Duplicate"):
        parse_review(make_review(make_file(), make_file()))


def test_generated_standard_library_diffs_map_back_to_source():
    # Independent producer/oracle: verify every emitted coordinate against actual
    # old/new text across deterministic edits, context widths and file boundaries.
    for size, start, remove, insert, context in product(
        range(1, 6), range(6), range(3), range(3), (0, 1, 3)
    ):
        if start > size or start + remove > size or (remove == insert == 0):
            continue
        old = [f"original {i}\n" for i in range(size)]
        new = old[:start] + [f"inserted {i}\n" for i in range(insert)] + old[start + remove :]
        raw = list(difflib.unified_diff(old, new, n=context))
        patch = "".join(raw[2:])
        hunks = parse_patch(patch)
        added = removed = 0
        for hunk in hunks:
            for line in hunk.lines:
                if line.old_line is not None:
                    assert line.content == old[line.old_line - 1].rstrip("\n")
                if line.new_line is not None:
                    assert line.content == new[line.new_line - 1].rstrip("\n")
                added += line.kind == "added"
                removed += line.kind == "removed"
        assert added == insert and removed == remove


def test_added_file_with_old_side_data_is_unusable(make_file):
    result = parse_file(make_file(status="added"))
    assert result.patch_state == "malformed"
    assert result.issue.code == "added_file_has_old_lines"


def test_unrepresentable_header_integer_is_a_parse_error():
    with pytest.raises(PatchParseError) as caught:
        parse_patch("@@ -" + "1" * 5000 + " +1 @@")
    assert caught.value.code == "invalid_range"


def test_generated_multiple_hunks_preserve_coordinates():
    old = [f"line {i}\n" for i in range(30)]
    new = old[:2] + ["new early\n", "another\n"] + old[3:25] + ["new late\n"] + old[27:]
    patch = "".join(list(difflib.unified_diff(old, new, n=1))[2:])
    hunks = parse_patch(patch)
    assert len(hunks) == 2
    for hunk in hunks:
        for line in hunk.lines:
            if line.old_line is not None:
                assert line.content == old[line.old_line - 1].rstrip("\n")
            if line.new_line is not None:
                assert line.content == new[line.new_line - 1].rstrip("\n")


def test_hunk_gap_cannot_imply_lines_after_marked_eof():
    patch = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n@@ -5,0 +6 @@\n+extra"
    with pytest.raises(PatchParseError) as caught:
        parse_patch(patch)
    assert caught.value.code == "line_after_eof"


def test_zero_range_at_marked_old_eof_can_still_extend_new_side():
    patch = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n@@ -1,0 +2 @@\n+extra"
    hunks = parse_patch(patch)
    assert hunks[1].lines[0].new_line == 2

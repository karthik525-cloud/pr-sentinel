import json

import pytest
from pydantic import ValidationError

from app.diff_parser import parse_review
from app.finding_validator import finding_key, validate_findings
from app.findings import ReviewFinding, ReviewReport


@pytest.fixture
def candidate():
    return dict(
        file_path="src/app.py",
        line_number=11,
        severity="high",
        category="bug",
        title="Missing bounds check",
        explanation="An empty input raises IndexError.",
        suggested_fix="Check whether the input is empty before indexing.",
    )


def test_valid_finding_and_report_serialization(candidate, make_file, make_review):
    report = validate_findings(parse_review(make_review(make_file())), [candidate])
    assert len(report.findings) == 1 and not report.rejected
    assert report.findings[0].line_number == 11
    assert report.validation_scope == "schema_and_diff_location"
    assert report.base_sha == "a" * 40 and report.head_sha == "b" * 40
    assert ReviewReport.model_validate_json(report.model_dump_json()) == report
    assert "confidence" not in ReviewFinding.model_json_schema()["properties"]


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"file_path": "other.py"}, "file_not_in_pr"),
        ({"file_path": "SRC/app.py"}, "file_not_in_pr"),
        ({"file_path": "./src/app.py"}, "file_not_in_pr"),
        ({"line_number": 1}, "line_not_in_diff"),
        ({"line_number": 14}, "line_not_in_diff"),
        ({"line_number": 10}, "line_not_added"),
        ({"line_number": 13}, "line_not_added"),
    ],
)
def test_invalid_locations(candidate, make_file, make_review, overrides, code):
    report = validate_findings(parse_review(make_review(make_file())), [candidate | overrides])
    assert not report.findings
    assert report.rejected[0].code == code
    assert report.rejected[0].candidate_index == 0


def test_line_in_gap_is_not_a_location(candidate, make_file, make_review):
    file = make_file(patch="@@ -1 +1 @@\n-a\n+b\n@@ -10 +10 @@\n-c\n+d", additions=2, deletions=2)
    report = validate_findings(parse_review(make_review(file)), [candidate | {"line_number": 5}])
    assert report.rejected[0].code == "line_not_in_diff"


def test_replacement_uses_new_number_not_old_number(candidate, make_file, make_review):
    file = make_file(patch="@@ -10 +20 @@\n-old\n+new", additions=1, deletions=1)
    report = validate_findings(
        parse_review(make_review(file)),
        [
            candidate | {"line_number": 20},
            candidate | {"line_number": 10},
        ],
    )
    assert report.findings[0].line_number == 20
    assert report.rejected[0].code == "line_not_in_diff"


def test_renamed_file_uses_current_path_only(candidate, make_file, make_review):
    file = make_file(path="new.py", previous_path="old.py", status="renamed")
    report = validate_findings(
        parse_review(make_review(file)),
        [
            candidate | {"file_path": "new.py"},
            candidate | {"file_path": "old.py"},
        ],
    )
    assert report.findings[0].file_path == "new.py"
    assert report.rejected[0].code == "file_not_in_pr"


@pytest.mark.parametrize(
    "status,patch,additions,deletions,code",
    [
        ("removed", "@@ -1 +0,0 @@\n-old", 0, 1, "deleted_file"),
        ("removed", None, 0, 1, "deleted_file"),
        ("renamed", None, 0, 0, "patch_unavailable"),
        ("modified", None, 2, 1, "patch_unavailable"),
        ("modified", "@@ -1,2 +1,2 @@\n-old\n+new", 1, 1, "patch_malformed"),
        ("modified", "@@ -10 +10 @@\n-old\n+new", 2, 1, "patch_malformed"),
        ("modified", "@@ -11 +10,0 @@\n-old", 0, 1, "line_not_in_diff"),
    ],
)
def test_unsupported_locations(
    candidate, make_file, make_review, status, patch, additions, deletions, code
):
    file = make_file(status=status, patch=patch, additions=additions, deletions=deletions)
    report = validate_findings(parse_review(make_review(file)), [candidate])
    assert not report.findings
    assert report.rejected[0].code == code


def test_missing_file_precedes_patch_state(candidate, make_file, make_review):
    report = validate_findings(
        parse_review(make_review(make_file(patch=None))), [candidate | {"file_path": "absent.py"}]
    )
    assert report.rejected[0].code == "file_not_in_pr"


def test_all_added_lines_are_valid(candidate, make_file, make_review):
    report = validate_findings(
        parse_review(make_review(make_file())),
        [
            candidate,
            candidate | {"line_number": 12},
        ],
    )
    assert [finding.line_number for finding in report.findings] == [11, 12]
    assert not report.rejected


def test_pure_addition_at_start(candidate, make_file, make_review):
    file = make_file(status="added", patch="@@ -0,0 +1 @@\n+new", additions=1, deletions=0)
    report = validate_findings(parse_review(make_review(file)), [candidate | {"line_number": 1}])
    assert len(report.findings) == 1


def test_duplicates_are_stable_and_reference_original_input_index(
    candidate, make_file, make_review
):
    candidates = [
        candidate | {"line_number": "11"},
        candidate,
        candidate
        | {
            "title": "  MISSING   bounds\tcheck  ",
            "severity": "low",
            "explanation": "Other prose.",
        },
        candidate | {"category": "reliability"},
        candidate | {"title": "Different issue"},
        candidate | {"line_number": 12},
    ]
    parsed = parse_review(make_review(make_file()))
    report = validate_findings(parsed, candidates)
    assert report == validate_findings(parsed, candidates)
    assert len(report.findings) == 4
    assert [item.code for item in report.rejected] == ["schema_invalid", "duplicate_finding"]
    assert report.rejected[1].candidate_index == 2
    assert report.rejected[1].duplicate_of == 1
    assert finding_key(ReviewFinding(**candidate)) == finding_key(ReviewFinding(**candidates[2]))


def test_duplicate_identity_keeps_exact_path_case(candidate, make_file, make_review):
    files = [make_file(path="src/app.py"), make_file(path="src/App.py")]
    report = validate_findings(
        parse_review(make_review(*files)), [candidate, candidate | {"file_path": "src/App.py"}]
    )
    assert len(report.findings) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("line_number", 0),
        ("line_number", -1),
        ("line_number", True),
        ("line_number", 11.0),
        ("line_number", "11"),
        ("line_number", 2_147_483_648),
        ("file_path", ""),
        ("file_path", "   "),
        ("file_path", "x\x00y"),
        ("file_path", "a\nb"),
        ("file_path", 123),
        ("file_path", "x" * 4097),
        ("severity", "urgent"),
        ("severity", "HIGH"),
        ("category", "style"),
        ("title", "  "),
        ("title", "t" * 121),
        ("explanation", ""),
        ("explanation", 123),
        ("explanation", "x" * 4001),
        ("suggested_fix", None),
        ("suggested_fix", "\t"),
        ("suggested_fix", "x\x00y"),
        ("confidence", 0.9),
    ],
)
def test_pydantic_constraints_and_machine_reasons(candidate, make_file, make_review, field, value):
    raw = candidate | {field: value}
    with pytest.raises(ValidationError):
        ReviewFinding.model_validate(raw)
    report = validate_findings(parse_review(make_review(make_file())), [raw])
    rejection = report.rejected[0]
    assert rejection.code == "schema_invalid"
    assert any(issue.location == (field,) for issue in rejection.schema_issues)
    serialized = json.loads(report.model_dump_json())
    assert "input" not in serialized["rejected"][0]["schema_issues"][0]


@pytest.mark.parametrize("raw", [None, [], "not a finding", 42, {}, {"file_path": "src/app.py"}])
def test_invalid_candidate_shapes(raw, make_file, make_review):
    report = validate_findings(parse_review(make_review(make_file())), [raw])
    assert report.rejected[0].code == "schema_invalid"
    assert report.rejected[0].schema_issues


def test_constructed_model_is_revalidated(candidate, make_file, make_review):
    invalid = ReviewFinding.model_construct(**(candidate | {"line_number": True}))
    report = validate_findings(parse_review(make_review(make_file())), [invalid])
    assert report.rejected[0].code == "schema_invalid"


def test_validated_model_and_empty_batch(candidate, make_file, make_review):
    parsed = parse_review(make_review(make_file()))
    assert validate_findings(parsed, []).findings == ()
    finding = ReviewFinding(**candidate)
    assert validate_findings(parsed, [finding]).findings == (finding,)


def test_no_files_no_finding(candidate, make_review):
    report = validate_findings(parse_review(make_review()), [candidate])
    assert report.rejected[0].code == "file_not_in_pr"


def test_title_cannot_span_lines(candidate):
    with pytest.raises(ValidationError, match="single line"):
        ReviewFinding(**(candidate | {"title": "first\nsecond"}))


def test_invalid_candidate_does_not_block_later_candidate(candidate, make_file, make_review):
    parsed = parse_review(make_review(make_file()))
    report = validate_findings(parsed, iter([None, candidate]))
    assert len(report.findings) == 1
    assert report.rejected[0].candidate_index == 0

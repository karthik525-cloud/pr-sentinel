import copy
import json

import pytest

from app.ai_models import ReviewFailure
from app.config import Settings
from app.evaluation.__main__ import main
from app.evaluation.dataset import (
    Dataset,
    dataset_digest,
    load_dataset,
    patch_for,
    review_for,
    validate_dataset,
)
from app.evaluation.live import FixtureGitHub, execute, plan
from app.evaluation.scoring import (
    PredictionCase,
    Predictions,
    evaluate,
    load_predictions,
    score_case,
)
from app.llm import ModelTurn, ToolCall


@pytest.fixture
def dataset():
    return load_dataset()


def finding(case, index=0, **overrides):
    d = case.expected[index]
    return (
        dict(
            file_path=d.file_path,
            line_number=d.line,
            category=d.category,
            severity=d.severity,
            title="Detected regression",
            explanation=d.explanation,
            suggested_fix="Restore the documented contract.",
        )
        | overrides
    )


def prediction(case, findings=None, **kwargs):
    return PredictionCase(
        case_id=case.id, findings=[finding(case)] if findings is None else findings, **kwargs
    )


def bundle(dataset, cases, provenance="synthetic"):
    return Predictions(
        format_version="1.0",
        dataset_version=dataset.version,
        dataset_sha256=dataset_digest(dataset),
        provenance=provenance,
        cases=cases,
    )


def settings(**overrides):
    return Settings(
        **(
            dict(
                gemini_api_key="test-only-key",
                gemini_model="gemini-3.5-flash-lite",
                gemini_free_tier_confirmed=True,
            )
            | overrides
        )
    )


def test_dataset_distribution_and_provenance(dataset):
    assert len(dataset.cases) == 25
    assert sum(bool(c.expected) for c in dataset.cases) == 17
    assert sum(not c.expected and not c.excluded_reason for c in dataset.cases) == 6
    assert sum(bool(c.excluded_reason) for c in dataset.cases) == 2
    assert sum(len(c.expected) for c in dataset.cases) == 19
    assert sum(c.context_required for c in dataset.cases) == 3
    from app.diff_parser import parse_review

    assert any(len(f.hunks) > 1 for c in dataset.cases for f in parse_review(review_for(c)).files)
    assert plan(dataset, settings(), [dataset.cases[0].id])["maximum_llm_requests"] == 4


@pytest.mark.parametrize("index", range(17))
def test_all_authored_rationales_match_their_rubrics(dataset, index):
    c = dataset.cases[index]
    result = score_case(c, prediction(c, [finding(c, i) for i in range(len(c.expected))]))
    assert result["tp"] == len(c.expected), result
    assert result["fp"] == result["fn"] == 0


def test_exact_missing_duplicate_and_false_positive(dataset):
    c = dataset.cases[0]
    assert score_case(c, prediction(c))["matches"][0]["location"] == "exact"
    missing = score_case(c, prediction(c, []))
    assert (missing["tp"], missing["fn"]) == (0, 1)
    duplicate = score_case(
        c, prediction(c, [finding(c), finding(c, title="Same bug differently worded")])
    )
    assert (duplicate["tp"], duplicate["fp"]) == (1, 1)
    assert duplicate["false_positives"][0]["reason"] == "duplicate"
    false = score_case(
        c, prediction(c, [finding(c, explanation="A completely unrelated performance concern.")])
    )
    assert (false["tp"], false["fp"], false["fn"]) == (0, 1, 1)


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"file_path": "wrong.py"}, "file_not_in_pr"),
        ({"line_number": 999}, "line_not_in_diff"),
        ({"line_number": 1}, "line_not_added"),
        ({"line_number": "2"}, "schema_invalid"),
        ({"category": "unknown"}, "schema_invalid"),
    ],
)
def test_invalid_predictions_are_false_positives(dataset, override, reason):
    c = dataset.cases[0]
    r = score_case(c, prediction(c, [finding(c, **override)]))
    assert (r["fp"], r["fn"], r["tp"]) == (1, 1, 0)
    assert r["false_positives"][0]["reason"] == reason


def test_wrong_classification_does_not_hide_detection(dataset):
    c = dataset.cases[0]
    r = score_case(c, prediction(c, [finding(c, category="security", severity="critical")]))
    assert r["tp"] == 1
    assert r["category_agreements"] == r["severity_agreements"] == 0
    assert r["category_counts"]["bug"] == [1, 0, 0]


def test_acceptable_range_and_wrong_added_line(dataset):
    c = next(c for c in dataset.cases if c.id == "mutating-sort")
    r = score_case(c, prediction(c, [finding(c, line_number=c.expected[0].end)]))
    assert r["tp"] == 1 and r["range_matches"] == 1
    c = next(c for c in dataset.cases if c.id == "two-regressions")
    r = score_case(c, prediction(c, [finding(c, line_number=c.expected[1].line)]))
    assert r["tp"] == 0 and r["false_positives"][0]["reason"] == "wrong_location"


def test_one_to_one_uses_maximum_assignment_not_greedy(dataset):
    c = dataset.cases[3]
    d = c.expected[0]
    # Broad prediction matches both labels; narrow one only the first.
    d2 = d.model_copy(update={"id": "other", "concepts": [["distinct"], ["second"]]})
    c = c.model_copy(update={"expected": [d, d2]})
    broad = finding(c, explanation=d.explanation + " distinct second")
    narrow = finding(c)
    r = score_case(c, prediction(c, [broad, narrow]))
    assert r["tp"] == 2
    assert [m["defect_id"] for m in r["matches"]] == ["other", d.id]


def test_clean_empty_excluded_and_zero_denominators(dataset):
    c = next(c for c in dataset.cases if c.id == "clean-timeout")
    report = evaluate(dataset, bundle(dataset, [prediction(c, [])]))
    assert report["aggregate"]["precision"] is None
    assert report["aggregate"]["recall"] is None
    assert report["aggregate"]["f1"] is None
    assert report["aggregate"]["location_accuracy"] is None
    assert report["aggregate"]["clean_case_false_positive_rate"] == 0
    bad = finding(dataset.cases[0])
    report = evaluate(dataset, bundle(dataset, [prediction(c, [bad])]))
    assert report["aggregate"]["clean_case_false_positive_rate"] == 1
    excluded = dataset.cases[-1]
    report = evaluate(dataset, bundle(dataset, [prediction(excluded, [bad])]))
    assert report["scored_cases"] == 0 and report["excluded_cases"] == 1
    assert report["aggregate"]["precision"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "duplicate_case",
        "patch",
        "syntax",
        "label",
        "range",
        "path",
        "context",
        "concept",
        "duplicate_defect",
    ],
)
def test_invalid_fixtures(dataset, mutation):
    raw = copy.deepcopy(dataset.model_dump())
    c = raw["cases"][0]
    if mutation == "version":
        raw["version"] = "2"
    elif mutation == "duplicate_case":
        raw["cases"].append(c)
    elif mutation == "patch":
        c["files"][0]["patch"] += "\n+invented"
    elif mutation == "syntax":
        c["files"][0]["head"] = "def !!!\n"
    elif mutation == "label":
        c["expected"][0]["file_path"] = "absent.py"
    elif mutation == "range":
        c["expected"][0]["end"] = 100
    elif mutation == "path":
        c["files"][0]["path"] = "../private.py"
    elif mutation == "context":
        c["context_required"] = True
    elif mutation == "concept":
        c["expected"][0]["concepts"] = [[""], ["x"]]
    elif mutation == "duplicate_defect":
        c["expected"].append(c["expected"][0])
    with pytest.raises((ValueError, SyntaxError)):
        validate_dataset(Dataset.model_validate(raw))


@pytest.mark.parametrize(
    "text", ["{", '{"format_version":"1.0","format_version":"1.0"}', "NaN", "[]", "{}"]
)
def test_malformed_prediction_files(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_predictions(path)


@pytest.mark.parametrize("mutation", ["hash", "unknown", "duplicate", "version"])
def test_prediction_identity_checks(dataset, mutation):
    p = bundle(dataset, [prediction(dataset.cases[0])])
    if mutation == "hash":
        p = p.model_copy(update={"dataset_sha256": "bad"})
    if mutation == "version":
        p = p.model_copy(update={"dataset_version": "2"})
    if mutation == "unknown":
        p = p.model_copy(update={"cases": [p.cases[0].model_copy(update={"case_id": "unknown"})]})
    if mutation == "duplicate":
        p = p.model_copy(update={"cases": p.cases * 2})
    with pytest.raises(ValueError):
        evaluate(dataset, p)


def test_offline_cli_is_reproducible_and_does_not_read_configuration(
    dataset, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AI_MAX_LLM_REQUESTS", "invalid-environment")
    path = tmp_path / "predictions.json"
    path.write_text(bundle(dataset, [prediction(dataset.cases[0])]).model_dump_json())
    outputs = [tmp_path / "a.json", tmp_path / "b.json"]
    for output in outputs:
        assert main(["offline", "--predictions", str(path), "--output", str(output)]) == 0
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert "SYNTHETIC" in capsys.readouterr().out
    assert main(["offline", "--predictions", str(path), "--output", str(outputs[0])]) == 2


def test_live_preview_never_constructs_provider(dataset, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert main(["live", "--cases", "inclusive-limit"]) == 0
    assert '"automatic_retries": false' in capsys.readouterr().out


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["missing"],
        ["ambiguous-cache"],
        ["inclusive-limit"] * 2,
        ["inclusive-limit", "empty-average", "last-item", "mutating-sort"],
    ],
)
def test_live_selection_limits(dataset, ids):
    with pytest.raises(ValueError):
        plan(dataset, settings(), ids)


def test_custom_dataset_refused_live(dataset):
    modified = dataset.model_copy(update={"provenance": "private"})
    with pytest.raises(ValueError):
        plan(modified, settings(), ["inclusive-limit"])


class Session:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.calls = []
        self.closed = False

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return turn

    async def aclose(self):
        self.closed = True


async def test_live_requires_approval_before_side_effects(dataset, tmp_path):
    with pytest.raises(ValueError):
        await execute(dataset, settings(), ["inclusive-limit"], tmp_path / "run")
    assert not (tmp_path / "run").exists()


async def test_live_reuses_real_context_tool_parser_and_validator(dataset, tmp_path):
    c = next(c for c in dataset.cases if c.id == "context-unit")
    findings = [finding(c), finding(c, line_number=999)]
    s = Session(
        [
            ModelTurn(
                calls=(
                    ToolCall(
                        "get_file_context", dict(path="src/cache.py", start_line=1, end_line=4)
                    ),
                )
            ),
            ModelTurn("Ready"),
            ModelTurn(json.dumps(dict(complete=True, findings=findings))),
        ]
    )
    result = await execute(
        dataset, settings(), [c.id], tmp_path / "run", approved=True, session_factory=lambda _: s
    )
    assert s.closed and len(s.calls) == 3
    payload = s.calls[1]["tool_results"][0].payload
    assert payload["ok"] and "milliseconds" in str(payload["lines"])
    raw = result["records"][0]["raw_review"]
    assert raw["status"] == "completed"
    assert len(raw["report"]["findings"]) == len(raw["report"]["rejected"]) == 1
    assert raw["usage"]["tool_calls"] == 1
    assert result["records"][0]["input_tokens"] is None
    saved = load_predictions(tmp_path / "run" / "predictions.json")
    assert len(saved.cases[0].findings) == 2  # Rejected predictions are not hidden.
    assert evaluate(dataset, saved)["aggregate"]["fp"] == 1
    assert "test-only-key" not in (tmp_path / "run" / "run.json").read_text()


@pytest.mark.parametrize(
    "code,status,count",
    [
        ("gemini_quota_exhausted", 429, 1),
        ("gemini_upstream_error", 502, 2),
        ("gemini_timeout", 504, 2),
        ("gemini_access_denied", 502, 1),
    ],
)
async def test_live_stops_on_quota_or_errors(dataset, tmp_path, code, status, count):
    sessions = []

    def factory(_):
        s = Session([ReviewFailure(status, code, "Safe failure")])
        sessions.append(s)
        return s

    result = await execute(
        dataset,
        settings(),
        ["inclusive-limit", "empty-average", "last-item"],
        tmp_path / "run",
        approved=True,
        session_factory=factory,
    )
    assert len(sessions) == count and all(len(s.calls) == 1 and s.closed for s in sessions)
    assert len(result["unattempted_cases"]) == 3 - count


async def test_live_budget_and_overwrite_safeguards(dataset, tmp_path):
    sessions = []

    def factory(_):
        s = Session([])
        sessions.append(s)
        return s

    target = tmp_path / "run"
    result = await execute(
        dataset,
        settings(ai_max_llm_requests=1),
        ["inclusive-limit", "empty-average"],
        target,
        approved=True,
        session_factory=factory,
    )
    assert len(sessions) == 1 and not sessions[0].calls
    assert result["stop_reason"] == "review_error_or_budget_limit"
    with pytest.raises(FileExistsError):
        await execute(
            dataset, settings(), ["inclusive-limit"], target, approved=True, session_factory=factory
        )
    assert len(sessions) == 1


async def test_fixture_file_size_and_identity_limits(dataset):
    client = FixtureGitHub(dataset.cases[0], settings(ai_max_file_bytes=1))
    from app.github import GitHubError

    with pytest.raises(GitHubError):
        await client.get_file_at_head(client.review, "src/service.py")
    with pytest.raises(GitHubError):
        await client.get_file_at_head(client.review, "../secret")


def test_patch_generation_roundtrip(dataset):
    for c in dataset.cases:
        for f in c.files:
            assert patch_for(f.base, f.head, f.path) == f.patch


def test_location_accuracy_penalizes_invalid_file_and_line(dataset):
    c = dataset.cases[0]
    for change in ({"line_number": 999}, {"file_path": "other.py"}):
        report = evaluate(dataset, bundle(dataset, [prediction(c, [finding(c, **change)])]))
        assert report["aggregate"]["location_accuracy"] == 0


def test_saved_synthetic_demo_is_reproducible(dataset):
    from pathlib import Path

    predictions = load_predictions(Path("benchmarks/synthetic_predictions.json"))
    report = evaluate(dataset, predictions)
    assert report == json.loads(Path("benchmarks/synthetic_report.json").read_text())
    assert report["provenance"] == "synthetic"
    assert (report["aggregate"]["tp"], report["aggregate"]["fp"], report["aggregate"]["fn"]) == (
        3,
        3,
        2,
    )


async def test_evaluation_with_actual_sdk_and_mock_transport(dataset, tmp_path):
    import httpx
    from google import genai
    from google.genai import types
    from app.gemini_provider import GeminiSession

    calls = []
    c = dataset.cases[0]

    def handler(request):
        calls.append(json.loads(request.content))
        assert request.url.host == "generativelanguage.googleapis.com"
        assert "gemini-3.5-flash-lite" in request.url.path
        text = (
            "Ready" if len(calls) == 1 else json.dumps({"complete": True, "findings": [finding(c)]})
        )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": text}]},
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def factory(config):
        sdk = genai.Client(
            api_key="test-only-key",
            vertexai=False,
            enterprise=False,
            http_options=types.HttpOptions(
                httpx_async_client=http, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
        return GeminiSession(config, sdk_client=sdk)

    try:
        result = await execute(
            dataset, settings(), [c.id], tmp_path / "run", approved=True, session_factory=factory
        )
    finally:
        await http.aclose()
    assert len(calls) == 2
    assert "tools" in calls[0] and "tools" not in calls[1]
    assert calls[1]["generationConfig"]["responseMimeType"] == "application/json"
    assert result["records"][0]["raw_review"]["report"]["findings"]
    assert "concepts" not in json.dumps(calls)  # No evaluation labels sent to Gemini.


async def test_secret_model_output_is_not_persisted(dataset, tmp_path):
    c = dataset.cases[0]
    secret_finding = finding(c, explanation="test-only-key")
    session = Session(
        [
            ModelTurn("Ready"),
            ModelTurn(json.dumps({"complete": True, "findings": [secret_finding]})),
        ]
    )
    result = await execute(
        dataset,
        settings(),
        [c.id],
        tmp_path / "run",
        approved=True,
        session_factory=lambda _: session,
    )
    assert result["records"][0]["raw_review"]["issues"][0]["code"] == "sensitive_model_output"
    assert "test-only-key" not in (tmp_path / "run" / "run.json").read_text()
    assert load_predictions(tmp_path / "run" / "predictions.json").cases[0].findings == []

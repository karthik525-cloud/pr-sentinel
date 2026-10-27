"""Deterministic semantic-rubric + location matching; no APIs or model judge."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from app.diff_parser import parse_review
from app.evaluation.dataset import Case, Dataset, Defect, dataset_digest, review_for
from app.finding_validator import validate_findings
from app.findings import ReviewFinding, StrictModel
from app.review_agent import _strict_json

CATEGORIES = ("bug", "security", "reliability", "performance")
MATCHING_VERSION = "rubric-location-bipartite-v1"


class PredictionCase(StrictModel):
    case_id: str
    findings: list[object] = Field(max_length=10)
    status: Literal["completed", "incomplete", "error"] = "completed"


class Predictions(StrictModel):
    format_version: Literal["1.0"]
    dataset_version: str
    dataset_sha256: str
    provenance: Literal["synthetic", "recorded-live", "unverified-replay"]
    model: str | None = None
    cases: list[PredictionCase] = Field(min_length=1)


def load_predictions(path: Path) -> Predictions:
    if path.stat().st_size > 4_000_000:
        raise ValueError("Predictions too large")
    return Predictions.model_validate(_strict_json(path.read_text()))


def semantic_match(finding: ReviewFinding, defect: Defect) -> bool:
    text = " ".join((finding.title + " " + finding.explanation).casefold().split())
    return all(any(phrase.casefold() in text for phrase in group) for group in defect.concepts)


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def metrics(tp: int, fp: int, fn: int) -> dict:
    return dict(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=ratio(tp, tp + fp),
        recall=ratio(tp, tp + fn),
        f1=ratio(2 * tp, 2 * tp + fp + fn),
    )


def score_case(case: Case, prediction: PredictionCase) -> dict:
    if case.excluded_reason:
        return dict(case_id=case.id, excluded=True, reason=case.excluded_reason)
    # Validate independently so even duplicate predictions keep their original indices.
    parsed = parse_review(review_for(case))
    findings = []
    rejected = {}
    for i, raw in enumerate(prediction.findings):
        report = validate_findings(parsed, [raw])
        if report.findings:
            findings.append((i, report.findings[0]))
        else:
            rejected[i] = report.rejected[0].code
    labels = case.expected
    edges = {
        i: [
            j
            for j, d in enumerate(labels)
            if f.file_path == d.file_path
            and d.start <= f.line_number <= d.end
            and semantic_match(f, d)
        ]
        for i, f in findings
    }

    @lru_cache(None)
    def assign(pos: int, used: int) -> tuple[int, int, tuple[tuple[int, int], ...]]:
        if pos == len(findings):
            return 0, 0, ()
        i, f = findings[pos]
        best = assign(pos + 1, used)
        for j in edges[i]:
            if used & (1 << j):
                continue
            count, exact, pairs = assign(pos + 1, used | (1 << j))
            proposal = count + 1, exact + int(f.line_number == labels[j].line), ((i, j),) + pairs
            # Maximize cardinality, then exact locations; ties prefer earlier input/label indices.
            if proposal[:2] > best[:2] or (proposal[:2] == best[:2] and proposal[2] < best[2]):
                best = proposal
        return best

    _, _, pairs = assign(0, 0)
    matched = dict(pairs)
    used = set(matched.values())
    matches, failures = [], []
    category_counts = {c: [0, 0, 0] for c in CATEGORIES}
    exact = severity = category = semantic_located = semantic_candidates = 0
    # Location accuracy includes schema-valid predictions rejected for wrong file/line.
    for raw in prediction.findings:
        try:
            candidate = ReviewFinding.model_validate(raw)
        except ValidationError:
            continue
        related = [d for d in labels if semantic_match(candidate, d)]
        if related:
            semantic_candidates += 1
            semantic_located += int(
                any(
                    candidate.file_path == d.file_path and d.start <= candidate.line_number <= d.end
                    for d in related
                )
            )
    by_index = dict(findings)
    for i, f in findings:
        semantic_labels = [d for d in labels if semantic_match(f, d)]
        if i in matched:
            d = labels[matched[i]]
            exact += f.line_number == d.line
            severity += f.severity == d.severity
            category += f.category == d.category
            category_counts[d.category][0] += 1
            matches.append(
                dict(
                    prediction_index=i,
                    defect_id=d.id,
                    location="exact" if f.line_number == d.line else "acceptable_range",
                    severity_agrees=f.severity == d.severity,
                    category_agrees=f.category == d.category,
                    prediction=f.model_dump(),
                    expected=d.model_dump(),
                )
            )
        else:
            reason = (
                "duplicate"
                if edges[i]
                else ("wrong_location" if semantic_labels else "unmatched_claim")
            )
            failures.append(dict(prediction_index=i, reason=reason, prediction=f.model_dump()))
            category_counts[f.category][1] += 1
    for i, reason in rejected.items():
        failures.append(dict(prediction_index=i, reason=reason))
    missing = [d.model_dump() for j, d in enumerate(labels) if j not in used]
    for j, d in enumerate(labels):
        if j not in used:
            category_counts[d.category][2] += 1
    # Invalid predictions are FPs in aggregate; category is unavailable/untrusted.
    # Preserve wrong-location diagnostics even when Stage 2 rejected the candidate.
    for failure in failures:
        i = failure["prediction_index"]
        if i not in by_index:
            failure["invalid"] = True
    tp = len(matches)
    return dict(
        case_id=case.id,
        excluded=False,
        status=prediction.status,
        **metrics(tp, len(prediction.findings) - tp, len(labels) - tp),
        clean=not labels,
        matches=matches,
        false_positives=sorted(failures, key=lambda x: x["prediction_index"]),
        false_negatives=missing,
        exact_matches=exact,
        range_matches=tp - exact,
        severity_agreements=severity,
        category_agreements=category,
        semantic_candidates=semantic_candidates,
        semantic_located=semantic_located,
        invalid_predictions=len(rejected),
        category_counts=category_counts,
    )


def evaluate(dataset: Dataset, predictions: Predictions) -> dict:
    if (
        predictions.dataset_version != dataset.version
        or predictions.dataset_sha256 != dataset_digest(dataset)
    ):
        raise ValueError("Dataset identity mismatch")
    ids = [p.case_id for p in predictions.cases]
    if len(set(ids)) != len(ids) or not set(ids) <= {c.id for c in dataset.cases}:
        raise ValueError("Duplicate or unknown prediction case IDs")
    selected = {p.case_id: p for p in predictions.cases}
    results = [score_case(c, selected[c.id]) for c in dataset.cases if c.id in selected]
    scored = [r for r in results if not r["excluded"]]

    def total(key):
        return sum(r[key] for r in scored)

    aggregate = metrics(total("tp"), total("fp"), total("fn"))
    clean = [r for r in scored if r["clean"]]
    aggregate.update(
        clean_case_false_positive_rate=ratio(sum(r["fp"] > 0 for r in clean), len(clean)),
        location_accuracy=ratio(total("semantic_located"), total("semantic_candidates")),
        exact_location_share=ratio(total("exact_matches"), total("tp")),
        severity_agreement=ratio(total("severity_agreements"), total("tp")),
        category_agreement=ratio(total("category_agreements"), total("tp")),
        invalid_predictions=total("invalid_predictions"),
        error_rate=ratio(sum(r["status"] == "error" for r in scored), len(scored)),
        incomplete_rate=ratio(sum(r["status"] == "incomplete" for r in scored), len(scored)),
    )
    return dict(
        dataset_version=dataset.version,
        dataset_sha256=dataset_digest(dataset),
        provenance=predictions.provenance,
        model=predictions.model,
        configuration=dict(
            matching_version=MATCHING_VERSION,
            category_required_for_detection=False,
            severity_required_for_detection=False,
        ),
        selected_case_ids=ids,
        unselected_case_ids=[c.id for c in dataset.cases if c.id not in selected],
        scored_cases=len(scored),
        excluded_cases=len(results) - len(scored),
        aggregate=aggregate,
        per_category={
            c: metrics(*(sum(r["category_counts"][c][i] for r in scored) for i in range(3)))
            for c in CATEGORIES
        },
        unclassified_false_positives=total("invalid_predictions"),
        cases=results,
    )


def summary(report: dict) -> str:
    a = report["aggregate"]

    def fmt(x):
        return "N/A" if x is None else f"{x:.3f}"

    return (
        f"{report['provenance'].upper()} — {report['scored_cases']} scored cases; "
        f"{report['excluded_cases']} excluded\n"
        f"TP={a['tp']} FP={a['fp']} FN={a['fn']} | precision={fmt(a['precision'])} "
        f"recall={fmt(a['recall'])} F1={fmt(a['f1'])}\n"
        f"Clean-case FP rate={fmt(a['clean_case_false_positive_rate'])}; "
        f"location accuracy={fmt(a['location_accuracy'])}"
    )

"""Create evaluator demonstration inputs; these are NOT Gemini predictions."""

import json
from pathlib import Path

from app.evaluation.dataset import dataset_digest, load_dataset
from app.evaluation.scoring import PredictionCase, Predictions, evaluate, summary


def main() -> None:
    dataset = load_dataset()
    cases = {c.id: c for c in dataset.cases}

    def prediction(id: str, **changes) -> dict:
        d = cases[id].expected[0]
        return (
            dict(
                file_path=d.file_path,
                line_number=d.line,
                severity=d.severity,
                category=d.category,
                title="Synthetic evaluator example",
                explanation=d.explanation,
                suggested_fix="Restore the documented behavior.",
            )
            | changes
        )

    inputs = [
        PredictionCase(
            case_id="inclusive-limit",
            findings=[prediction("inclusive-limit"), prediction("inclusive-limit")],
        ),
        PredictionCase(case_id="empty-average", findings=[]),
        PredictionCase(case_id="last-item", findings=[prediction("last-item", line_number=999)]),
        PredictionCase(
            case_id="context-unit",
            findings=[prediction("context-unit", category="reliability", severity="low")],
        ),
        PredictionCase(
            case_id="mutating-sort", findings=[prediction("mutating-sort", line_number=3)]
        ),
        PredictionCase(case_id="clean-timeout", findings=[prediction("inclusive-limit")]),
        PredictionCase(case_id="clean-set", findings=[]),
        PredictionCase(case_id="ambiguous-rounding", findings=[]),
    ]
    predictions = Predictions(
        format_version="1.0",
        dataset_version=dataset.version,
        dataset_sha256=dataset_digest(dataset),
        provenance="synthetic",
        cases=inputs,
    )
    Path("benchmarks/synthetic_predictions.json").write_text(
        predictions.model_dump_json(indent=2) + "\n"
    )
    report = evaluate(dataset, predictions)
    Path("benchmarks/synthetic_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(summary(report))


if __name__ == "__main__":
    main()

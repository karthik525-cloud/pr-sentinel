"""python -m app.evaluation offline|live (live defaults to preview)."""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.evaluation.dataset import DEFAULT_DATASET, load_dataset
from app.evaluation.live import execute, plan, write_json
from app.evaluation.scoring import evaluate, load_predictions, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    offline = commands.add_parser("offline")
    offline.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    offline.add_argument("--predictions", type=Path, required=True)
    offline.add_argument("--output", type=Path, required=True)
    live = commands.add_parser("live")
    live.add_argument("--cases", nargs="+", required=True)
    live.add_argument("--execute", action="store_true")
    live.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        dataset = load_dataset(args.dataset if args.mode == "offline" else DEFAULT_DATASET)
        if args.mode == "offline":
            report = evaluate(dataset, load_predictions(args.predictions))
            # Reserve output exclusively: never overwrite a fixture or prior report.
            with args.output.open("x") as stream:
                json.dump(report, stream, indent=2, allow_nan=False)
                stream.write("\n")
            print(summary(report))
            return 0
        settings = Settings.from_env()
        preview = plan(dataset, settings, args.cases)
        print(json.dumps(preview, indent=2), flush=True)
        if not args.execute:
            return 0
        if args.output is None:
            raise ValueError("Execution requires a new output directory")
        result = asyncio.run(execute(dataset, settings, args.cases, args.output, approved=True))
        report = evaluate(dataset, load_predictions(args.output / "predictions.json"))
        write_json(args.output / "evaluation.json", report)
        print(summary(report))
        return 1 if any(r["raw_review"]["status"] != "completed" for r in result["records"]) else 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Interrupted; completed-case checkpoints remain. No automatic retry.")
        return 130
    except Exception:
        # Do not echo Pydantic inputs, paths, credentials, SDK details or fixture content.
        print(
            "Evaluation failed: invalid configuration, fixture/prediction input, or output destination."
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

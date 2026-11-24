"""``python -m ipsensitive.eval_cli`` - regenerate the evaluation report.

Exits non-zero when the merged detection misses the ticket's thresholds, so the
command can be wired into CI unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .evaluation import (
    DEFAULT_DATASET,
    DEFAULT_MODEL_OUTPUT,
    DEFAULT_REPORT_PREFIX,
    PRECISION_FLOOR,
    RECALL_FLOOR,
    evaluate,
    write_reports,
)
from .moderation.model_client import RecordedModelClient
from .moderation.prompt import load_prompt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the merged sensitive-content detection")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET), help="evaluation set JSON")
    parser.add_argument("--model", default=str(DEFAULT_MODEL_OUTPUT), help="recorded model answers JSON")
    parser.add_argument("--out", default=str(DEFAULT_REPORT_PREFIX), help="report prefix (without extension)")
    parser.add_argument("--min-confidence", type=float, default=0.5, help="model-only confidence floor")
    parser.add_argument("--prompt", default=None, help="prompt file to load (default: prompts/ip.sensitive.v1.md)")
    args = parser.parse_args(argv)

    prompt = load_prompt(args.prompt) if args.prompt else load_prompt()
    settings = Settings(
        model_mode="recorded",
        model_replay_path=Path(args.model),
        min_model_confidence=args.min_confidence,
    )
    client = RecordedModelClient.from_file(args.model)
    report = evaluate(dataset_path=args.dataset, model_client=client, prompt=prompt, settings=settings)
    markdown_path, json_path = write_reports(report, args.out)

    summary = {
        "dataset": report.dataset_id,
        "documents": len(report.documents),
        "gold": report.gold_total,
        "detected": report.detected_total,
        "tp": report.tp,
        "fp": report.fp,
        "fn": report.fn,
        "recall": round(report.recall, 4),
        "precision": round(report.precision, 4),
        "recall_floor": RECALL_FLOOR,
        "precision_floor": PRECISION_FLOOR,
        "passes": report.passes,
        "report_markdown": str(markdown_path),
        "report_json": str(json_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report.passes else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())

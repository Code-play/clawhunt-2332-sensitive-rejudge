"""Acceptance criterion 1: merged detection on the fictional set, recall >= 95%."""

from __future__ import annotations

import json
from pathlib import Path

from ipsensitive.evaluation import (
    DEFAULT_DATASET,
    DEFAULT_REPORT_PREFIX,
    PRECISION_FLOOR,
    RECALL_FLOOR,
    evaluate,
    load_dataset,
)

ROOT = Path(__file__).resolve().parents[1]


def test_evaluation_set_is_fictional_and_batches_a_long_draft():
    dataset_id, version, documents = load_dataset(DEFAULT_DATASET)
    assert dataset_id == "ip-sensitive-eval-v1" and version == 1
    raw = json.loads(Path(DEFAULT_DATASET).read_text(encoding="utf-8"))
    assert raw["fictional"] is True
    assert len(documents) >= 6
    assert any(len(document.segments) > 30 for document in documents), "the set must exercise multi-batch batching"


def test_merged_detection_meets_the_ticket_thresholds(recorded_client, prompt, settings):
    report = evaluate(
        dataset_path=DEFAULT_DATASET,
        model_client=recorded_client,
        prompt=prompt,
        settings=settings,
    )
    assert report.recall >= RECALL_FLOOR, f"recall {report.recall:.4f} below {RECALL_FLOOR}"
    assert report.precision >= PRECISION_FLOOR, f"precision {report.precision:.4f} below {PRECISION_FLOOR}"
    assert report.gold_total >= 20
    assert report.tp == report.gold_total - report.fn
    assert report.passes is True


def test_evaluation_is_not_trivially_perfect(recorded_client, prompt, settings):
    # The set contains one deliberately missed fragment, several over-flags and
    # both drop paths, so the numbers above mean something.
    report = evaluate(dataset_path=DEFAULT_DATASET, model_client=recorded_client, prompt=prompt, settings=settings)
    assert report.fn >= 1
    assert report.fp >= 1
    assert report.dropped_by_code.get("quote_not_found", 0) >= 1
    assert report.dropped_by_code.get("low_confidence", 0) >= 1
    assert report.mask_replacements >= 1
    assert report.context_seq_flags >= 1


def test_committed_report_matches_a_fresh_evaluation(recorded_client, prompt, settings):
    report = evaluate(dataset_path=DEFAULT_DATASET, model_client=recorded_client, prompt=prompt, settings=settings)
    committed = json.loads(Path(str(DEFAULT_REPORT_PREFIX) + ".json").read_text(encoding="utf-8"))
    assert committed["dataset_id"] == report.dataset_id
    assert committed["prompt_version"] == report.prompt_version
    assert committed["model_id"] == report.model_id
    assert committed["summary"]["gold"] == report.gold_total
    assert committed["summary"]["detected"] == report.detected_total
    assert committed["summary"]["tp"] == report.tp
    assert committed["summary"]["fp"] == report.fp
    assert committed["summary"]["fn"] == report.fn
    assert committed["summary"]["recall"] == round(report.recall, 4)
    assert committed["summary"]["precision"] == round(report.precision, 4)
    assert committed["summary"]["passes"] is True
    assert committed["thresholds"] == {"recall": RECALL_FLOOR, "precision": PRECISION_FLOOR}
    assert [doc["content_id"] for doc in committed["documents"]] == [doc.content_id for doc in report.documents]


def test_committed_markdown_report_is_current(recorded_client, prompt, settings):
    report = evaluate(dataset_path=DEFAULT_DATASET, model_client=recorded_client, prompt=prompt, settings=settings)
    markdown = Path(str(DEFAULT_REPORT_PREFIX) + ".md").read_text(encoding="utf-8")
    assert f"| Recall | {report.recall:.2%} ({report.tp}/{report.gold_total})" in markdown
    assert f"| Precision | {report.precision:.2%} ({report.tp}/{report.detected_total})" in markdown
    assert f">= {RECALL_FLOOR:.0%}" in markdown and f">= {PRECISION_FLOOR:.0%}" in markdown
    assert "third_party_personal" in markdown and "unpublished_business" in markdown

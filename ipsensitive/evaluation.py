"""Offline evaluation of the merged rule + model detection.

The evaluation runs the production path (masking gate, batching, recorded model
answers, rule stub, merge) over the fictional evaluation set and scores the
surviving fragments against the gold labels.  It produces the recall / precision
report the ticket asks for, as Markdown and JSON.

Matching rule (fragment level, type-aware):

* a detection and a gold flag belong to the same fragment when they are in the
  same segment and share at least half of the shorter span;
* **recall** = gold flags with at least one such detection / all gold flags;
* **precision** = detections with at least one such gold flag / all detections;
* the primary metrics require the flag_type to agree; span-only metrics ignore
  the type and are reported alongside for transparency.

The overlap definition is deliberately fragment-based rather than character
based: the merge unions overlapping hits (a rule span plus a wider model span),
so a surviving record can be longer than the gold fragment it covers.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, Settings
from .moderation.merge import Candidate, MergeOutcome
from .moderation.model_client import ModelClient
from .moderation.pipeline import Detection, detect_document
from .moderation.prompt import PromptSpec, load_prompt
from .moderation.rules import RuleSource
from .moderation.schema import FLAG_TYPES
from .moderation.segments import Segment, normalise_segments

RECALL_FLOOR = 0.95
PRECISION_FLOOR = 0.60
OVERLAP_FLOOR = 0.5

DEFAULT_DATASET = PROJECT_ROOT / "evalset" / "eval_set_v1.json"
DEFAULT_MODEL_OUTPUT = PROJECT_ROOT / "evalset" / "model_output_v1.json"
DEFAULT_REPORT_PREFIX = PROJECT_ROOT / "reports" / "evaluation_v1"


class EvaluationError(ValueError):
    """The evaluation set or its gold labels are inconsistent."""


@dataclass(frozen=True)
class GoldFlag:
    seq: int
    quote: str
    flag_type: str
    start: int
    end: int


@dataclass(frozen=True)
class Document:
    content_id: str
    title: str
    segments: tuple[Segment, ...]
    gold_raw: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class DocumentScore:
    content_id: str
    title: str
    segments: int
    batches: int
    gold_total: int
    detected_total: int
    tp: int
    fp: int
    fn: int
    recall: float
    precision: float
    recall_span: float
    precision_span: float
    missed: tuple[dict[str, Any], ...]
    false_positives: tuple[dict[str, Any], ...]
    dropped_by_code: dict[str, int]
    model_status: str


@dataclass(frozen=True)
class EvaluationReport:
    dataset_id: str
    dataset_version: int
    prompt_version: str
    model_id: str
    documents: tuple[DocumentScore, ...]
    gold_total: int
    detected_total: int
    tp: int
    fp: int
    fn: int
    recall: float
    precision: float
    recall_span: float
    precision_span: float
    per_type: dict[str, dict[str, float]]
    dropped_by_code: dict[str, int]
    context_seq_flags: int
    mask_replacements: int
    min_model_confidence: float
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def passes(self) -> bool:
        return self.recall >= RECALL_FLOOR and self.precision >= PRECISION_FLOOR

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "prompt_version": self.prompt_version,
            "model_id": self.model_id,
            "generated_at": self.generated_at,
            "thresholds": {"recall": RECALL_FLOOR, "precision": PRECISION_FLOOR},
            "summary": {
                "gold": self.gold_total,
                "detected": self.detected_total,
                "tp": self.tp,
                "fp": self.fp,
                "fn": self.fn,
                "recall": round(self.recall, 4),
                "precision": round(self.precision, 4),
                "recall_span_only": round(self.recall_span, 4),
                "precision_span_only": round(self.precision_span, 4),
                "passes": self.passes,
            },
            "per_type": self.per_type,
            "dropped_by_code": self.dropped_by_code,
            "context_seq_flags": self.context_seq_flags,
            "mask_replacements": self.mask_replacements,
            "min_model_confidence": self.min_model_confidence,
            "documents": [
                {
                    "content_id": doc.content_id,
                    "title": doc.title,
                    "segments": doc.segments,
                    "batches": doc.batches,
                    "gold": doc.gold_total,
                    "detected": doc.detected_total,
                    "tp": doc.tp,
                    "fp": doc.fp,
                    "fn": doc.fn,
                    "recall": round(doc.recall, 4),
                    "precision": round(doc.precision, 4),
                    "recall_span_only": round(doc.recall_span, 4),
                    "precision_span_only": round(doc.precision_span, 4),
                    "model_status": doc.model_status,
                    "missed": list(doc.missed),
                    "false_positives": list(doc.false_positives),
                    "dropped_by_code": doc.dropped_by_code,
                }
                for doc in self.documents
            ],
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append(f"# Sensitive re-review evaluation - {self.dataset_id}")
        lines.append("")
        lines.append(
            "Merged rule + model detection on a fully fictional evaluation set "
            f"(prompt `{self.prompt_version}`, model `{self.model_id}`)."
        )
        lines.append("")
        lines.append("| Metric | Value | Floor | Result |")
        lines.append("|---|---|---|---|")
        lines.append(
            f"| Recall | {self.recall:.2%} ({self.tp}/{self.gold_total}) | "
            f">= {RECALL_FLOOR:.0%} | {'pass' if self.recall >= RECALL_FLOOR else 'FAIL'} |"
        )
        lines.append(
            f"| Precision | {self.precision:.2%} ({self.tp}/{self.detected_total}) | "
            f">= {PRECISION_FLOOR:.0%} | {'pass' if self.precision >= PRECISION_FLOOR else 'FAIL'} |"
        )
        lines.append(
            f"| Recall (span only, type ignored) | {self.recall_span:.2%} | - | - |"
        )
        lines.append(
            f"| Precision (span only, type ignored) | {self.precision_span:.2%} | - | - |"
        )
        lines.append("")
        lines.append(
            f"Generated at {self.generated_at}. Minimum model-only confidence "
            f"{self.min_model_confidence}. Masking gate replacements: {self.mask_replacements}. "
            f"Context-segment model flags kept as diagnostics: {self.context_seq_flags}."
        )
        lines.append("")
        lines.append("## Per document")
        lines.append("")
        lines.append("| Document | Segments | Batches | Gold | Detected | TP | FP | FN | Recall | Precision |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for doc in self.documents:
            lines.append(
                f"| {doc.content_id} {doc.title} | {doc.segments} | {doc.batches} | {doc.gold_total} | "
                f"{doc.detected_total} | {doc.tp} | {doc.fp} | {doc.fn} | "
                f"{doc.recall:.0%} | {doc.precision:.0%} |"
            )
        lines.append("")
        lines.append("## Per flag type")
        lines.append("")
        lines.append("| Flag type | Gold | Detected | TP | Recall | Precision |")
        lines.append("|---|---|---|---|---|---|")
        for flag_type in FLAG_TYPES:
            row = self.per_type[flag_type]
            lines.append(
                f"| {flag_type} | {int(row['gold'])} | {int(row['detected'])} | {int(row['tp'])} | "
                f"{row['recall']:.0%} | {row['precision']:.0%} |"
            )
        lines.append("")
        lines.append("## Dropped hits")
        lines.append("")
        if self.dropped_by_code:
            lines.append("| Code | Count | Meaning |")
            lines.append("|---|---|---|")
            meanings = {
                "quote_not_found": "quote is not verbatim in the segment - the model paraphrased it",
                "low_confidence": "model-only hit below the confidence floor",
                "unknown_seq": "the model referenced a segment the draft does not have",
            }
            for code, count in self.dropped_by_code.items():
                lines.append(f"| {code} | {count} | {meanings.get(code, 'rejected entry')} |")
        else:  # pragma: no cover - the shipped set always drops something
            lines.append("None.")
        lines.append("")
        lines.append("## Missed gold fragments")
        lines.append("")
        missed = [item for doc in self.documents for item in doc.missed]
        if missed:
            for item in missed:
                lines.append(f"- {item['content_id']} segment {item['seq']}: `{item['quote']}` ({item['flag_type']})")
        else:  # pragma: no cover - the shipped set misses one fragment on purpose
            lines.append("None.")
        lines.append("")
        lines.append("## False positives kept")
        lines.append("")
        false_positives = [item for doc in self.documents for item in doc.false_positives]
        if false_positives:
            for item in false_positives:
                lines.append(
                    f"- {item['content_id']} segment {item['seq']}: `{item['quote']}` "
                    f"({item['flag_type']}, {item['detector']}, confidence {item['confidence']})"
                )
        else:  # pragma: no cover - the shipped set has over-flagged fragments on purpose
            lines.append("None.")
        lines.append("")
        lines.append("## Method")
        lines.append("")
        lines.append("1. Each draft is passed through the unified masking gate, so the model sees `[PII:...]`")
        lines.append("   placeholders instead of phone numbers, e-mail addresses, card numbers or ID numbers.")
        lines.append("2. Segments are batched 30 per call with one context segment on each side; the recorded")
        lines.append("   model answers are validated per entry (unknown fields, unknown flag types, reason length).")
        lines.append("3. The rule stage runs on the same masked text; rule hits and model flags are merged:")
        lines.append("   overlapping hits become one record (`rule+model`), model-only hits below 0.5 are")
        lines.append("   dropped, and a quote that is not verbatim in the segment is dropped.")
        lines.append("4. A detection and a gold flag match when they are in the same segment, share at least")
        lines.append("   half of the shorter span and - for the primary metrics - carry the same flag type.")
        lines.append("")
        lines.append("## Confidentiality")
        lines.append("")
        lines.append(
            "Every person, enterprise, brand and number in the evaluation set is invented for this "
            "ticket. No real personal or enterprise information and no client name appears in the set, "
            "the recorded answers or this report."
        )
        lines.append("")
        return "\n".join(lines)


def load_dataset(path: str | Path = DEFAULT_DATASET) -> tuple[str, int, tuple[Document, ...]]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"evaluation set not found: {source}")
    document = json.loads(source.read_text(encoding="utf-8"))
    dataset_id = str(document.get("dataset_id") or source.stem)
    version = int(document.get("version") or 1)
    documents: list[Document] = []
    seen: set[str] = set()
    for entry in document.get("documents") or []:
        content_id = str(entry["content_id"])
        if content_id in seen:
            raise EvaluationError(f"duplicate content_id {content_id!r} in the evaluation set")
        seen.add(content_id)
        segments = normalise_segments(entry["segments"])
        gold = tuple(entry.get("gold") or [])
        for item in gold:
            if item["flag_type"] not in FLAG_TYPES:
                raise EvaluationError(f"{content_id}: gold flag type {item['flag_type']!r} is not allowed")
            if int(item["seq"]) not in {segment.seq for segment in segments}:
                raise EvaluationError(f"{content_id}: gold flag references missing segment {item['seq']}")
        documents.append(
            Document(
                content_id=content_id,
                title=str(entry.get("title") or ""),
                segments=segments,
                gold_raw=gold,
            )
        )
    return dataset_id, version, tuple(documents)


def _resolve_gold(detection: Detection, document: Document) -> tuple[GoldFlag, ...]:
    by_seq = {segment.seq: segment.text for segment in detection.masked.segments}
    resolved: list[GoldFlag] = []
    for item in document.gold_raw:
        seq = int(item["seq"])
        quote = str(item["quote"])
        text = by_seq.get(seq)
        if text is None:
            raise EvaluationError(f"{document.content_id}: gold flag on missing segment {seq}")
        start = text.find(quote)
        if start < 0:
            raise EvaluationError(
                f"{document.content_id} segment {seq}: gold quote {quote!r} is not in the masked text "
                f"{text!r}; fix the evaluation set (a masking change can break a gold label)"
            )
        resolved.append(
            GoldFlag(seq=seq, quote=quote, flag_type=str(item["flag_type"]), start=start, end=start + len(quote))
        )
    return tuple(resolved)


def _overlaps(a_seq: int, a_start: int, a_end: int, b_seq: int, b_start: int, b_end: int) -> bool:
    if a_seq != b_seq:
        return False
    intersection = min(a_end, b_end) - max(a_start, b_start)
    if intersection <= 0:
        return False
    shorter = min(a_end - a_start, b_end - b_start)
    return intersection >= OVERLAP_FLOOR * shorter


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def score_document(
    *,
    document: Document,
    detection: Detection,
) -> tuple[DocumentScore, tuple[int, ...], tuple[int, ...]]:
    gold = _resolve_gold(detection, document)
    detections: Sequence[Candidate] = detection.outcome.kept

    strict_gold_hit: list[bool] = [False] * len(gold)
    strict_detection_hit: list[bool] = [False] * len(detections)
    span_gold_hit: list[bool] = [False] * len(gold)
    span_detection_hit: list[bool] = [False] * len(detections)
    for det_index, det in enumerate(detections):
        for gold_index, gold_flag in enumerate(gold):
            if not _overlaps(det.seq, det.start, det.end, gold_flag.seq, gold_flag.start, gold_flag.end):
                continue
            span_detection_hit[det_index] = True
            span_gold_hit[gold_index] = True
            if det.flag_type == gold_flag.flag_type:
                strict_detection_hit[det_index] = True
                strict_gold_hit[gold_index] = True

    tp = sum(strict_detection_hit)
    fp = len(detections) - tp
    fn = len(gold) - sum(strict_gold_hit)
    missed = tuple(
        {
            "content_id": document.content_id,
            "seq": gold_flag.seq,
            "quote": gold_flag.quote,
            "flag_type": gold_flag.flag_type,
        }
        for gold_flag, hit in zip(gold, strict_gold_hit, strict=False)
        if not hit
    )
    false_positives = tuple(
        {
            "content_id": document.content_id,
            "seq": det.seq,
            "quote": det.quote,
            "flag_type": det.flag_type,
            "detector": det.detector,
            "confidence": det.confidence,
        }
        for det, hit in zip(detections, strict_detection_hit, strict=False)
        if not hit
    )
    dropped: Mapping[str, int] = detection.outcome.dropped_by_code
    score = DocumentScore(
        content_id=document.content_id,
        title=document.title,
        segments=len(document.segments),
        batches=len(detection.batches),
        gold_total=len(gold),
        detected_total=len(detections),
        tp=tp,
        fp=fp,
        fn=fn,
        recall=_ratio(len(gold) - fn, len(gold)),
        precision=_ratio(tp, len(detections)),
        recall_span=_ratio(sum(span_gold_hit), len(gold)),
        precision_span=_ratio(sum(span_detection_hit), len(detections)),
        missed=missed,
        false_positives=false_positives,
        dropped_by_code=dict(sorted(dropped.items())),
        model_status=detection.model_status,
    )
    strict_pairs = tuple(gold_index for gold_index, hit in enumerate(strict_gold_hit) if hit)
    span_pairs = tuple(gold_index for gold_index, hit in enumerate(span_gold_hit) if hit)
    return score, strict_pairs, span_pairs


def evaluate(
    *,
    dataset_path: str | Path = DEFAULT_DATASET,
    model_client: ModelClient,
    prompt: PromptSpec | None = None,
    settings: Settings | None = None,
    rule_source: RuleSource | None = None,
) -> EvaluationReport:
    dataset_id, version, documents = load_dataset(dataset_path)
    prompt_spec = prompt or load_prompt()
    settings = settings or Settings(min_model_confidence=0.5)

    scores: list[DocumentScore] = []
    type_gold: Counter[str] = Counter()
    type_detected: Counter[str] = Counter()
    type_tp: Counter[str] = Counter()
    type_recall_hits: Counter[str] = Counter()
    type_span_hits: Counter[str] = Counter()
    dropped: Counter[str] = Counter()
    context_seq_flags = 0
    mask_replacements = 0
    for document in documents:
        detection = detect_document(
            document.segments,
            model_client=model_client,
            prompt=prompt_spec,
            settings=settings,
            rule_source=rule_source,
            document_key=document.content_id,
        )
        score, strict_hits, span_hits = score_document(document=document, detection=detection)
        scores.append(score)
        dropped.update(score.dropped_by_code)
        context_seq_flags += detection.outcome.context_seq_flags
        mask_replacements += detection.masked.replacements

        gold = _resolve_gold(detection, document)
        for gold_flag in gold:
            type_gold[gold_flag.flag_type] += 1
        for detection_item in detection.outcome.kept:
            type_detected[detection_item.flag_type] += 1
        for index in strict_hits:
            type_tp[gold[index].flag_type] += 1
            type_recall_hits[gold[index].flag_type] += 1
        for index in span_hits:
            type_span_hits[gold[index].flag_type] += 1

    gold_total = sum(score.gold_total for score in scores)
    detected_total = sum(score.detected_total for score in scores)
    tp = sum(score.tp for score in scores)
    fp = sum(score.fp for score in scores)
    fn = sum(score.fn for score in scores)
    per_type: dict[str, dict[str, float]] = {}
    for flag_type in FLAG_TYPES:
        gold_count = type_gold[flag_type]
        detected_count = type_detected[flag_type]
        hits = type_tp[flag_type]
        per_type[flag_type] = {
            "gold": gold_count,
            "detected": detected_count,
            "tp": hits,
            "recall": round(_ratio(hits, gold_count), 4),
            "precision": round(_ratio(hits, detected_count), 4),
        }
    return EvaluationReport(
        dataset_id=dataset_id,
        dataset_version=version,
        prompt_version=prompt_spec.version,
        model_id=getattr(model_client, "model_id", "unknown"),
        documents=tuple(scores),
        gold_total=gold_total,
        detected_total=detected_total,
        tp=tp,
        fp=fp,
        fn=fn,
        recall=_ratio(gold_total - fn, gold_total),
        precision=_ratio(tp, detected_total),
        recall_span=_ratio(sum(score.recall_span * score.gold_total for score in scores), gold_total),
        precision_span=_ratio(sum(score.precision_span * score.detected_total for score in scores), detected_total),
        per_type=per_type,
        dropped_by_code=dict(sorted(dropped.items())),
        context_seq_flags=context_seq_flags,
        mask_replacements=mask_replacements,
        min_model_confidence=settings.min_model_confidence,
    )


def write_reports(report: EvaluationReport, prefix: str | Path = DEFAULT_REPORT_PREFIX) -> tuple[Path, Path]:
    target = Path(prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    json_path = target.with_suffix(".json")
    markdown_path = target.with_suffix(".md")
    json_path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(report.to_markdown(), encoding="utf-8")
    return markdown_path, json_path


__all__ = [
    "DEFAULT_DATASET",
    "DEFAULT_MODEL_OUTPUT",
    "DEFAULT_REPORT_PREFIX",
    "DocumentScore",
    "EvaluationError",
    "EvaluationReport",
    "PRECISION_FLOOR",
    "RECALL_FLOOR",
    "evaluate",
    "load_dataset",
    "score_document",
    "write_reports",
]

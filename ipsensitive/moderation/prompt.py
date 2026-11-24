"""Loading and rendering the ``ip.sensitive.v1`` prompt.

The prompt is a versioned file, not a string buried in code: the ticket asks for
a prompt file (``prompts/ip.sensitive.v1.md``).  Its front matter declares the
contract (batch size, context segments, flag types, reason limit); this module
validates the contract and rejects a prompt that drifts from the ticket's
specification.  The rendered payload is the only thing a model ever sees.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import DEFAULT_PROMPT_PATH
from .segments import SegmentBatch
from .schema import FLAG_TYPES, REASON_MAX_CHARS

USER_MARKER = "<!-- USER -->"
PAYLOAD_PLACEHOLDER = "{{batch_payload}}"

EXPECTED_BATCH_SIZE = 30
EXPECTED_CONTEXT_SEGMENTS = 1
EXPECTED_FLAG_TYPES = FLAG_TYPES

#: Substrings the system prompt must contain; each maps to a requirement of the
#: ticket, so removing one is a prompt regression rather than a style change.
REQUIRED_SYSTEM_ANCHORS: tuple[str, ...] = (
    "third_party_personal",
    "unpublished_business",
    "unlicensed_brand",
    "宁可多标",
    "逐字复制",
    "[PII:",
    "confidence",
    "reason",
    "不计算",
)


class PromptError(ValueError):
    """The prompt file is missing or violates the ticket's contract."""


def _parse_scalar(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if "," in value:
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


def _parse_document(text: str) -> tuple[dict[str, Any], str, str]:
    """Split ``text`` into front matter, system prompt and user template."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PromptError("prompt file must start with a '---' front matter block")
    try:
        closing = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:  # pragma: no cover - defensive
        raise PromptError("front matter block is not closed") from error

    meta: dict[str, Any] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        if ":" not in line:
            raise PromptError(f"front matter line is not 'key: value': {line!r}")
        key, raw = line.split(":", 1)
        meta[key.strip()] = _parse_scalar(raw)

    body = "\n".join(lines[closing + 1 :])
    if USER_MARKER not in body:
        raise PromptError(f"prompt body must contain the {USER_MARKER} separator")
    system_text, user_template = body.split(USER_MARKER, 1)
    return meta, system_text.strip(), user_template.strip()


@dataclass(frozen=True)
class PromptSpec:
    version: str
    batch_size: int
    context_segments: int
    flag_types: tuple[str, ...]
    reason_max_chars: int
    confidence_min: float
    confidence_max: float
    quote_max_chars: int
    temperature: float
    max_output_tokens: int
    target_only: bool
    system_text: str
    user_template: str
    source_path: Path

    def user_message(self, batch: SegmentBatch) -> str:
        payload = json.dumps(batch.as_payload(self.version), ensure_ascii=False, indent=2)
        rendered = (
            self.user_template.replace(PAYLOAD_PLACEHOLDER, payload)
            .replace("{{prompt_version}}", self.version)
            .replace("{{batch_size}}", str(self.batch_size))
            .replace("{{context_segments}}", str(self.context_segments))
            .replace("{{reason_max_chars}}", str(self.reason_max_chars))
            .replace("{{target_seqs}}", ", ".join(str(seq) for seq in batch.target_seqs))
        )
        if PAYLOAD_PLACEHOLDER in rendered or "{{" in rendered:
            raise PromptError("user template still contains an unreplaced placeholder")
        return rendered

    def messages(self, batch: SegmentBatch) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system_text},
            {"role": "user", "content": self.user_message(batch)},
        ]


def _validate(meta: Mapping[str, Any], system_text: str, user_template: str, source_path: Path) -> PromptSpec:
    missing = [key for key in ("version", "batch_size", "context_segments", "flag_types") if key not in meta]
    if missing:
        raise PromptError(f"front matter is missing {missing}")

    version = str(meta["version"])
    if not version.startswith("ip.sensitive."):
        raise PromptError(f"prompt version must be 'ip.sensitive.<n>', got {version!r}")
    if int(meta["batch_size"]) != EXPECTED_BATCH_SIZE:
        raise PromptError(f"batch_size must be {EXPECTED_BATCH_SIZE}, got {meta['batch_size']!r}")
    if int(meta["context_segments"]) != EXPECTED_CONTEXT_SEGMENTS:
        raise PromptError(f"context_segments must be {EXPECTED_CONTEXT_SEGMENTS}")
    declared_types = meta["flag_types"]
    if isinstance(declared_types, str):
        declared_types = (declared_types,)
    if tuple(declared_types) != EXPECTED_FLAG_TYPES:
        raise PromptError(f"flag_types must be {EXPECTED_FLAG_TYPES}, got {declared_types!r}")
    if int(meta.get("reason_max_chars", REASON_MAX_CHARS)) != REASON_MAX_CHARS:
        raise PromptError(f"reason_max_chars must be {REASON_MAX_CHARS}")
    if int(meta.get("quote_max_chars", 200)) > 200:
        raise PromptError("quote_max_chars must not exceed 200")
    if float(meta.get("confidence_min", 0.0)) != 0.0 or float(meta.get("confidence_max", 1.0)) != 1.0:
        raise PromptError("confidence range must be 0..1")
    if str(meta.get("response_format", "json")) != "json":
        raise PromptError("response_format must be json")
    if not bool(meta.get("target_only", True)):
        raise PromptError("target_only must be true: context segments are reference only")

    for anchor in REQUIRED_SYSTEM_ANCHORS:
        if anchor not in system_text:
            raise PromptError(f"system prompt lost a required instruction: {anchor!r}")
    if PAYLOAD_PLACEHOLDER not in user_template:
        raise PromptError(f"user template must contain {PAYLOAD_PLACEHOLDER}")

    return PromptSpec(
        version=version,
        batch_size=int(meta["batch_size"]),
        context_segments=int(meta["context_segments"]),
        flag_types=tuple(declared_types),
        reason_max_chars=int(meta.get("reason_max_chars", REASON_MAX_CHARS)),
        confidence_min=float(meta.get("confidence_min", 0.0)),
        confidence_max=float(meta.get("confidence_max", 1.0)),
        quote_max_chars=int(meta.get("quote_max_chars", 200)),
        temperature=float(meta.get("temperature", 0.1)),
        max_output_tokens=int(meta.get("max_output_tokens", 4096)),
        target_only=bool(meta.get("target_only", True)),
        system_text=system_text,
        user_template=user_template,
        source_path=source_path,
    )


def load_prompt(path: str | Path = DEFAULT_PROMPT_PATH) -> PromptSpec:
    source = Path(path)
    if not source.is_file():
        raise PromptError(f"prompt file not found: {source}")
    meta, system_text, user_template = _parse_document(source.read_text(encoding="utf-8"))
    return _validate(meta, system_text, user_template, source)


__all__ = [
    "EXPECTED_BATCH_SIZE",
    "EXPECTED_CONTEXT_SEGMENTS",
    "PAYLOAD_PLACEHOLDER",
    "PromptError",
    "PromptSpec",
    "REQUIRED_SYSTEM_ANCHORS",
    "USER_MARKER",
    "load_prompt",
]

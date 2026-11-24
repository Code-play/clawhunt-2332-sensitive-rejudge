"""Validation of the model's JSON answer.

The prompt asks for ``{"flags": [...]}``.  Models are fallible: they wrap the
object in code fences, add prose, invent fields, exceed the reason limit or emit
a confidence outside 0..1.  Every entry is validated individually so one broken
entry never discards a whole batch; rejected entries are counted and reported
instead of being silently dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

FlagType = Literal["third_party_personal", "unpublished_business", "unlicensed_brand", "other"]

FLAG_TYPES: tuple[str, ...] = ("third_party_personal", "unpublished_business", "unlicensed_brand", "other")
REASON_MAX_CHARS = 40
QUOTE_MAX_CHARS = 200
MAX_FLAGS_PER_BATCH = 300

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


class ModelFlag(BaseModel):
    """One flag exactly as the prompt specifies it."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    seq: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=QUOTE_MAX_CHARS)
    flag_type: FlagType
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)
    confidence: float = Field(ge=0.0, le=1.0)


class ModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flags: list[ModelFlag] = Field(default_factory=list)


@dataclass(frozen=True)
class RejectedFlag:
    """One entry the pipeline refused to use, with a machine-readable code."""

    index: int
    code: str
    detail: str = ""


@dataclass(frozen=True)
class ParsedBatch:
    flags: tuple[ModelFlag, ...] = ()
    rejected: tuple[RejectedFlag, ...] = ()
    raw_text: str = ""

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


@dataclass(frozen=True)
class ModelFlagHit:
    """A validated flag plus the batch that produced it (merge input)."""

    seq: int
    quote: str
    flag_type: str
    reason: str
    confidence: float
    batch_index: int
    batch_target_seqs: tuple[int, ...] = field(default_factory=tuple)


def _extract_json(text: str) -> str | None:
    """Return the first JSON object in ``text`` (fences and prose tolerated)."""

    stripped = text.strip()
    if not stripped:
        return None
    fenced = _FENCE_RE.search(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : index + 1]
    return None


def _error_code(validation_error: ValidationError) -> str:
    errors = validation_error.errors()
    if not errors:
        return "invalid_flag"
    first = errors[0]
    error_type = str(first.get("type", ""))
    location = tuple(first.get("loc", ()))
    if error_type == "extra_forbidden":
        return "unexpected_field"
    if error_type == "missing":
        return "missing_field"
    if error_type == "literal_error" and "flag_type" in location:
        return "bad_flag_type"
    if error_type in {"string_too_long"} and "reason" in location:
        return "reason_too_long"
    if error_type in {"string_too_long"} and "quote" in location:
        return "bad_quote"
    if error_type in {"string_too_short", "string_type"} and "quote" in location:
        return "bad_quote"
    if error_type in {"greater_than_equal", "less_than_equal", "float_type"} and "confidence" in location:
        return "confidence_out_of_range"
    if error_type in {"int_type", "int_parsing", "greater_than_equal"} and "seq" in location:
        return "bad_seq"
    return "invalid_flag"


def parse_model_output(
    raw_text: str,
    *,
    batch_index: int = 0,
    batch_target_seqs: tuple[int, ...] = (),
    reason_max_chars: int = REASON_MAX_CHARS,
) -> ParsedBatch:
    """Parse and validate one raw model answer.

    ``reason_max_chars`` comes from the prompt file so a stricter declared limit
    is enforced before the database does.
    """

    if raw_text is None or not str(raw_text).strip():
        return ParsedBatch(rejected=(RejectedFlag(index=-1, code="empty_response"),), raw_text=raw_text or "")

    candidate = _extract_json(raw_text)
    if candidate is None:
        return ParsedBatch(rejected=(RejectedFlag(index=-1, code="invalid_json"),), raw_text=raw_text)

    try:
        document = json.loads(candidate)
    except json.JSONDecodeError as error:
        return ParsedBatch(
            rejected=(RejectedFlag(index=-1, code="invalid_json", detail=str(error)[:120]),),
            raw_text=raw_text,
        )

    if not isinstance(document, dict):
        return ParsedBatch(rejected=(RejectedFlag(index=-1, code="not_a_json_object"),), raw_text=raw_text)

    flags_raw = document.get("flags")
    if flags_raw is None:
        return ParsedBatch(rejected=(RejectedFlag(index=-1, code="missing_flags"),), raw_text=raw_text)
    if not isinstance(flags_raw, list):
        return ParsedBatch(rejected=(RejectedFlag(index=-1, code="flags_not_a_list"),), raw_text=raw_text)
    if len(flags_raw) > MAX_FLAGS_PER_BATCH:
        return ParsedBatch(
            rejected=(
                RejectedFlag(index=-1, code="too_many_flags", detail=f"{len(flags_raw)} > {MAX_FLAGS_PER_BATCH}"),
            ),
            raw_text=raw_text,
        )

    accepted: list[ModelFlag] = []
    rejected: list[RejectedFlag] = []
    for index, entry in enumerate(flags_raw):
        if not isinstance(entry, dict):
            rejected.append(RejectedFlag(index=index, code="flag_not_an_object"))
            continue
        if "reason" in entry and isinstance(entry["reason"], str) and len(entry["reason"]) > reason_max_chars:
            rejected.append(
                RejectedFlag(index=index, code="reason_too_long", detail=f"{len(entry['reason'])} chars")
            )
            continue
        try:
            accepted.append(ModelFlag.model_validate(entry))
        except ValidationError as error:
            rejected.append(RejectedFlag(index=index, code=_error_code(error), detail=str(error.errors()[0].get("msg", ""))[:120]))

    return ParsedBatch(
        flags=tuple(accepted),
        rejected=tuple(rejected),
        raw_text=raw_text,
    )


__all__ = [
    "FLAG_TYPES",
    "FlagType",
    "MAX_FLAGS_PER_BATCH",
    "ModelFlag",
    "ModelFlagHit",
    "ModelPayload",
    "ParsedBatch",
    "QUOTE_MAX_CHARS",
    "REASON_MAX_CHARS",
    "RejectedFlag",
    "parse_model_output",
]

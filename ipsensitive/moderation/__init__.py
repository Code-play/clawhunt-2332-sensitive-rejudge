"""Sensitive-content re-review for the Personal IP content assistant."""

from .merge import Candidate, Dropped, MergeOutcome, merge_fragments
from .model_client import ModelClient, ModelError, ModelUnavailable
from .pipeline import ReviewRequest, ReviewResult, SensitiveReviewer, build_request
from .prompt import PromptSpec, load_prompt
from .rules import RegexRuleDetector, RuleHit, RuleSource
from .schema import FLAG_TYPES, ModelFlag, ModelFlagHit, parse_model_output
from .segments import Segment, SegmentBatch, build_batches, normalise_segments

__all__ = [
    "Candidate",
    "Dropped",
    "FLAG_TYPES",
    "MergeOutcome",
    "ModelClient",
    "ModelError",
    "ModelFlag",
    "ModelFlagHit",
    "ModelUnavailable",
    "PromptSpec",
    "RegexRuleDetector",
    "ReviewRequest",
    "ReviewResult",
    "RuleHit",
    "RuleSource",
    "Segment",
    "SegmentBatch",
    "SensitiveReviewer",
    "build_batches",
    "build_request",
    "load_prompt",
    "merge_fragments",
    "normalise_segments",
    "parse_model_output",
]

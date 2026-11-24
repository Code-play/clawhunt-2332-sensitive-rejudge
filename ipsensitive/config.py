"""Environment-only configuration.

The platform convention forbids configuration files with secrets: every value is
read from the process environment.  ``load_settings`` is the single place that
touches ``os.environ``; everything else receives a ``Settings`` instance.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "ip.sensitive.v1.md"
DEFAULT_DATABASE_URL = "postgresql+psycopg://ipsensitive:ipsensitive_local_dev@127.0.0.1:55432/ipsensitive"

MODEL_MODES = ("recorded", "http")
ON_MODEL_ERROR_POLICIES = ("fail", "rules_only")


@dataclass(frozen=True)
class Settings:
    """Runtime configuration.

    ``model_mode`` selects the model transport: ``recorded`` replays a fixture
    (tests, offline evaluation and the demo), ``http`` calls the gateway at
    ``model_base_url``.  No SDK is used for the gateway call.
    """

    database_url: str = DEFAULT_DATABASE_URL
    prompt_path: Path = DEFAULT_PROMPT_PATH
    model_mode: str = "recorded"
    model_replay_path: Path | None = None
    model_base_url: str | None = None
    model_api_key: str | None = None
    model_name: str | None = None
    model_timeout_seconds: float = 30.0
    on_model_error: str = "fail"
    #: Ticket requirement: model-only hits below this confidence are dropped.
    min_model_confidence: float = 0.5

    def __post_init__(self) -> None:
        if self.model_mode not in MODEL_MODES:
            raise ValueError(f"model_mode must be one of {MODEL_MODES}, got {self.model_mode!r}")
        if self.on_model_error not in ON_MODEL_ERROR_POLICIES:
            raise ValueError(
                f"on_model_error must be one of {ON_MODEL_ERROR_POLICIES}, got {self.on_model_error!r}"
            )
        if not 0.0 <= self.min_model_confidence <= 1.0:
            raise ValueError("min_model_confidence must be inside [0, 1]")
        if self.model_mode == "http" and not self.model_base_url:
            raise ValueError("model_mode=http requires IP_SENSITIVE_MODEL_BASE_URL")


def _float(source: Mapping[str, str], name: str, default: float) -> float:
    raw = source.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build ``Settings`` from ``env`` (the process environment by default)."""

    source = os.environ if env is None else env
    replay = source.get("IP_SENSITIVE_MODEL_REPLAY")
    return Settings(
        database_url=source.get("DATABASE_URL") or DEFAULT_DATABASE_URL,
        prompt_path=Path(source.get("IP_SENSITIVE_PROMPT_PATH") or DEFAULT_PROMPT_PATH),
        model_mode=source.get("IP_SENSITIVE_MODEL_MODE") or "recorded",
        model_replay_path=Path(replay) if replay else None,
        model_base_url=source.get("IP_SENSITIVE_MODEL_BASE_URL") or None,
        model_api_key=source.get("IP_SENSITIVE_MODEL_API_KEY") or None,
        model_name=source.get("IP_SENSITIVE_MODEL_NAME") or None,
        model_timeout_seconds=_float(source, "IP_SENSITIVE_MODEL_TIMEOUT_SECONDS", 30.0),
        on_model_error=source.get("IP_SENSITIVE_ON_MODEL_ERROR") or "fail",
        min_model_confidence=_float(source, "IP_SENSITIVE_MIN_MODEL_CONFIDENCE", 0.5),
    )

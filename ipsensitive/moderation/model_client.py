"""Model transports.

No vendor SDK is used: the gateway is an OpenAI-compatible ``/chat/completions``
endpoint called with the standard library, and its URL/key/model come from the
environment.  That keeps third-party code (and therefore any risk of data leaving
the client's own cloud account through an SDK) out of the pipeline.

``recorded`` mode replays a JSON fixture of raw model answers.  It is what the
tests, the offline evaluation and the demo use; the recorded answers are the
fictional model output shipped with the ticket's evaluation set.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..config import PROJECT_ROOT, Settings
from .prompt import PromptSpec
from .segments import SegmentBatch


class ModelError(RuntimeError):
    """Base class for model transport failures."""


class ModelUnavailable(ModelError):
    """The model could not be reached or returned an unusable answer."""


class ModelReplayError(ModelError):
    """Recorded mode has no answer for the requested batch."""


@runtime_checkable
class ModelClient(Protocol):
    model_id: str

    def review(self, *, document_key: str, batch: SegmentBatch) -> str:
        """Return the raw assistant answer for one batch."""


@dataclass
class RecordedModelClient:
    """Replay a fixture of raw answers keyed by ``<document>:<batch>``."""

    responses: Mapping[str, str]
    model_id: str = "recorded-model-v1"

    def __post_init__(self) -> None:
        # A fixture may store the answer as JSON text or as an object; the
        # pipeline always receives the raw text a gateway would return.
        normalised: dict[str, str] = {}
        for key, value in self.responses.items():
            if isinstance(value, str):
                normalised[key] = value
            elif isinstance(value, Mapping) and "text" in value:
                normalised[key] = str(value["text"])
            elif isinstance(value, Mapping):
                normalised[key] = json.dumps(value, ensure_ascii=False)
            else:  # pragma: no cover - defensive
                raise ValueError(f"replay entry {key!r} must be a string or an object")
        self.responses = normalised

    @classmethod
    def from_file(cls, path: str | Path) -> "RecordedModelClient":
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(f"model replay fixture not found: {source}")
        document = json.loads(source.read_text(encoding="utf-8"))
        responses = document.get("responses") or {}
        return cls(responses=dict(responses), model_id=str(document.get("model_id") or "recorded-model-v1"))

    def review(self, *, document_key: str, batch: SegmentBatch) -> str:
        for key in (f"{document_key}:{batch.index}", f"*:{batch.index}", f"{document_key}:*", "*:*"):
            if key in self.responses:
                return self.responses[key]
        raise ModelReplayError(f"no recorded model answer for {document_key} batch {batch.index}")


#: ``transport(url, headers, body, timeout) -> (status, text)``
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, str]]


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is operator-configured
            return int(response.status), response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return int(error.code), error.read().decode("utf-8", errors="replace")


@dataclass
class HttpChatModelClient:
    """OpenAI-compatible chat-completions client (stdlib HTTP only)."""

    base_url: str
    api_key: str
    model_name: str
    prompt: PromptSpec
    timeout_seconds: float = 30.0
    attempts: int = 2
    transport: Transport = _urllib_transport

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if not self.model_name:
            raise ValueError("model_name is required in http mode")

    @property
    def model_id(self) -> str:  # type: ignore[override]
        return self.model_name

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _body(self, batch: SegmentBatch) -> bytes:
        payload: dict[str, Any] = {
            "model": self.model_name,
            "temperature": self.prompt.temperature,
            "max_tokens": self.prompt.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": self.prompt.messages(batch),
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def review(self, *, document_key: str, batch: SegmentBatch) -> str:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = self._body(batch)
        last_error: str = "no attempt made"
        for attempt in range(1, max(1, self.attempts) + 1):
            status, text = self.transport(self.endpoint, headers, body, self.timeout_seconds)
            if status == 200:
                return self._content_of(text)
            last_error = f"HTTP {status}: {text[:200]}"
            if 400 <= status < 500 and status not in {408, 429}:
                break  # a request the gateway will keep rejecting
            if attempt < self.attempts:
                time.sleep(0.4 * attempt)
        raise ModelUnavailable(f"model call failed ({last_error})")

    @staticmethod
    def _content_of(text: str) -> str:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ModelUnavailable(f"gateway returned non-JSON: {error}") from error
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:  # pragma: no cover - defensive
            raise ModelUnavailable("gateway response has no choices[0].message.content") from error
        if not isinstance(content, str) or not content.strip():
            raise ModelUnavailable("gateway returned an empty message")
        return content


def build_model_client(settings: Settings, prompt: PromptSpec) -> ModelClient:
    """Build the transport selected by ``IP_SENSITIVE_MODEL_MODE``."""

    if settings.model_mode == "recorded":
        path = settings.model_replay_path or (PROJECT_ROOT / "evalset" / "model_output_v1.json")
        return RecordedModelClient.from_file(path)
    if not settings.model_base_url:  # pragma: no cover - guarded by Settings
        raise ModelUnavailable("http mode requires IP_SENSITIVE_MODEL_BASE_URL")
    return HttpChatModelClient(
        base_url=settings.model_base_url,
        api_key=settings.model_api_key or "",
        model_name=settings.model_name or "",
        prompt=prompt,
        timeout_seconds=settings.model_timeout_seconds,
    )


__all__ = [
    "HttpChatModelClient",
    "ModelClient",
    "ModelError",
    "ModelReplayError",
    "ModelUnavailable",
    "RecordedModelClient",
    "Transport",
    "build_model_client",
]

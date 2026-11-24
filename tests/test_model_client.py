"""Model transports: recorded replay and the stdlib HTTP gateway client."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from ipsensitive.config import Settings
from ipsensitive.moderation.model_client import (
    HttpChatModelClient,
    ModelReplayError,
    ModelUnavailable,
    RecordedModelClient,
    build_model_client,
)
from ipsensitive.moderation.segments import Segment, build_batches


def _batch(text: str = "唯一一段。", seq: int = 0):
    return build_batches([Segment(seq=seq, text=text)])[0]


def test_recorded_client_reads_the_fixture(model_output_path):
    client = RecordedModelClient.from_file(model_output_path)
    assert client.model_id == "fictional-ip-sensitive-model-v1"
    raw = client.review(document_key="c-101", batch=_batch())
    assert json.loads(raw)["flags"]


def test_recorded_client_supports_wildcards_and_fails_closed():
    client = RecordedModelClient({"*:0": '{"flags": []}'})
    assert client.review(document_key="anything", batch=_batch()) == '{"flags": []}'
    with pytest.raises(ModelReplayError):
        RecordedModelClient({}).review(document_key="anything", batch=_batch())


@dataclass
class FakeTransport:
    statuses: list[int]
    body: str
    calls: list[tuple[str, dict[str, str], bytes, float]] | None = None

    def __post_init__(self) -> None:
        self.calls = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, str]:
        assert self.calls is not None
        self.calls.append((url, headers, body, timeout))
        status = self.statuses.pop(0) if self.statuses else 200
        return status, self.body


def _openai_body(content: str) -> str:
    return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False)


def _client(transport, prompt, **overrides) -> HttpChatModelClient:
    defaults = dict(
        base_url="http://gateway.local/v1",
        api_key="test-key",
        model_name="fictional-model",
        prompt=prompt,
        timeout_seconds=5.0,
        attempts=2,
        transport=transport,
    )
    defaults.update(overrides)
    return HttpChatModelClient(**defaults)


def test_http_client_builds_the_contract_request(prompt):
    transport = FakeTransport([200], _openai_body('{"flags": []}'))
    client = _client(transport, prompt)
    assert client.endpoint == "http://gateway.local/v1/chat/completions"
    assert client.model_id == "fictional-model"

    assert client.review(document_key="doc", batch=_batch("内部数据")) == '{"flags": []}'
    url, headers, body, timeout = transport.calls[0]
    assert url == "http://gateway.local/v1/chat/completions"
    assert headers["Authorization"] == "Bearer test-key"
    assert headers["Content-Type"] == "application/json"
    assert timeout == 5.0
    payload = json.loads(body)
    assert payload["model"] == "fictional-model"
    assert payload["temperature"] == prompt.temperature
    assert payload["max_tokens"] == prompt.max_output_tokens
    assert payload["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert "内部数据" in payload["messages"][1]["content"]


def test_http_client_retries_a_server_error_once(prompt):
    transport = FakeTransport([500, 200], _openai_body('{"flags": []}'))
    client = _client(transport, prompt)
    assert client.review(document_key="doc", batch=_batch()) == '{"flags": []}'
    assert len(transport.calls) == 2


def test_http_client_does_not_retry_a_rejected_request(prompt):
    transport = FakeTransport([400], "bad request")
    client = _client(transport, prompt)
    with pytest.raises(ModelUnavailable):
        client.review(document_key="doc", batch=_batch())
    assert len(transport.calls) == 1


@pytest.mark.parametrize("body", ["not json", json.dumps({"choices": []}), json.dumps({"choices": [{"message": {"content": ""}}]})])
def test_http_client_rejects_unusable_gateway_answers(prompt, body):
    transport = FakeTransport([200], body)
    with pytest.raises(ModelUnavailable):
        _client(transport, prompt).review(document_key="doc", batch=_batch())


def test_http_client_requires_a_model_name(prompt):
    with pytest.raises(ValueError):
        _client(FakeTransport([200], "{}"), prompt, model_name="")


def test_build_model_client_from_settings(settings, prompt, model_output_path):
    recorded = build_model_client(settings, prompt)
    assert isinstance(recorded, RecordedModelClient)
    assert recorded.model_id == "fictional-ip-sensitive-model-v1"

    http_settings = Settings(
        model_mode="http",
        model_base_url="http://gateway.local/v1",
        model_api_key="key",
        model_name="fictional-model",
    )
    http_client = build_model_client(http_settings, prompt)
    assert isinstance(http_client, HttpChatModelClient)
    assert http_client.endpoint == "http://gateway.local/v1/chat/completions"

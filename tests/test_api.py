"""HTTP API: run a re-review, work the queue, tenant isolation."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from ipsensitive.api.main import create_app
from ipsensitive.config import Settings


def _body(tenant, *, text: str = "内部会议的结论是砍预算。", **overrides):
    body = {
        "org_id": str(tenant["org_id"]),
        "site_id": str(tenant["site_id"]),
        "segments": [{"seq": 0, "text": text}],
    }
    body.update(overrides)
    return body


def _model(quote: str = "内部会议的结论", *, seq: int = 0, confidence: float = 0.9):
    return {
        "*:0": {
            "flags": [
                {
                    "seq": seq,
                    "quote": quote,
                    "flag_type": "unpublished_business",
                    "reason": "未公开会议内容",
                    "confidence": confidence,
                }
            ]
        }
    }


def test_healthz_reports_the_prompt_contract(api_client_factory, make_client):
    client = api_client_factory(make_client({}))
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["prompt_version"] == "ip.sensitive.v1"
    assert body["prompt_batch_size"] == 30
    assert body["model_mode"] == "recorded"
    assert body["min_model_confidence"] == 0.5


def test_review_run_writes_the_queue_and_returns_the_flags(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client(_model()))
    content_id = str(uuid.uuid4())
    response = client.post(f"/v1/ip-drafts/{content_id}/sensitive-review", json=_body(tenant))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["prompt_version"] == "ip.sensitive.v1"
    assert body["model_status"] == "ok" and body["degraded"] is False
    assert body["written"] == 1 and body["kept"] == 1
    assert len(body["flags"]) == 1
    flag = body["flags"][0]
    assert flag["content_id"] == content_id
    assert flag["detector"] == "rule+model"
    assert flag["disposition"] == "pending"
    assert flag["flag_type"] == "unpublished_business"
    assert flag["quote"] == "内部会议的结论"
    assert flag["quote_end"] > flag["quote_start"]
    assert "amount" not in flag and "price" not in flag


def test_queue_listing_and_filters(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client(_model()))
    content_id = str(uuid.uuid4())
    assert client.post(f"/v1/ip-drafts/{content_id}/sensitive-review", json=_body(tenant)).status_code == 201

    listing = client.get(
        f"/v1/ip-drafts/{content_id}/sensitive-flags",
        params={"org_id": str(tenant["org_id"]), "site_id": str(tenant["site_id"])},
    )
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["total"] == 1 and len(payload["items"]) == 1

    pending = client.get(
        f"/v1/ip-drafts/{content_id}/sensitive-flags",
        params={
            "org_id": str(tenant["org_id"]),
            "site_id": str(tenant["site_id"]),
            "disposition": "pending",
        },
    )
    assert pending.json()["total"] == 1
    empty = client.get(
        f"/v1/ip-drafts/{content_id}/sensitive-flags",
        params={
            "org_id": str(tenant["org_id"]),
            "site_id": str(tenant["site_id"]),
            "disposition": "accepted",
        },
    )
    assert empty.json()["total"] == 0

    bad_limit = client.get(
        f"/v1/ip-drafts/{content_id}/sensitive-flags",
        params={"org_id": str(tenant["org_id"]), "site_id": str(tenant["site_id"]), "limit": 0},
    )
    assert bad_limit.status_code == 422


def test_flag_detail_disposition_and_delete(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client(_model()))
    content_id = str(uuid.uuid4())
    created = client.post(f"/v1/ip-drafts/{content_id}/sensitive-review", json=_body(tenant)).json()
    flag_id = created["flags"][0]["id"]
    params = {"org_id": str(tenant["org_id"]), "site_id": str(tenant["site_id"])}

    detail = client.get(f"/v1/sensitive-flags/{flag_id}", params=params)
    assert detail.status_code == 200 and detail.json()["id"] == flag_id

    staff = str(uuid.uuid4())
    reviewed = client.patch(
        f"/v1/sensitive-flags/{flag_id}",
        json={
            "org_id": str(tenant["org_id"]),
            "site_id": str(tenant["site_id"]),
            "disposition": "accepted",
            "reviewed_by": staff,
            "review_note": "确认需要修改",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["disposition"] == "accepted"
    assert reviewed.json()["reviewed_by"] == staff
    assert reviewed.json()["reviewed_at"] is not None

    back_to_pending = client.patch(
        f"/v1/sensitive-flags/{flag_id}",
        json={
            "org_id": str(tenant["org_id"]),
            "site_id": str(tenant["site_id"]),
            "disposition": "pending",
            "reviewed_by": staff,
        },
    )
    assert back_to_pending.status_code == 422

    deleted = client.delete(f"/v1/sensitive-flags/{flag_id}", params=params)
    assert deleted.status_code == 204
    assert client.get(f"/v1/sensitive-flags/{flag_id}", params=params).status_code == 404


def test_cross_tenant_access_is_a_plain_404(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client(_model()))
    content_id = str(uuid.uuid4())
    created = client.post(f"/v1/ip-drafts/{content_id}/sensitive-review", json=_body(tenant)).json()
    flag_id = created["flags"][0]["id"]
    other = {"org_id": str(uuid.uuid4()), "site_id": str(uuid.uuid4())}

    assert client.get(f"/v1/sensitive-flags/{flag_id}", params=other).status_code == 404
    assert (
        client.patch(
            f"/v1/sensitive-flags/{flag_id}",
            json={**other, "disposition": "dismissed", "reviewed_by": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    assert client.delete(f"/v1/sensitive-flags/{flag_id}", params=other).status_code == 404
    listing = client.get(
        f"/v1/ip-drafts/{content_id}/sensitive-flags",
        params={"org_id": other["org_id"], "site_id": other["site_id"]},
    )
    assert listing.status_code == 200 and listing.json()["total"] == 0


def test_invalid_payloads_are_rejected(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client(_model()))
    content_id = str(uuid.uuid4())
    empty = client.post(
        f"/v1/ip-drafts/{content_id}/sensitive-review",
        json={**_body(tenant), "segments": []},
    )
    assert empty.status_code == 422
    duplicate = client.post(
        f"/v1/ip-drafts/{content_id}/sensitive-review",
        json={**_body(tenant), "segments": [{"seq": 0, "text": "a"}, {"seq": 0, "text": "b"}]},
    )
    assert duplicate.status_code == 422
    unknown_rule_type = client.post(
        f"/v1/ip-drafts/{content_id}/sensitive-review",
        json={**_body(tenant), "rule_hits": [{"seq": 0, "quote": "a", "flag_type": "gossip", "rule_code": "x"}]},
    )
    assert unknown_rule_type.status_code == 422


def test_model_failure_writes_nothing(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client({}))
    content_id = str(uuid.uuid4())
    response = client.post(f"/v1/ip-drafts/{content_id}/sensitive-review", json=_body(tenant))
    assert response.status_code == 503
    assert "model unavailable" in response.json()["detail"]
    listing = client.get(
        f"/v1/ip-drafts/{content_id}/sensitive-flags",
        params={"org_id": str(tenant["org_id"]), "site_id": str(tenant["site_id"])},
    )
    assert listing.json()["total"] == 0


def test_configurable_rules_only_policy_reports_a_degraded_run(api_client_factory, make_client, tenant, prompt, settings):
    degraded_settings = Settings(
        database_url=settings.database_url,
        model_mode="recorded",
        on_model_error="rules_only",
        min_model_confidence=0.5,
    )
    app = create_app(settings=degraded_settings, model_client=make_client({}), prompt=prompt)
    try:
        client = TestClient(app)
        content_id = str(uuid.uuid4())
        response = client.post(f"/v1/ip-drafts/{content_id}/sensitive-review", json=_body(tenant))
        assert response.status_code == 201
        body = response.json()
        assert body["degraded"] is True
        assert body["model_status"] == "unavailable"
        assert [flag["detector"] for flag in body["flags"]] == ["rule"]
    finally:
        app.state.engine.dispose()


def test_rule_hits_can_be_supplied_by_the_upstream_stage(api_client_factory, make_client, tenant):
    client = api_client_factory(make_client(_model(quote="没有拿到授权就拿去用了")))
    content_id = str(uuid.uuid4())
    response = client.post(
        f"/v1/ip-drafts/{content_id}/sensitive-review",
        json={
            **_body(tenant, text="这一段提到了别人的商标，没有拿到授权就拿去用了。"),
            "rule_hits": [
                {
                    "seq": 0,
                    "quote": "没有拿到授权",
                    "flag_type": "unlicensed_brand",
                    "rule_code": "brand.upstream",
                    "reason": "上游规则命中",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    flag = response.json()["flags"][0]
    assert flag["detector"] == "rule+model"
    assert flag["rule_code"] == "brand.upstream"
    assert flag["flag_type"] == "unlicensed_brand"

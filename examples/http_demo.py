"""End-to-end demo of the sensitive re-review over the HTTP API.

Uses the recorded fictional model answers, so it needs no model gateway: it
proves the masking gate, the rule stage, the merge, the review queue and the
staff disposition path.  Requires the database to be migrated
(``alembic upgrade head``).

Run:  python examples/http_demo.py
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ipsensitive.api.main import create_app  # noqa: E402
from ipsensitive.config import DEFAULT_DATABASE_URL, Settings  # noqa: E402
from ipsensitive.moderation.model_client import RecordedModelClient  # noqa: E402

# A fictional draft (the c-102 evaluation document).  The phone number is real
# looking on purpose so the masking gate has something to remove.
DRAFT = [
    {"seq": 0, "text": "上周和三家供应商吃饭，聊到明年招标的底价。"},
    {"seq": 1, "text": "A 厂的采购经理姓吴，手机号 13812345678，他私下给我的报价比公开低 8%。"},
    {"seq": 2, "text": "这条供应链的小道消息，我不好说得太细。"},
    {"seq": 3, "text": "另外，某竞品公司还没公开的融资消息，是投资人告诉我的。"},
    {"seq": 4, "text": "内容发出去之前，我会再确认一遍。"},
]


def main() -> int:
    fixture = json.loads((ROOT / "evalset" / "model_output_v1.json").read_text(encoding="utf-8"))
    recorded_answer = fixture["responses"]["c-102:0"]
    model_client = RecordedModelClient({"*:0": recorded_answer}, model_id=fixture["model_id"])

    settings = Settings(
        database_url=os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL,
        model_mode="recorded",
        model_replay_path=ROOT / "evalset" / "model_output_v1.json",
    )
    app = create_app(settings=settings, model_client=model_client)
    tenant = {"org_id": str(uuid.uuid4()), "site_id": str(uuid.uuid4())}
    content_id = str(uuid.uuid4())

    with TestClient(app) as api:
        health = api.get("/healthz").json()
        print(f"healthz: prompt={health['prompt_version']} batch={health['prompt_batch_size']} mode={health['model_mode']}")

        response = api.post(
            f"/v1/ip-drafts/{content_id}/sensitive-review",
            json={**tenant, "segments": DRAFT, "created_by": str(uuid.uuid4())},
        )
        response.raise_for_status()
        run = response.json()
        print(
            "run: written={written} kept={kept} rule_hits={rule_hits} model_flags={model_flags} "
            "model_rejected={model_rejected} mask_replacements={mask_replacements}".format(**run)
        )
        print(f"run: dropped_by_code={run['dropped_by_code']} context_seq_flags={run['context_seq_flags']}")

        print("flags (masked quotes only):")
        for flag in run["flags"]:
            print(
                f"  seq={flag['segment_seq']} [{flag['quote_start']}:{flag['quote_end']}] "
                f"{flag['detector']:<10} {flag['flag_type']:<20} conf={flag['confidence']:.2f} "
                f"quote={flag['quote']!r}"
            )

        queue = api.get(f"/v1/ip-drafts/{content_id}/sensitive-flags", params=tenant).json()
        print(f"queue: total={queue['total']} pending={sum(1 for item in queue['items'] if item['disposition'] == 'pending')}")

        first = run["flags"][0]
        reviewed = api.patch(
            f"/v1/sensitive-flags/{first['id']}",
            json={
                **tenant,
                "disposition": "accepted",
                "reviewed_by": str(uuid.uuid4()),
                "review_note": "demo: 需要脱敏改写",
            },
        )
        reviewed.raise_for_status()
        body = reviewed.json()
        print(f"review: {body['id']} -> {body['disposition']} by {body['reviewed_by']}")

        queue_after = api.get(
            f"/v1/ip-drafts/{content_id}/sensitive-flags",
            params={**tenant, "disposition": "pending"},
        ).json()
        print(f"queue after review: pending={queue_after['total']}")

        deleted = api.delete(f"/v1/sensitive-flags/{first['id']}", params=tenant)
        print(f"delete: status={deleted.status_code}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

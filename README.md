# IP sensitive re-review - S5-IP-024

Model re-review of sensitive content for the Personal IP content assistant
(member space platform, Stage v AI assisted tools).  The rule stage runs first;
the model then adds what the rules missed and re-judges their hits.  The merged
result is an L1 draft in the staff review queue (`ip_sensitive_flags`,
`disposition = 'pending'`) - nothing is ever sent outwards from this module.

## What is in this repository

| Ticket deliverable | Location |
|---|---|
| Prompt file `ip.sensitive.v1` | `prompts/ip.sensitive.v1.md` (front matter declares batch size 30, one context segment per side, four flag types, 40-character reasons) |
| Model call + merge code | `ipsensitive/moderation/` (`masking`, `segments`, `prompt`, `model_client`, `rules`, `schema`, `merge`, `pipeline`) |
| Persistence and CRUD | `ipsensitive/models/`, `ipsensitive/repo.py`, `ipsensitive/api/`, `migrations/` |
| Evaluation report (recall, precision) | `reports/evaluation_v1.md` (generated; JSON alongside it) |
| Evaluation set and recorded model answers | `evalset/eval_set_v1.json`, `evalset/model_output_v1.json` (all fictional) |
| Unit and acceptance tests | `tests/` |
| Diagnostics per acceptance criterion | `VERIFICATION.md` |

## Quick start

```bash
docker compose up -d db            # PostgreSQL 16 on 127.0.0.1:55432
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

export DATABASE_URL=postgresql+psycopg://ipsensitive:ipsensitive_local_dev@127.0.0.1:55432/ipsensitive
alembic upgrade head
uvicorn --factory ipsensitive.api.main:create_app --port 8000

python -m ipsensitive.eval_cli     # regenerates reports/evaluation_v1.{md,json}
python -m pytest                   # full suite against PostgreSQL 16
python examples/http_demo.py       # end-to-end run over the HTTP API
```

Configuration is environment-only; see `.env.example` and `ipsensitive/config.py`.
The service has no configuration file and no vendor SDK.

### Model gateway

`IP_SENSITIVE_MODEL_MODE=recorded` (default) replays `evalset/model_output_v1.json`;
that is what the tests, the evaluation and the demo use.  With
`IP_SENSITIVE_MODEL_MODE=http` the client posts to
`$IP_SENSITIVE_MODEL_BASE_URL/chat/completions` (OpenAI-compatible,
`response_format: json_object`) using the standard library - no SDK, so no
third-party code can ship data out of the client's own cloud account.
`IP_SENSITIVE_ON_MODEL_ERROR` decides what a model outage does: `fail` (default)
aborts the run and writes nothing, `rules_only` keeps the rule-stage flags and
marks the run degraded.

## How a run works

1. **Unified masking gate** (`moderation/masking.py`).  Direct identifiers
   (mobile numbers, e-mail addresses, 16-19 digit card numbers, 18-character ID
   numbers, optional name roster) become typed placeholders such as
   `[PII:PHONE]`.  The model only ever sees masked text, and the masked text is
   the reference for quote verification.
2. **Batching** (`moderation/segments.py`).  Segments (paragraphs, `seq` ordered)
   go to the model 30 at a time, with exactly one segment of context on each
   side.  The payload marks every segment `target` / `context_before` /
   `context_after` and repeats `target_seqs`.
3. **Rule stage** (`moderation/rules.py`).  The upstream rule engine contract is
   `detect(segments) -> list[RuleHit]` (`seq`, `quote`, `flag_type`,
   `rule_code`, `reason`).  This ticket ships a deterministic `RegexRuleDetector`
   stub plus the option to pass the real rule hits through the API
   (`rule_hits`); see *Integration points*.
4. **Model answer validation** (`moderation/schema.py`).  The raw answer may be
   plain JSON, code-fenced or surrounded by prose; every flag entry is validated
   on its own (allowed type, reason <= 40 characters, confidence 0..1, no extra
   fields) and rejected entries are counted, never silently dropped.
5. **Merge** (`moderation/merge.py`) - the ticket's contract, literally:
   - a fragment hit by both rule and model becomes **one** row with
     `detector = 'rule+model'`;
   - a **model-only** hit with `confidence < 0.5` is dropped;
   - a `quote` that is not found verbatim in the masked segment is dropped;
   - everything else is kept: 宁可多标、不能漏标 (over-flag rather than miss).
6. **Persistence** (`repo.py`, `models/flags.py`).  Rows carry the mask-only
   quote and its offsets, the merged type, detector, confidence, reason, rule
   code, prompt version and `disposition = 'pending'`.  One unique expression
   index (`content_id`, `segment_seq`, `md5(quote)`, `WHERE deleted_at IS NULL`)
   makes a second live row for the same fragment impossible, so re-running the
   pipeline is idempotent.

The same detection path is used by the API and by the offline evaluation
(`moderation/pipeline.detect_document`), so the evaluation measures production
code.

## Merge details that the tests pin down

- Two hits belong to one fragment when their spans overlap inside one segment;
  the record keeps the union span, so the stored quote is always verbatim text.
- The rule's `flag_type` wins when rule and model disagree (the rule hit is
  deterministic); the merged confidence is the maximum of the members.
- A low-confidence model hit that overlaps a rule hit is **not** dropped (it is
  not "model only"); it merges into `rule+model`.
- Repeated text inside one segment is flagged once, at its first occurrence.
- A model flag for a context segment is kept (recall-first) and counted in
  `context_seq_flags` instead of being silently discarded.
- Merged reasons are truncated to 40 characters; a merged span is capped at 200
  characters.

## Data model

`ip_sensitive_flags` follows the platform convention: `id` UUID, `org_id`,
`site_id`, `created_at`, `created_by`, `updated_at`, `deleted_at` (soft delete).
Domain columns: `content_id`, `run_id`, `segment_seq`, `quote`, `quote_start`,
`quote_end`, `flag_type` (`third_party_personal` / `unpublished_business` /
`unlicensed_brand` / `other`), `detector` (`rule` / `model` / `rule+model`),
`confidence` (`numeric(4,3)`), `reason` (<= 40), `rule_code`, `prompt_version`,
`disposition` (`pending` / `accepted` / `dismissed`), `reviewed_by`,
`reviewed_at`, `review_note`.

The migration has a real `downgrade()`; `upgrade -> downgrade base -> upgrade`
and `alembic check` are part of the test suite.  All integrity rules are
enforced by PostgreSQL (CHECK constraints, the partial unique fragment index,
`disposition` default `'pending'`), not only by the application.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/ip-drafts/{content_id}/sensitive-review` | Run one re-review; returns the run summary and the flags it wrote |
| GET | `/v1/ip-drafts/{content_id}/sensitive-flags` | Review queue of one draft (`disposition`, `limit`, `offset`) |
| GET | `/v1/sensitive-flags/{flag_id}` | One flag |
| PATCH | `/v1/sensitive-flags/{flag_id}` | Staff disposition: `accepted` or `dismissed` (+ `reviewed_by`, optional note) |
| DELETE | `/v1/sensitive-flags/{flag_id}` | Soft delete (row stays for audit) |
| GET | `/healthz` | Prompt version, batch size, model mode, confidence floor |

`org_id`/`site_id` are query parameters (write routes take them in the body) and
every read filters on them; a cross-tenant id is a plain 404.  `pending` is set
by the pipeline only - staff cannot move a flag back to `pending` (422).

```bash
curl -X POST http://127.0.0.1:8000/v1/ip-drafts/$CONTENT_ID/sensitive-review \
  -H 'Content-Type: application/json' \
  -d '{"org_id": "...", "site_id": "...",
       "segments": [{"seq": 0, "text": "内部会议的结论是明年砍一半预算。"}]}'
```

The response reports `mask_replacements`, per-batch accepted/rejected counts with
reject codes, `kept`, `dropped_by_code` (`quote_not_found`, `low_confidence`,
`unknown_seq`), `written`, `skipped_duplicates` and the created flags.

## Evaluation

`evalset/eval_set_v1.json` holds 8 fictional drafts (one of them 35 segments, so
two model batches are exercised) with 24 gold fragments; the recorded model
answers deliberately include a paraphrase, a low-confidence guess, an unknown
flag type and a context-segment flag.  `python -m ipsensitive.eval_cli`
regenerates `reports/evaluation_v1.md` / `.json` and exits non-zero when the
ticket thresholds are missed.

Current result: **recall 95.83 % (23/24), precision 76.67 % (23/30)** against the
required recall >= 95 % and precision >= 60 %.  Matching is fragment level: a
detection and a gold fragment match when they share at least half of the shorter
span in the same segment (and, for the primary metrics, the same `flag_type`).
Span-only numbers are reported next to the type-aware ones.  The report lists the
one missed fragment and every over-flagged fragment, so the numbers can be
audited rather than taken on trust.

## Integration points

- **Upstream rule engine** (another ticket): replace
  `RegexRuleDetector.detect` with the real rule result, or keep the stub and
  pass `rule_hits` in the POST body.  The contract is
  `RuleHit(seq, quote, flag_type, rule_code, reason)`; quotes are expected on the
  masked text.  The stub exists only so the merge can be exercised.
- **Drafts table** (`ip_drafts`, another ticket): `ip_sensitive_flags.content_id`
  is a plain UUID with no cross-ticket foreign key; add the FK when that table
  lands.  Ownership check (content belongs to the tenant in the request) belongs
  to the gateway/that module.
- **Authentication**: this service trusts `org_id`, `site_id`, `created_by` and
  `reviewed_by` from the request.  In deployment the gateway must inject them
  from the authenticated staff session; no auth is implemented here by design.
- **Model gateway**: point `IP_SENSITIVE_MODEL_BASE_URL` at the client-side
  gateway; the prompt version travels in every payload, and the recorded model
  id / prompt version are stored per row (`prompt_version`).
- **Review workbench** (another ticket): the queue reads masked quotes and
  offsets (`quote_start`/`quote_end` inside the masked segment), which is what
  the red-flag highlighting needs.  Raw offsets are intentionally not stored, so
  the queue cannot leak identifiers that the masking gate removed.

## Deliberate limitations

- The rule detector is a stub; it is not the client's rule engine.
- The model answers used by the evaluation, the tests and the demo are recorded
  from a fictional model.  They are the shipped evaluation fixture, not a live
  model call; `IP_SENSITIVE_MODEL_MODE=http` is the deployment path.
- 24 gold fragments is a demonstration set sized for this ticket, not a
  production calibration corpus; the harness accepts any larger set with the
  same schema.
- No UI, no queue assignment, no rework workflow: staff disposition is the API's
  PATCH.  No monetary value is computed anywhere (`AI 不计算金额`), and no flag is
  ever published or sent outside the platform.

## Confidentiality

Every person, enterprise, brand, phone number, e-mail address and ID number in
the code, tests, evaluation set and reports is invented for this ticket.  No real
personal or enterprise information is used and no client name appears in the
deliverable.  The prompt keeps the ticket's rule that content must not leave the
platform: the only outbound call is the configured model gateway, and it receives
masked text only.

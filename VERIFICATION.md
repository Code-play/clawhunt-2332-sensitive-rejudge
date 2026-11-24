# VERIFICATION - S5-IP-024

Clause -> command -> expected -> measured. Everything below was run in this
workspace on 2026-11-24 against a live PostgreSQL 16 database
(`postgres:16-alpine`, host port 45432), Python 3.13.14, pytest 9.2.0,
SQLAlchemy 2.1.9, Alembic 1.x and psycopg 3.

Environment used for every run:

```text
DATABASE_URL=postgresql+psycopg://ipsensitive:ipsensitive_local_dev@127.0.0.1:45432/ipsensitive
```

The schema is created by the ticket's own Alembic migration (`alembic upgrade
head` is part of the test fixture), so every test run also proves the migration
works.

## 1. Acceptance criterion 1 - merged detection: recall >= 95 %, precision >= 60 % on the fictional set

Clause: "After merging rules and model, on the fictional evaluation set, recall
>= 95 %, precision >= 60 % (evaluation report attached)."

```bash
python -m ipsensitive.eval_cli
```

Expected: the merged pipeline (masking gate -> rule stage -> recorded fictional
model -> merge) runs over `evalset/eval_set_v1.json`, both thresholds hold, and
`reports/evaluation_v1.md` / `.json` are regenerated.

Measured (exit code 0):

```text
{
  "dataset": "ip-sensitive-eval-v1",
  "documents": 8,
  "gold": 24,
  "detected": 30,
  "tp": 23,
  "fp": 7,
  "fn": 1,
  "recall": 0.9583,
  "precision": 0.7667,
  "recall_floor": 0.95,
  "precision_floor": 0.6,
  "passes": true,
  ...
}
```

The attached report (`reports/evaluation_v1.md`) also lists, per document, per
flag type, the one deliberately missed fragment, every over-flagged fragment and
both drop paths (`quote_not_found`, `low_confidence`). The 35-segment document is
evaluated through two model batches, so the 30 + 1/1 batching is measured, not
assumed.

```bash
python -m pytest tests/test_acceptance_evaluation.py -q --tb=no -o addopts=""
```

Expected: thresholds asserted, the set proven fictional and multi-batch, and the
committed report proven current (a fresh evaluation reproduces it).
Measured: `5 passed`.

## 2. Acceptance criterion 2 - a quote that is not in the source is discarded

Clause: "When the quote returned by the model is not found in the original text,
that entry is discarded (tested)."

```bash
python -m pytest tests/test_acceptance_quote.py -q --tb=no -o addopts=""
```

Expected: a paraphrase, a quote with different whitespace, a quote pointing at
the wrong segment and a bad upstream rule quote all produce
`dropped_by_code == {"quote_not_found": 1}` and no row; a verbatim quote is kept
as the control case.

Measured: `5 passed`. The drafts in this file contain no rule trigger, so a row
can only come from the model flag under test.

## 3. Acceptance criterion 3 - the same fragment hit by rule and model is one row

Clause: "When the same fragment is matched by both rules and model, only 1 record
is written (tested)."

```bash
python -m pytest tests/test_acceptance_merge.py -q --tb=no -o addopts=""
```

Expected: one persisted row with `detector = 'rule+model'`; a wider model span
still yields exactly one row (union span); re-running the pipeline writes
nothing (`skipped_duplicates = 1`); pure rule and pure model fragments still
record their own detector.

Measured: `4 passed`.

## 4. Prompt file `ip.sensitive.v1`

```bash
python -m pytest tests/test_prompt.py tests/test_segments.py -q --tb=no -o addopts=""
```

Expected: the file declares version `ip.sensitive.v1`, 30 segments per batch,
one context segment per side, the four `flag_type` values, `reason <= 40`
characters and confidence 0..1; each required instruction is present; a batch of
30 targets + 1 context renders with roles; any contract violation (batch size,
context, version, recall policy, verbatim-quote instruction) is rejected.

Measured: `19 passed` (prompt) and `10 passed` (batching).

## 5. Model answer validation

```bash
python -m pytest tests/test_schema.py tests/test_model_client.py -q --tb=no -o addopts=""
```

Expected: plain / code-fenced / prose-wrapped JSON is parsed; unknown types,
over-long reasons, out-of-range confidence, empty quotes, unknown fields and
missing fields are rejected per entry with a code and never discard the other
entries; the HTTP client posts the contract payload, retries a 5xx once, does not
retry a 4xx, and rejects unusable gateway answers.

Measured: `22 passed` (validation) and `10 passed` (transports).

## 6. Masking gate (unified entry before any model call)

```bash
python -m pytest tests/test_masking.py -q --tb=no -o addopts=""
```

Expected: phone numbers, e-mail addresses, card numbers, ID numbers and roster
names become typed `[PII:...]` placeholders; masking is idempotent; a placeholder
maps back to its raw span while a span cutting the placeholder does not; no
11-digit run survives.

Measured: `11 passed`.

## 7. Merge unit behaviour

```bash
python -m pytest tests/test_merge.py -q --tb=no -o addopts=""
```

Expected: rule+model merge, rule type wins, model-only drop below 0.5, exactly
0.5 kept, low-confidence hit overlapping a rule kept as rule+model, non-verbatim
quotes dropped for both detectors, unknown segment dropped, separate fragments
stay separate, repeated text flagged once, merged span capped at 200 characters,
merged reasons capped at 40.

Measured: `18 passed`.

## 8. Persistence and the review queue

```bash
python -m pytest tests/test_storage.py -q --tb=no -o addopts=""
```

Expected: new rows are `pending`; duplicate fragments skipped; the partial unique
index rejects a second live row; soft-deleted rows disappear and can be recreated;
filters and pagination work; staff dispositions set reviewer and timestamp;
`pending` cannot be set by staff; invalid rows are rejected by PostgreSQL.

Measured: `9 passed`.

## 9. HTTP API and CRUD

```bash
python -m pytest tests/test_api.py -q --tb=no -o addopts=""
```

Expected: `POST sensitive-review` writes the queue and returns its flags;
queue listing and filters; detail; PATCH accepted/dismissed; DELETE soft delete;
cross-tenant ids are plain 404s; invalid payloads are 422; a model outage with
`fail` policy is 503 with nothing written; `rules_only` reports a degraded run.

Measured: `9 passed`.

## 10. Schema, migration and DDL

```bash
python -m pytest tests/test_migration.py -q --tb=no -o addopts=""
```

Expected: database at Alembic head; platform columns present; every CHECK
constraint exists; `disposition` defaults to `pending` at the database level; the
queue indexes and the unique fragment expression index exist; the migration
round-trips; `alembic check` reports no drift.

Measured: `8 passed`.

Command-level evidence:

```bash
alembic current
alembic check
alembic downgrade base && alembic upgrade head && alembic current
docker exec <db> psql -U ipsensitive -d ipsensitive -c "\d+ ip_sensitive_flags"
```

Measured:

```text
0001_ip_sensitive_flags (head)
No new upgrade operations detected.
INFO  [alembic.runtime.migration] Running downgrade 0001_ip_sensitive_flags -> , ip_sensitive_flags - review queue ...
INFO  [alembic.runtime.migration] Running upgrade  -> 0001_ip_sensitive_flags, ip_sensitive_flags - review queue ...
0001_ip_sensitive_flags (head)
```

DDL excerpt (indexes and CHECK constraints, abridged):

```text
Indexes:
    "ip_sensitive_flags_pkey" PRIMARY KEY, btree (id)
    "ix_ip_sensitive_flags_queue" btree (org_id, site_id, content_id, disposition) WHERE deleted_at IS NULL
    "ix_ip_sensitive_flags_run" btree (run_id)
    "uq_ip_sensitive_flags_fragment" UNIQUE, btree (content_id, segment_seq, md5(quote)) WHERE deleted_at IS NULL
Check constraints:
    "ck_ip_sensitive_flags_confidence" CHECK (confidence >= 0::numeric AND confidence <= 1::numeric)
    "ck_ip_sensitive_flags_detector" CHECK (detector::text = ANY (ARRAY['rule','model','rule+model']))
    "ck_ip_sensitive_flags_disposition" CHECK (disposition::text = ANY (ARRAY['pending','accepted','dismissed']))
    "ck_ip_sensitive_flags_flag_type" CHECK (flag_type::text = ANY (ARRAY['third_party_personal', ...]))
    "ck_ip_sensitive_flags_quote_len" CHECK (char_length(quote) >= 1 AND char_length(quote) <= 200)
    "ck_ip_sensitive_flags_reason_len" CHECK (char_length(reason::text) >= 1 AND char_length(reason::text) <= 40)
    "ck_ip_sensitive_flags_review_state" CHECK (disposition = 'pending' AND reviewed_by IS NULL ...)
    "ck_ip_sensitive_flags_seq" CHECK (segment_seq >= 0)
    "ck_ip_sensitive_flags_span" CHECK (quote_start >= 0 AND quote_end > quote_start AND quote_end - quote_start <= 200)
disposition | character varying(16) | not null | 'pending'::character varying
```

## 11. Counterexamples and policy paths

```bash
python -m pytest tests/test_counterexamples.py -q --tb=no -o addopts=""
```

Expected: model flags on missing segments dropped; rejected entries counted, not
written; non-JSON answers keep rule flags; the 0.49/0.5 boundary behaves as the
ticket says; context-segment flags kept and counted; model outage with `fail`
policy raises and writes nothing; `rules_only` is degraded and writes rule flags;
duplicate/missing segments rejected before any model call; only masked quotes are
stored; the AI stage has no monetary field; confidence stored with three
decimals.

Measured: `11 passed`.

## 12. Configuration

```bash
python -m pytest tests/test_config.py -q --tb=no -o addopts=""
```

Expected: every setting comes from the environment, defaults are safe, invalid
modes/limits are rejected.

Measured: `7 passed`.

## 13. Full suite

```bash
python -m pytest -q --tb=no -o addopts=""
```

Measured:

```text
153 passed in 3.60s
```

Per file: acceptance_evaluation 5, acceptance_quote 5, acceptance_merge 4, prompt
19, segments 10, masking 11, schema 22, model_client 10, rules 5, merge 18,
storage 9, api 9, migration 8, counterexamples 11, config 7.

## 14. End-to-end HTTP demo

```bash
DATABASE_URL=... python examples/http_demo.py
```

Expected: a draft with a raw phone number runs through masking, the rule stage
and the recorded model; the merged flags are returned; staff disposition and
soft delete work.

Measured (abridged):

```text
healthz: prompt=ip.sensitive.v1 batch=30 mode=recorded
run: written=4 kept=4 rule_hits=2 model_flags=4 model_rejected=1 mask_replacements=1
run: dropped_by_code={} context_seq_flags=0
flags (masked quotes only):
  seq=0 [13:20] model      unpublished_business conf=0.70 quote='明年招标的底价'
  seq=1 [4:26] rule+model third_party_personal conf=1.00 quote='采购经理姓吴，手机号 [PII:PHONE]'
  seq=1 [27:42] model      unpublished_business conf=0.78 quote='他私下给我的报价比公开低 8%'
  seq=3 [3:17] rule+model unpublished_business conf=1.00 quote='某竞品公司还没公开的融资消息'
queue: total=4 pending=4
review: <flag uuid> -> accepted by <staff uuid>
queue after review: pending=3
delete: status=204
```

The masked phone number never reaches the model, and the stored quote contains
`[PII:PHONE]` rather than the number.

## Summary

| Clause | Result |
|---|---|
| 1. Merged recall >= 95 %, precision >= 60 % on the fictional set | pass - recall 95.83 % (23/24), precision 76.67 % (23/30), report attached |
| 2. Non-verbatim quote discarded | pass (5 tests) |
| 3. Rule + model on one fragment = 1 row | pass (4 tests) |
| 4. Prompt `ip.sensitive.v1`: 30 per batch, 1 context each side, JSON schema | pass (19 + 10 tests) |
| 5. Model answers validated per entry | pass (22 + 10 tests) |
| 6. Unified masking gate before every model call | pass (11 tests) |
| 7. Merge contract in code | pass (18 tests) |
| 8. `ip_sensitive_flags`, `disposition` pending, staff dispositions | pass (9 tests) |
| 9. HTTP CRUD, tenant isolation | pass (9 tests) |
| 10. Migration, DDL, roundtrip, no drift | pass (8 tests + commands) |
| 11. Counterexamples and outage policies | pass (11 tests) |
| 12. Environment-only configuration | pass (7 tests) |
| 13. Full suite | 153 passed |
| 14. End-to-end HTTP demo | pass |

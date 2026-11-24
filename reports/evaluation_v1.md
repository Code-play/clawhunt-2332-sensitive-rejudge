# Sensitive re-review evaluation - ip-sensitive-eval-v1

Merged rule + model detection on a fully fictional evaluation set (prompt `ip.sensitive.v1`, model `fictional-ip-sensitive-model-v1`).

| Metric | Value | Floor | Result |
|---|---|---|---|
| Recall | 95.83% (23/24) | >= 95% | pass |
| Precision | 76.67% (23/30) | >= 60% | pass |
| Recall (span only, type ignored) | 95.83% | - | - |
| Precision (span only, type ignored) | 76.67% | - | - |

Generated at 2026-11-24T09:21:53+00:00. Minimum model-only confidence 0.5. Masking gate replacements: 4. Context-segment model flags kept as diagnostics: 1.

## Per document

| Document | Segments | Batches | Gold | Detected | TP | FP | FN | Recall | Precision |
|---|---|---|---|---|---|---|---|---|---|
| c-101 从车间主任到行业顾问 | 6 | 1 | 5 | 6 | 5 | 1 | 0 | 100% | 83% |
| c-102 供应链那点事 | 5 | 1 | 4 | 4 | 4 | 0 | 0 | 100% | 100% |
| c-103 品牌联名避坑 | 4 | 1 | 3 | 4 | 3 | 1 | 0 | 100% | 75% |
| c-104 客户案例怎么写 | 5 | 1 | 2 | 3 | 2 | 1 | 0 | 100% | 67% |
| c-105 个人 IP 运营复盘（长文，两个批次） | 35 | 2 | 5 | 7 | 5 | 2 | 0 | 100% | 71% |
| c-106 三种常见的合规雷区 | 4 | 1 | 2 | 2 | 1 | 1 | 1 | 50% | 50% |
| c-107 招聘那篇稿子 | 5 | 1 | 2 | 3 | 2 | 1 | 0 | 100% | 67% |
| c-108 带徒弟 | 4 | 1 | 1 | 1 | 1 | 0 | 0 | 100% | 100% |

## Per flag type

| Flag type | Gold | Detected | TP | Recall | Precision |
|---|---|---|---|---|---|
| third_party_personal | 5 | 9 | 5 | 100% | 56% |
| unpublished_business | 9 | 9 | 9 | 100% | 100% |
| unlicensed_brand | 7 | 6 | 6 | 86% | 100% |
| other | 3 | 6 | 3 | 100% | 50% |

## Dropped hits

| Code | Count | Meaning |
|---|---|---|
| low_confidence | 2 | model-only hit below the confidence floor |
| quote_not_found | 1 | quote is not verbatim in the segment - the model paraphrased it |

## Missed gold fragments

- c-106 segment 2: `引用别人课程里的原话` (unlicensed_brand)

## False positives kept

- c-101 segment 5: `[PII:EMAIL]` (third_party_personal, rule, confidence 1.0)
- c-103 segment 3: `这事后来赔了钱` (other, model, confidence 0.55)
- c-104 segment 0: `[PII:PHONE]` (third_party_personal, rule, confidence 1.0)
- c-105 segment 7: `今天先聊到这里` (other, model, confidence 0.52)
- c-105 segment 29: `这周先聊到这里` (other, model, confidence 0.6)
- c-106 segment 3: `合作方的员工姓名和联系方式` (third_party_personal, model, confidence 0.5)
- c-107 segment 2: `[PII:ID]` (third_party_personal, rule, confidence 1.0)

## Method

1. Each draft is passed through the unified masking gate, so the model sees `[PII:...]`
   placeholders instead of phone numbers, e-mail addresses, card numbers or ID numbers.
2. Segments are batched 30 per call with one context segment on each side; the recorded
   model answers are validated per entry (unknown fields, unknown flag types, reason length).
3. The rule stage runs on the same masked text; rule hits and model flags are merged:
   overlapping hits become one record (`rule+model`), model-only hits below 0.5 are
   dropped, and a quote that is not verbatim in the segment is dropped.
4. A detection and a gold flag match when they are in the same segment, share at least
   half of the shorter span and - for the primary metrics - carry the same flag type.

## Confidentiality

Every person, enterprise, brand and number in the evaluation set is invented for this ticket. No real personal or enterprise information and no client name appears in the set, the recorded answers or this report.

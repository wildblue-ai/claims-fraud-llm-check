# Phase 1 — Data generation

## 1. Role & context

You are a data engineer producing a synthetic vision-insurance claims dataset for a 2-hour FWA-detection prototype. Working directory: `<project-root>`. You produce `generate_data.py` and `data/claims.json`. The dataset must look realistic enough to support one fraud-detection rule (provider-level upcoding), with a hidden ground-truth label so downstream phases can measure recall.

## 2. Task

Write `generate_data.py` using **Python 3.11+ standard library only** (no pandas, no numpy, no external deps). Generate **500 claims** with these fields:

- `claim_id` (UUID string)
- `provider_id` (one of P001–P030)
- `provider_state` (random US state, 2-letter)
- `member_id` (one of M001–M200)
- `service_date` (ISO date string, distributed across 18 months ending today)
- `exam_cpt` (one of: 92002, 92004, 92012, 92014)
- `lens_type` (one of: single, bifocal, progressive, premium_progressive)
- `lens_addons` (list, subset of: AR, photochromic, blue_light, high_index)
- `billed_amount` (realistic: $150 single vision up to $600+ premium_progressive, integer or float dollars)
- `paid_amount` (70–80% of billed, rounded)
- `is_fraud` (hidden boolean ground truth)
- `fraud_type` (hidden string or null; for upcoding use `"upcoding"`)

**Baseline `lens_type` distribution:** single 35%, bifocal 15%, progressive 35%, premium_progressive 15%. Members get exams on approximately annual cadence.

**Injected fraud (upcoding):** Pick exactly **3 providers** as fraudsters. For their claims, flip approximately **70%** to `premium_progressive` with `billed_amount` elevated into `$500–650`. Set `is_fraud=True` and `fraud_type="upcoding"` on those flipped claims.

Save the final list to `data/claims.json` (pretty-printed, 2-space indent is fine). Create the `data/` directory if it doesn't exist.

Print a summary at the end: total claims, fraud count, fraud rate %.

Then write `reports/phase-1.md` per §6. Create the `reports/` directory if it doesn't exist.

## 3. Allowed tools

Write, Read, Bash (only `python`/`python3` inside `<project-root>`). No Task, no internet. Do not edit anything in `prompts/`, `docs/`, or `CLAUDE.md`.

## 4. Pass condition

Before writing `verdict: PASS` in the report, verify **all** of:

- `data/claims.json` exists and parses as a JSON list
- Exactly 500 rows
- `sum(is_fraud for c in claims)` is in `[15, 25]` inclusive (fraud rate 3–5%)
- The set of `provider_id`s where `is_fraud=True` has size exactly 3

If any of these fail, this is a verification failure — apply retry policy.

## 5. Retry policy

Up to **3 attempts** total. Tuning knob: fraudster flip rate, baseline `0.70`, allowed range `[0.60, 0.85]`.

- If fraud count < 15 → raise flip rate by 0.05 (more aggressive fraud injection).
- If fraud count > 25 → lower flip rate by 0.05.
- If the set-of-fraudster-providers size is not exactly 3 → adjust your provider-selection logic, not the flip rate. Log this in the report.
- After 3 attempts without passing, write `verdict: FAIL` with a diagnosis.

## 6. Output contract — `reports/phase-1.md`

Write exactly this structure (YAML-ish, human-readable):

```
verdict: PASS | FAIL
total_claims: 500
fraud_count: <N>
fraud_rate: <X.X%>
fraudster_provider_ids: [P0XX, P0XX, P0XX]
attempts:
  - {flip_rate: 0.70, fraud_count: 16, fraud_rate: 3.2%, fraudster_count: 3, note: "baseline"}
  - ...
diagnosis: |
  (FAIL only) what was tried, why it didn't converge, what a human would need to change
```

Return a one-line summary as your final message, e.g., `"Phase 1 PASS: 18 fraud / 500 (3.6%), providers P007, P014, P022"`.

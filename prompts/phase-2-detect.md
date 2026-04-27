# Phase 2 — Detection

## 1. Role & context

You are a data engineer implementing a provider-level upcoding detector for a synthetic vision-claims dataset. Phase 1 has already run — `data/claims.json` exists with 500 claims including a hidden `is_fraud` label. Working directory: `<project-root>`. You write `detection.py`, run it, and produce `data/claims_scored.json`.

## 2. Task

Write `detection.py` using Python 3.11+ standard library only. It must:

1. Read `data/claims.json`.
2. For each `provider_id`, compute their `premium_progressive` rate = (share of that provider's claims that are `premium_progressive`).
3. Compute the **population mean** and **stdev** of that rate across all providers.
4. Flag any claim from a provider whose rate is `> threshold` σ above the mean. (Default `threshold = 2.0`.)
5. For **every** claim (flagged or not), add these fields:
   - `risk_score` (float): the provider's z-score scaled 0–100 and capped at 100. Non-triggered providers get `risk_score` based on their z, floor at 0.
   - `triggered` (bool)
   - `triggered_rule` (`"provider_upcoding"` if triggered, else `null`)
   - `rule_reason` (string if triggered, else `null`). Example: `"Provider P012 bills premium_progressive at 78% vs population mean of 15% (z=4.1)"`
6. Save the augmented list to `data/claims_scored.json`.
7. Compute precision and recall against the ground-truth `is_fraud` label across all 500 claims. Print both plus the threshold used.
8. Write `reports/phase-2.md` per §6.

## 3. Allowed tools

Write, Read, Bash (only `python`/`python3` inside `<project-root>`). No Task, no internet. Do not edit anything in `prompts/`, `docs/`, or `CLAUDE.md`.

## 4. Pass condition

`recall >= 0.70` on the injected-fraud labels. Precision is **reported**, not gated.

## 5. Retry policy

Up to **3 attempts**. Tuning knob: z-score threshold, baseline `2.0`, allowed range `[1.5, 2.5]`, step `0.25`.

- If `recall < 0.70` → lower threshold by 0.25 and rerun.
- If already at lower bound (`1.5`) and still failing → stop, write `verdict: FAIL` with diagnosis.
- Do not alter the rule itself — only the threshold.

## 6. Output contract — `reports/phase-2.md`

```
verdict: PASS | FAIL
recall: 0.XX
precision: 0.XX
threshold_used: X.X
attempts:
  - {threshold: 2.0, recall: 0.67, precision: 0.92, note: "baseline"}
  - {threshold: 1.75, recall: 0.83, precision: 0.71, note: "lowered for recall"}
flagged_sample: [<claim_id>, <claim_id>, <claim_id>]
diagnosis: |
  (FAIL only) what was tried, why recall stayed below threshold, what a human would need to change
```

`flagged_sample` should be 3 `claim_id`s from triggered claims for orchestrator spot-check (pick any).

Return a one-line summary as your final message, e.g., `"Phase 2 PASS: recall=0.83, precision=0.71, threshold=1.75"`.

# One-shot per phase: design

**Date:** 2026-04-22
**Project:** Insurance Fraud Detection POC (2-hour prototype)
**Source brief:** [`../../../Claude Code Brief.md`](../../../Claude%20Code%20Brief.md) (original filename retained as historical record)

## Context

The brief defines three phases (data, detection, app) with manual human checkpoints between them. This design replaces each manual checkpoint with **self-verifying subagent dispatch**: each phase runs as a Task subagent against a durable prompt file, self-verifies against a numeric/behavioral pass condition, and writes a machine-readable report. The orchestrator (this session) reads the report and surfaces back to the user at phase boundaries.

### Goals

- **Per-phase autonomy.** "Run phase N" completes the phase end-to-end — write code, run it, self-verify — with no back-and-forth *within* a phase.
- **Durable prompt artifacts.** Each prompt is a checked-in file in `prompts/`. Viable as portfolio material; trivially promotable to headless dispatch (`claude -p`) later.
- **User stays in the loop at phase boundaries.** This is not "build the whole app in one command" — the user decides to advance after each phase.

### Non-goals

- Headless shell-script dispatch of `claude -p`. Deferred.
- Parallel phase execution. Phases have sequential file dependencies.
- Auto-advancing through all three phases without user input.
- A separate test suite. Per the brief, tests are out of scope.

## Architecture

```
<project-root>/
├── CLAUDE.md                             # pointer to brief + hard rules
├── Claude Code Brief: ....md             # source of truth
├── prompts/
│   ├── phase-1-data.md
│   ├── phase-2-detect.md
│   └── phase-3-app.md
├── reports/                              # gitignored, written by subagents
│   ├── phase-1.md
│   ├── phase-2.md
│   └── phase-3.md
├── data/
│   ├── claims.json                       # gitignored, written by phase 1
│   └── claims_scored.json                # gitignored, written by phase 2
├── generate_data.py                      # written by phase 1
├── detection.py                          # written by phase 2
├── main.py                               # written by phase 3
├── templates/index.html                  # written by phase 3
├── requirements.txt                      # written by phase 3
├── .env.example                          # bootstrap (committed)
├── .env                                  # gitignored
├── .gitignore                            # bootstrap
└── README.md                             # written by phase 3
```

Each prompt file is **self-contained**: it inlines the slice of the brief the subagent needs. The subagent does not need to read the full brief or `CLAUDE.md` to do its job.

## Bootstrap (before any phase dispatches)

Three files must exist in the project root before Phase 1 runs. These are one-time, not agent-dispatched:

- `.gitignore` — excludes `.env`, `__pycache__/`, `*.pyc`, `venv/`, `data/`, `reports/`, `.superpowers/`.
- `.env.example` — template with `ANTHROPIC_API_KEY=` placeholder. Committed.
- `.env` — real key, user-provided locally. Never committed.

If `<project-root>/` is not yet a git repository, bootstrap also includes `git init`. The orchestrator handles bootstrap in its first working turn, not via subagent.

## Per-phase prompt skeleton

Every build-phase prompt file follows the same six-section shape, in order:

1. **Role & context** — agent identity + working-directory state.
2. **Task** — concrete files to write, commands to run, fields to produce.
3. **Allowed tools** — Write, Read, Bash scoped to the project directory. No Task, no internet, no edits to `prompts/` or `CLAUDE.md`.
4. **Pass condition** — boolean gate, numeric or behavioral, lifted from the brief.
5. **Retry policy** — retry budget + bounded tuning knob (phases 1–2); halt-and-escalate (phase 3).
6. **Output contract** — structure of `reports/phase-N.md`: verdict, metrics, attempts log, diagnosis on fail.

## Prompts in this system

Four distinct prompts exist across build-time and runtime:

| # | Prompt | When it runs | What it does |
|---|---|---|---|
| 1 | `prompts/phase-1-data.md` | Build, once (retry ≤3) | Produces `generate_data.py` + `data/claims.json` |
| 2 | `prompts/phase-2-detect.md` | Build, once (retry ≤3) | Produces `detection.py` + `data/claims_scored.json` |
| 3 | `prompts/phase-3-app.md` | Build, once (halt-on-fail) | Produces `main.py`, `templates/index.html`, `README.md`, `requirements.txt` |
| 4 | `/explain` runtime prompt | Runtime, per user click | Inside `main.py`. Called on every "Analyze" click. Generates the 2–3 sentence fraud-analyst narrative that is the entire point of the demo. Sent to `claude-sonnet-4-5`, max 300 tokens, temp 0.3. Content specified verbatim in brief §"Prompt for /explain endpoint." |

Prompts 1–3 are orchestration artifacts — you see them during the build and never again. Prompt 4 is the customer-facing prompt — it runs every time someone uses the demo. If anything needs iteration after the build, it's prompt 4.

See Appendix A (Phase 1) and Appendix B (Phase 2) for sample prompt text.

## Phase 1 — Data generation

- **Artifacts:** `generate_data.py`, `data/claims.json`.
- **Pass condition:** 500 rows; fraud rate ∈ [3%, 5%]; exactly 3 distinct fraudster providers.
- **Retry budget:** 3.
- **Tuning knobs:**
  - Fraudster flip rate (baseline 70%, allowed range 60–85%).
  - Fraudster provider count (baseline 3, allowed range 2–4).
- **Report contents:** verdict, actual fraud count + rate, fraudster provider IDs, attempts log.

## Phase 2 — Detection

- **Artifacts:** `detection.py`, `data/claims_scored.json`.
- **Pass condition:** `recall ≥ 0.70` on `is_fraud` ground truth. (Matches the brief verbatim.)
- **Also reported, not gated:** precision, threshold used. If precision comes in unexpectedly low, the user decides whether to rerun with a different approach.
- **Retry budget:** 3.
- **Tuning knob:** z-score threshold (baseline 2.0, allowed range 1.5–2.5).
  - If recall < 0.70 → lower threshold by 0.25 (more sensitive).
  - If already at lower bound and still failing → stop, report failed budget.
- **Report contents:** verdict, recall, precision, threshold, attempts log (`[threshold, recall, precision, note]` per attempt), 3 sample flagged `claim_id`s for orchestrator spot-check.

## Phase 3 — App (halt-and-escalate)

- **Artifacts:** `main.py`, `templates/index.html`, `README.md`, `requirements.txt`.
- **Pass condition** (all must hold):
  1. Files exist and are syntactically valid.
  2. `python -c "from main import app"` exits clean.
  3. `uvicorn main:app --port 8765 --no-access-log` starts and binds.
  4. `GET http://localhost:8765/` returns 200 with hero-stat text in the body.
  5. `GET http://localhost:8765/explain/<flagged_claim_id>` returns 200 with non-empty text that does not match common error patterns (`Error`, `API key`, `401`, `403`, `429`, `500`).
- **No retry.** On any failure: write a diagnosis to `reports/phase-3.md` (which check failed, what the HTTP response was, uvicorn stderr excerpt) and surface to the user.
- **Cleanup:** agent kills the uvicorn PID before returning, pass or fail. Fixed port 8765 — if busy, report failure rather than auto-picking.

## Data flow

File-based, no shared in-memory state:

```
Phase 1 ──writes──▶ data/claims.json (with hidden is_fraud)
                             │
Phase 2 ──reads──▶  (applies rule + scores) ──writes──▶ data/claims_scored.json
                                                              │
Phase 3 ──reads──▶  (FastAPI loads at startup)
```

## Orchestration

Sequential, user-driven. Per phase:

1. User: "run phase N".
2. Orchestrator reads `prompts/phase-N-<name>.md`.
3. Orchestrator dispatches `Task(subagent_type="general-purpose", prompt=<file contents>)`.
4. Subagent executes, self-verifies, writes `reports/phase-N.md`, and returns a short summary as its final message.
5. Orchestrator reads the report and summarizes verdict + metrics to the user.
6. On PASS: user decides to advance to phase N+1.
7. On FAIL: user decides to re-dispatch (with prompt tweak), fix by hand, or abandon.

No automatic advancement between phases. No orchestrator-level retries beyond the per-phase budget baked into each prompt.

## Error handling

- **Within a phase (1–2):** retry up to budget with tuning-knob adjustment. Each attempt is logged in the report. Budget exhaustion = FAIL verdict with diagnosis.
- **Within phase 3:** halt-and-escalate on first failure. Diagnosis is the whole point of the report.
- **Between phases:** not Claude's decision. FAIL reports return to the user.
- **Infrastructure failure** (file system, API errors, missing deps): agent writes what it tried to the report and exits with FAIL. Does not attempt recovery beyond the tuning knob.

## Testing / verification

The self-verification gates *are* the test layer:
- Phase 1 verifies its own output against numeric bounds.
- Phase 2 verifies against the labeled ground truth produced by Phase 1.
- Phase 3 verifies against live HTTP behavior including a real Anthropic API call.

No separate test suite. Per brief §"Out of scope."

## Trade-offs accepted

- **In-session only (today).** No headless `claude -p` dispatch. Upgrade path is trivial: wrap each prompt in a shell script calling `claude -p "$(cat prompts/phase-N.md)" --allowedTools "Write,Read,Bash"`. Revisit if we want reproducible replay.
- **No precision gate on Phase 2.** Precision is reported, not gated. Risk: the retry agent could satisfy `recall ≥ 0.70` trivially by lowering the threshold until everything is flagged. Mitigation: the report surfaces precision prominently so the user catches and reruns. Accepted because (a) the brief specifies recall only, (b) iteration is cheap at prototype scale, (c) upfront guardrails risk being wrong.
- **Fixed uvicorn port 8765 on Phase 3.** Simpler cleanup, higher chance of conflict. Port conflict = FAIL rather than auto-pick. Acceptable for a local-only 2-hour build.
- **Reports directory is gitignored.** They're build artifacts, not source. The *prompts* are source.

## Open questions / deferred

None at spec-approval time. `git init` is folded into bootstrap above.

---

## Appendix A — Sample Phase 1 prompt (`prompts/phase-1-data.md`)

> Illustrative. The final prompt file may differ in minor wording.

---

**1. Role & context**

You are a data engineer producing a synthetic vision-insurance claims dataset for a 2-hour FWA-detection prototype. Working directory: `<project-root>`. You produce `generate_data.py` and `data/claims.json`. The dataset must look realistic enough to support one fraud detection rule (provider-level upcoding), with a hidden ground-truth label for downstream measurement.

**2. Task**

Write `generate_data.py` using Python 3.11+ standard library only (no pandas, no numpy). Generate 500 claims with these fields:

- `claim_id` (UUID)
- `provider_id` (P001–P030)
- `provider_state` (random US state)
- `member_id` (M001–M200)
- `service_date` (ISO date, across 18 months)
- `exam_cpt` (one of: 92002, 92004, 92012, 92014)
- `lens_type` (single | bifocal | progressive | premium_progressive)
- `lens_addons` (subset of AR, photochromic, blue_light, high_index)
- `billed_amount` ($150 single → $600+ premium_progressive)
- `paid_amount` (70–80% of billed)
- `is_fraud` (hidden boolean)
- `fraud_type` (hidden string or null)

Baseline `lens_type` distribution: single 35%, bifocal 15%, progressive 35%, premium_progressive 15%. Members on approximately annual cadence.

Injected upcoding fraud: pick 3 providers as fraudsters. Flip ~70% of their claims to `premium_progressive` with billed_amount in $500–650. Set `is_fraud=True`, `fraud_type="upcoding"`.

Save to `data/claims.json`. Print summary: total, fraud_count, fraud_rate. Then write `reports/phase-1.md` per §6.

**3. Allowed tools**

Write, Read, Bash (only `python`/`python3` inside the project dir). No Task, no internet, no edits to `prompts/` or `CLAUDE.md`.

**4. Pass condition**

All of:
- `data/claims.json` exists and parses
- Exactly 500 rows
- Fraud rate ∈ [3%, 5%] — i.e., fraud count in [15, 25]
- Exactly 3 distinct `provider_id`s have any `is_fraud=True` claims

**5. Retry policy**

Up to 3 attempts. Tuning knob: fraudster flip rate (baseline 0.70, allowed [0.60, 0.85]).
- Fraud rate < 3% → raise flip rate by 0.05
- Fraud rate > 5% → lower flip rate by 0.05
- Budget exhausted → FAIL with diagnosis

**6. Output contract — `reports/phase-1.md`**

```
verdict: PASS | FAIL
total_claims: 500
fraud_count: <N>
fraud_rate: <X.X%>
fraudster_provider_ids: [P0XX, P0XX, P0XX]
attempts:
  - {flip_rate: 0.70, fraud_count: 16, fraud_rate: 3.2%, note: "baseline"}
  - ...
diagnosis: (FAIL only) <what was tried, why it didn't converge>
```

---

## Appendix B — Sample Phase 2 prompt (`prompts/phase-2-detect.md`)

> Illustrative. The final prompt file may differ in minor wording.

---

**1. Role & context**

You are a data engineer implementing a provider-level upcoding detector for a synthetic vision-claims dataset. Phase 1 has run — `data/claims.json` exists with 500 claims including a hidden `is_fraud` label. Working directory: `<project-root>`. You write `detection.py`, run it, and produce `data/claims_scored.json`.

**2. Task**

1. Read `data/claims.json`.
2. For each provider, compute their `premium_progressive` rate (share of that provider's claims that are premium_progressive).
3. Compute population mean and stdev of that rate across providers.
4. Flag any claim from a provider whose rate is > `threshold` σ above the mean.
5. Add to each claim:
   - `risk_score` (z-score scaled 0–100, capped at 100)
   - `triggered` (bool)
   - `triggered_rule` (`"provider_upcoding"` or null)
   - `rule_reason` (e.g., `"Provider P012 bills premium_progressive at 78% vs population mean of 15% (z=4.1)"`)
6. Save to `data/claims_scored.json`.
7. Compute precision + recall against `is_fraud`. Print both.
8. Write `reports/phase-2.md` per §6.

**3. Allowed tools**

Write, Read, Bash (`python`/`python3` inside the project dir only). No Task, no internet, no edits to `prompts/` or `CLAUDE.md`.

**4. Pass condition**

`recall ≥ 0.70` on injected-fraud labels. Precision is reported, not gated.

**5. Retry policy**

Up to 3 attempts. Tuning knob: z-score threshold, baseline `2.0`, allowed `[1.5, 2.5]`.
- recall < 0.70 → lower threshold by 0.25
- at lower bound and still failing → stop, FAIL budget

Each attempt logs threshold, recall, precision, and direction.

**6. Output contract — `reports/phase-2.md`**

```
verdict: PASS | FAIL
recall: 0.XX
precision: 0.XX
threshold_used: X.X
attempts:
  - {threshold: 2.0, recall: 0.67, precision: 0.92, note: "baseline"}
  - {threshold: 1.75, recall: 0.83, precision: 0.71, note: "lowered for recall"}
flagged_sample: [<claim_id>, <claim_id>, <claim_id>]
diagnosis: (FAIL only) ...
```

---

## Appendix C — Runtime `/explain` prompt (not dispatched; lives inside `main.py`)

This is the prompt Claude receives every time a user clicks "Analyze" on a flagged row. It's the demo's AI moment. Content is lifted verbatim from the brief and embedded as a Python f-string in `main.py`:

```
You are a senior fraud analyst at a vision insurance company reviewing
a flagged claim. Be specific, measured, and professional.

FLAGGED CLAIM:
- Claim ID: {claim_id}
- Provider: {provider_id} ({provider_state})
- Member: {member_id}
- Service date: {service_date}
- Exam code: {exam_cpt}
- Lens type: {lens_type}
- Billed amount: ${billed_amount}

WHY IT WAS FLAGGED:
{rule_reason}

PROVIDER CONTEXT:
- Total claims in period: {provider_claim_count}
- Premium progressive rate: {provider_premium_rate}% (population mean: 15%)
- Total billed: ${provider_total_billed}

In 2-3 sentences, assess whether this looks like (a) likely fraud,
(b) possible documentation or coding error, or (c) a false positive
worth deprioritizing. Cite the specific pattern that drives your
assessment. Do not speculate beyond the data provided.
```

Model: `claude-sonnet-4-5` (or current Sonnet). `max_tokens=300`, `temperature=0.3`. Response cached in a module-level dict keyed by `claim_id`.

This prompt is the thing an interviewer actually evaluates. If time remains after Phase 3 passes, iterating on this prompt (tone, framing, structure) is higher-leverage than any other tweak.

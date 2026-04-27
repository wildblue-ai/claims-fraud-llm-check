# One-Shot Phase Scaffolding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce the three build-phase prompt files and the `.env.example` template so the Insurance Fraud Detection POC can be built via per-phase subagent dispatch per the design spec.

**Architecture:** This plan creates scaffolding only — no project code. Each prompt file is a self-contained brief for a Task subagent that will later produce one phase of the POC (data, detection, app). The prompts are the artifacts the orchestrator dispatches; the subagents produce `generate_data.py`, `detection.py`, `main.py`, etc.

**Tech Stack:** Markdown prompt files (Python 3.11+, FastAPI stack used by the subagents they drive). No build tooling.

**Spec reference:** `docs/superpowers/specs/2026-04-22-one-shot-phase-execution-design.md`

---

## File Structure

This plan creates exactly four files and makes one commit:

- `.env.example` — Anthropic API key template (committed, placeholder value).
- `prompts/phase-1-data.md` — subagent prompt to produce `generate_data.py` + `data/claims.json`.
- `prompts/phase-2-detect.md` — subagent prompt to produce `detection.py` + `data/claims_scored.json`.
- `prompts/phase-3-app.md` — subagent prompt to produce `main.py`, `templates/index.html`, `README.md`, `requirements.txt`.

The `reports/` directory is gitignored and gets created (`mkdir -p`) by each subagent on first run.

Each prompt file is **self-contained** — it inlines the slice of the brief its subagent needs so the subagent does not need to read the 300-line brief or `CLAUDE.md`.

---

### Task 1: Create `.env.example`

**Files:**
- Create: `.env.example`

- [ ] **Step 1: Write the file**

```
ANTHROPIC_API_KEY=sk-ant-placeholder-replace-with-your-key
```

Use the Write tool to create `<project-root>/.env.example` with exactly that one line.

- [ ] **Step 2: Verify**

Run: `cat <project-root>/.env.example`
Expected: the single `ANTHROPIC_API_KEY=sk-ant-placeholder-replace-with-your-key` line.

---

### Task 2: Write `prompts/phase-1-data.md`

**Files:**
- Create: `prompts/phase-1-data.md`

- [ ] **Step 1: Write the file**

Use the Write tool to create `<project-root>/prompts/phase-1-data.md` with the following exact content:

````markdown
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
````

- [ ] **Step 2: Verify**

Run: `wc -l <project-root>/prompts/phase-1-data.md && head -3 <project-root>/prompts/phase-1-data.md`
Expected: >60 lines, starts with `# Phase 1 — Data generation`.

---

### Task 3: Write `prompts/phase-2-detect.md`

**Files:**
- Create: `prompts/phase-2-detect.md`

- [ ] **Step 1: Write the file**

Use the Write tool to create `<project-root>/prompts/phase-2-detect.md` with the following exact content:

````markdown
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
````

- [ ] **Step 2: Verify**

Run: `wc -l <project-root>/prompts/phase-2-detect.md && head -3 <project-root>/prompts/phase-2-detect.md`
Expected: >50 lines, starts with `# Phase 2 — Detection`.

---

### Task 4: Write `prompts/phase-3-app.md`

**Files:**
- Create: `prompts/phase-3-app.md`

- [ ] **Step 1: Write the file**

Use the Write tool to create `<project-root>/prompts/phase-3-app.md` with the following exact content:

````markdown
# Phase 3 — App (halt-and-escalate)

## 1. Role & context

You are a senior Python engineer finishing a 2-hour prototype. Phase 2 has completed — `data/claims_scored.json` exists with fraud-scored claims. Working directory: `<project-root>`. You produce the FastAPI app: `main.py`, `templates/index.html`, `README.md`, and `requirements.txt`.

This is the final build phase. **No retries** — if a pass check fails, write a diagnosis and stop.

The user-facing demo moment is the HTMX-driven `/explain/{claim_id}` endpoint: a user clicks a flagged row, an Anthropic call returns a 2–3 sentence fraud-analyst narrative, it renders in-place.

## 2. Task

Write four files in the working directory.

### `requirements.txt`

```
fastapi>=0.110
uvicorn[standard]>=0.27
jinja2>=3.1
anthropic>=0.25
python-dotenv>=1.0
```

### `main.py`

Single FastAPI module. Responsibilities:

- Call `from dotenv import load_dotenv; load_dotenv()` at import time.
- At startup (module-level), load `data/claims_scored.json` into a dict keyed by `claim_id`.
- Precompute a `provider_summary` dict keyed by `provider_id` with: `claim_count`, `premium_rate` (0-100 int percent), `total_billed` (int dollars).
- Module-level `explanation_cache: dict[str, str] = {}` for `/explain` HTML snippets.
- Instantiate the Anthropic client once: `client = anthropic.Anthropic()` (picks up `ANTHROPIC_API_KEY` from env).
- Configure Jinja2Templates pointing at `"templates"`.

Two routes:

1. **`GET /`** — renders `templates/index.html`. Template context:
   - `total_claims: int` — len of claims
   - `flagged_count: int` — sum of `triggered`
   - `dollars_at_risk: int` — `sum(c["billed_amount"] for c in claims if c["triggered"])`, rounded to int
   - `detection_rate: float` — fraction of `is_fraud=True` claims that were flagged (0–1, format as percent in template)
   - `flagged_claims: list[dict]` — only triggered claims, sorted by `risk_score` desc

2. **`GET /explain/{claim_id}`** — returns an HTML snippet (media type `text/html`).
   - Look up the claim; return 404 if not found.
   - Look up provider summary for `claim["provider_id"]`.
   - If `claim_id in explanation_cache`, return the cached HTML immediately.
   - Build the prompt using **exactly** this template, substituting field values:

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

   - Call:
     ```python
     response = client.messages.create(
         model="claude-sonnet-4-5",
         max_tokens=300,
         temperature=0.3,
         messages=[{"role": "user", "content": prompt}],
     )
     text = response.content[0].text
     ```
   - Render and return:
     ```html
     <div class="bg-amber-50 border-l-4 border-amber-400 p-4 my-2 rounded">
       <div class="text-xs uppercase tracking-wide text-amber-700 mb-1">AI Fraud Analyst Review</div>
       <p class="text-gray-800">{text}</p>
     </div>
     ```
   - Store the HTML in `explanation_cache[claim_id]` before returning.

### `templates/index.html`

Single file using HTMX 2 + Tailwind 3 via CDN. Required structure:

```html
<!DOCTYPE html>
<html>
<head>
  <title>Insurance Fraud Detection POC</title>
  <script src="https://unpkg.com/htmx.org@2.0.3"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50">
  <header class="bg-[#003b71] text-white p-6">
    <h1 class="text-2xl font-semibold">Insurance Fraud Detection POC</h1>
  </header>
  <main class="max-w-7xl mx-auto p-6">
    <!-- Hero stats: 4-column grid -->
    <section class="grid grid-cols-4 gap-4">
      <!-- Four cards: total_claims, flagged_count, dollars_at_risk, detection_rate -->
    </section>
    <!-- Flagged table -->
    <section class="mt-8">
      <table class="w-full bg-white rounded shadow">
        <thead>
          <tr>
            <!-- Risk | Claim | Provider | Member | Date | Lens | Billed | Rule | (action) -->
          </tr>
        </thead>
        <tbody>
          {% for c in flagged_claims %}
          <tr>
            <td><!-- risk badge: >80 red, >60 amber, else yellow --></td>
            <td>{{ c.claim_id[:8] }}</td>
            <td>{{ c.provider_id }}</td>
            <td>{{ c.member_id }}</td>
            <td>{{ c.service_date }}</td>
            <td>{{ c.lens_type }}</td>
            <td>${{ c.billed_amount }}</td>
            <td>{{ c.triggered_rule }}</td>
            <td>
              <button hx-get="/explain/{{ c.claim_id }}"
                      hx-target="#detail-{{ c.claim_id }}"
                      hx-swap="innerHTML"
                      hx-trigger="click">▼ Analyze</button>
            </td>
          </tr>
          <tr>
            <td colspan="9"><div id="detail-{{ c.claim_id }}"></div></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </section>
  </main>
</body>
</html>
```

The snippet above is a skeleton — fill in the hero cards with proper Tailwind styling (clean and professional, insurance-corporate blue/teal) and color the risk badge based on score (`>80` red, `>60` amber, else yellow).

### `README.md`

Include in this order:

1. One-paragraph description of what this is and the problem it addresses.
2. A prominent line: **Built in 2 hours.**
3. Setup: `python -m venv venv`, activate, `pip install -r requirements.txt`, `cp .env.example .env` and fill in key, `python generate_data.py`, `python detection.py`, `uvicorn main:app --reload`.
4. Detection metrics from the current run — **read these from `reports/phase-2.md`** and quote them (recall, precision).
5. "What production would extend to" — list other fraud patterns (frequency anomalies, provider rings, phantom add-ons) and note that real deployment requires actual claims data + out-of-distribution fraud testing.
6. Caveat: "Synthetic data validates the detection pipeline, not model generalization."

## 3. Allowed tools

Write, Read, Bash (`python`/`python3`/`uvicorn`/`curl`/`kill`/`lsof` inside `<project-root>`). The Anthropic SDK may reach the internet via the Bash verification step. Do not edit `prompts/`, `docs/`, or `CLAUDE.md`.

## 4. Pass condition

Verify **all** of these in order. Abort on first failure.

1. **Files exist:** `main.py`, `templates/index.html`, `README.md`, `requirements.txt` all non-empty.
2. **Imports:** `python3 -c "from main import app"` exits 0 with no output on stderr.
3. **Server binds:** Start `uvicorn main:app --port 8765 --no-access-log` in background. Wait up to 3 seconds for port 8765 to accept connections. If it doesn't bind, FAIL.
4. **Root route:** `curl -sf http://localhost:8765/` returns 200. Body contains the substring `flagged` or `$` (hero stat).
5. **Explain route:** Read `data/claims_scored.json`, pick the first claim with `triggered=true`. `curl -sf http://localhost:8765/explain/<that_claim_id>` returns 200. Body is non-empty (>50 chars) and does **not** contain any of these strings: `API key`, `401`, `403`, `429`, `500`, a leading `Error:` pattern, or `Traceback`.
6. **Cleanup:** Kill the uvicorn PID. Verify port 8765 is no longer bound (best-effort).

## 5. Retry policy

**None.** On first failure of any of checks 1–6, write `reports/phase-3.md` with a diagnosis and stop. Do not attempt recovery.

## 6. Output contract — `reports/phase-3.md`

```
verdict: PASS | FAIL
checks:
  files_exist: PASS | FAIL
  import_main: PASS | FAIL
  uvicorn_binds: PASS | FAIL
  get_root: PASS | FAIL
  get_explain: PASS | FAIL
  cleanup: PASS | FAIL
sample_explain_output: |
  (PASS only) first ~200 chars of the Claude response from check 5
diagnosis:
  (FAIL only)
  which_check: <name of failing check>
  details: |
    <HTTP status, response body excerpt, uvicorn stderr excerpt, or exception traceback — whatever is relevant>
```

Return a one-line summary as your final message, e.g., `"Phase 3 PASS: all 6 checks passed. Sample explain output: 'This claim shows a classic upcoding pattern...'"`.
````

- [ ] **Step 2: Verify**

Run: `wc -l <project-root>/prompts/phase-3-app.md && head -3 <project-root>/prompts/phase-3-app.md`
Expected: >150 lines, starts with `# Phase 3 — App (halt-and-escalate)`.

---

### Task 5: Commit the scaffolding

**Files:**
- Stage: `.env.example`, `prompts/phase-1-data.md`, `prompts/phase-2-detect.md`, `prompts/phase-3-app.md`

- [ ] **Step 1: Check git status**

Run: `git status --short`
Expected: four untracked files — `.env.example`, `prompts/phase-1-data.md`, `prompts/phase-2-detect.md`, `prompts/phase-3-app.md`.

- [ ] **Step 2: Stage the four files**

Run: `git add .env.example prompts/phase-1-data.md prompts/phase-2-detect.md prompts/phase-3-app.md`

- [ ] **Step 3: Commit**

Run (HEREDOC for clean message):

```bash
git commit -m "$(cat <<'EOF'
Add one-shot phase prompts + env template

Three phase prompts (data, detect, app) dispatched as Task subagents per
the design in docs/superpowers/specs/2026-04-22-one-shot-phase-execution-design.md.
Phases 1–2 retry with a bounded tuning knob; phase 3 is halt-and-escalate.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify**

Run: `git log --oneline -2 && git status --short`
Expected: two commits on master, working tree clean.

---

## Done criteria

After all tasks complete:
- Four new tracked files exist: `.env.example`, `prompts/phase-{1,2,3}-*.md`.
- `git log` shows the initial commit and the scaffolding commit.
- Ready to dispatch Phase 1 as a Task subagent by passing the contents of `prompts/phase-1-data.md` as the prompt. No further scaffolding needed.

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

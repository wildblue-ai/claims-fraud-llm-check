# Claude Code Brief: Insurance Fraud Detection POC

## Context

I'm building a 2-hour prototype for a job application to a consultancy
(Springboard.ai) doing rapid AI ideation in the vision-insurance space.
The POC demonstrates fraud/waste/abuse detection on synthetic vision
insurance claims, with a Claude-powered reasoning layer that turns
flagged claims into investigator-ready narratives.

This is a strategy/PM-flavored build, not a production ML system. The
intellectual content is in the fraud pattern design and the LLM
integration, not in model sophistication. Simplicity is a feature.

## Working agreement

- **Planning mode first.** Before writing any code, produce a plan:
  file tree, dependencies, and the order of operations. Wait for my
  approval before implementing.
- **No new dependencies without asking.** The stack is fixed (see below).
  If you think I need something else, ask first.
- **Stop and ask at the checkpoints** marked below. Don't steamroll
  through the whole build.

## Stack (fixed — do not substitute)

- Python 3.11+
- FastAPI + Uvicorn (web framework)
- Jinja2 (templating, ships with FastAPI)
- Anthropic Python SDK (LLM calls)
- HTMX via CDN (no npm, no build step)
- Tailwind CSS via CDN (no build step)
- python-dotenv (env vars)
- Standard library only for data generation (no pandas needed at this scale)

## Deliverable

A single FastAPI app that runs locally with `uvicorn main:app --reload`
and shows:

1. A dashboard page with four hero stats: total claims, flagged count,
   dollars at risk, and detection rate vs injected fraud.
2. A sortable table of flagged claims (risk score, provider, member,
   date, lens type, billed amount, triggered rule).
3. Each flagged row is expandable via HTMX. Clicking expands an inline
   panel that calls the Anthropic API and returns a 2-3 sentence fraud
   analyst narrative explaining the flag.
4. A README with setup instructions and a short "what this is / what
   production would extend to" section.

## Project structure

```
claims-fraud-llm-check/
├── .env.example          # template, committed
├── .env                  # real secrets, gitignored
├── .gitignore
├── README.md
├── requirements.txt
├── main.py               # FastAPI app, all routes
├── detection.py          # fraud detection rules
├── generate_data.py      # synthetic data generator (one-off script)
├── data/
│   └── claims_scored.json  # generated output, gitignored
└── templates/
    └── index.html        # single page, HTMX + Tailwind via CDN
```

## Critical security requirement

**Before writing any code**, create `.gitignore` with at minimum:
```
.env
__pycache__/
*.pyc
venv/
data/claims_scored.json
```

The Anthropic API key lives in `.env` only. `.env.example` (committed)
shows the variable name with a placeholder. If you ever find yourself
about to write an API key into code, stop — something is wrong.

## Synthetic data spec (generate_data.py)

Generate 500 claims across 30 providers and ~200 members, spanning 18
months. Each claim has:

- `claim_id` (UUID)
- `provider_id` (P001–P030)
- `provider_state` (random US state)
- `member_id` (M001–M200)
- `service_date` (ISO date, distributed across 18 months)
- `exam_cpt` (one of: 92002, 92004, 92012, 92014)
- `lens_type` (one of: single, bifocal, progressive, premium_progressive)
- `lens_addons` (list, subset of: AR, photochromic, blue_light, high_index)
- `billed_amount` (realistic: $150 single vision up to $600+ premium_progressive)
- `paid_amount` (billed minus member copay, roughly 70-80% of billed)
- `is_fraud` (hidden ground truth — boolean)
- `fraud_type` (hidden ground truth — string or null)

### Baseline distribution (non-fraud)

Lens_type distribution should be roughly: single 35%, bifocal 15%,
progressive 35%, premium_progressive 15%. Members get exams on
approximately annual cadence.

### Injected fraud pattern: upcoding

Pick 3 providers and designate them as fraudsters. For those providers,
flip ~70% of their claims to `premium_progressive` with elevated billed
amounts ($500-650). Set `is_fraud=True` and `fraud_type="upcoding"` on
those claims. Target: ~15-25 fraud claims total (~3-5% of dataset —
realistic base rate).

Print a summary at the end: total claims generated, fraud claims
injected, fraud rate percentage.

**CHECKPOINT 1: After writing generate_data.py, run it and show me the
summary output before proceeding. Confirm the numbers look right.**

## Detection spec (detection.py)

One rule for this POC: **provider-level upcoding detector.**

- For each provider, compute their `premium_progressive` rate (share of
  their claims that are premium_progressive).
- Compute the population mean and standard deviation of that rate across
  all providers.
- Flag any claim from a provider whose rate is more than 2σ above the mean.
- Risk score: scale the provider's z-score to 0-100. Cap at 100.

Each claim gets added fields:
- `risk_score` (0-100)
- `triggered` (bool)
- `triggered_rule` (string or null, e.g., "provider_upcoding")
- `rule_reason` (human-readable string, e.g., "Provider P012 bills
  premium_progressive at 78% vs population mean of 15% (z=4.1)")

Save to `data/claims_scored.json`.

Print detection metrics at the end: how many claims flagged, how many of
the injected fraud claims were caught (true positives), false positive
count, overall precision and recall.

**CHECKPOINT 2: After running detection.py, show me the metrics. Target
is roughly 70%+ recall on injected fraud. If it's way off, we'll adjust
the threshold before moving on.**

## FastAPI app spec (main.py)

Load `data/claims_scored.json` once at startup into a module-level dict
keyed by claim_id. Also precompute provider summaries (claim count,
premium_progressive rate, total billed) for use in the explanation prompt.

### Routes

**`GET /`** — renders `templates/index.html`:
- Hero stats: total claims, flagged count, total dollars at risk (sum
  of `billed_amount` for flagged claims), detection rate (fraction of
  `is_fraud=True` claims that were flagged).
- Sortable table of flagged claims, default sorted by risk_score desc.
- Each row has columns: risk score (color-coded badge: >80 red, >60
  amber, else yellow), claim_id (short), provider_id, member_id,
  service_date, lens_type, billed_amount, triggered rule.
- Each row has a "▼ Analyze" button with:
  ```
  hx-get="/explain/{claim_id}"
  hx-target="#detail-{claim_id}"
  hx-swap="innerHTML"
  hx-trigger="click"
  ```
- Below each row, a collapsed `<tr><td colspan="8"><div id="detail-{claim_id}"></div></td></tr>`
  that the HTMX response populates.

**`GET /explain/{claim_id}`** — the Claude integration:
- Look up the claim and the provider's summary stats.
- Check `explanation_cache` (module-level dict). If hit, return cached HTML.
- Build the prompt (see below). Call Anthropic API with model
  `claude-sonnet-4-5` (or whatever the current Sonnet is — check the
  SDK docs if unsure). Max tokens 300, temperature 0.3.
- Return an HTML snippet:
  ```html
  <div class="bg-amber-50 border-l-4 border-amber-400 p-4 my-2 rounded">
    <div class="text-xs uppercase tracking-wide text-amber-700 mb-1">
      AI Fraud Analyst Review
    </div>
    <p class="text-gray-800">{response_text}</p>
  </div>
  ```
- Cache the snippet in `explanation_cache[claim_id]`.

### Prompt for /explain endpoint

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

**CHECKPOINT 3: After the app runs and a flagged row expands with real
Claude output, stop and show me a screenshot or describe what you see.
This is the moment the demo either works or doesn't.**

## Template spec (templates/index.html)

Single file. Structure:

```html
<!DOCTYPE html>
<html>
<head>
  <title>Insurance Fraud Detection POC</title>
  <script src="https://unpkg.com/htmx.org@2.0.3"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50">
  <header>...insurance-corporate blue header with title...</header>
  <main class="max-w-7xl mx-auto p-6">
    <section class="hero-stats grid grid-cols-4 gap-4">...</section>
    <section class="flagged-table mt-8">...</section>
  </main>
</body>
</html>
```

Use an insurance-corporate palette: primary blue around `#003b71` or `#1e40af`,
accent teal, neutral slate/gray for the body. Nothing fancy — clean
and professional beats clever.

## README spec

Include:
1. One-paragraph description: what this is, what problem it addresses.
2. "Built in 2 hours" line prominently near the top.
3. Setup: `python -m venv venv`, activate, `pip install -r requirements.txt`,
   copy `.env.example` to `.env` and fill in key, `python generate_data.py`,
   `python detection.py`, `uvicorn main:app --reload`.
4. Detection metrics from the current run (recall / precision against
   injected fraud).
5. "What production would extend to" section — list the other fraud
   patterns (frequency anomalies, provider rings, phantom add-ons) and
   note that real deployment would require actual claims data and
   out-of-distribution fraud testing.
6. Caveat: "Synthetic data validates the detection pipeline, not model
   generalization."

## Out of scope (do not build)

- Authentication, user accounts, databases (SQLite or otherwise)
- Charts, graphs, visualizations beyond the hero stats
- Additional fraud patterns beyond upcoding
- Deployment configuration (Docker, nginx, systemd)
- Tests
- Logging infrastructure beyond print statements
- Type hints on every function (use them where they help clarity, skip
  where they don't)

## Success criteria

I should be able to:
1. Clone the repo, follow the README, and have the app running in
   under 5 minutes.
2. See the dashboard load with accurate hero stats.
3. Click a flagged row and see a real Claude-generated fraud analyst
   narrative within 3-5 seconds.
4. Point to the detection metrics and show that the rules actually
   caught the injected fraud.

If all four work, we ship.
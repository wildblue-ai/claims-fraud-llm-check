# Insurance Fraud Detection — POC

2-hour prototype for a Springboard.ai job application. FastAPI app demonstrating fraud/waste/abuse detection on synthetic vision-insurance claims, with a Claude-powered narrative layer for flagged claims.

**Source of truth for the build:** [`Claude Code Brief.md`](./Claude%20Code%20Brief.md) — read it in full before making scope or design decisions.

## Hard rules from the brief

- **Stack is fixed** — Python 3.11+, FastAPI, Uvicorn, Jinja2, Anthropic SDK, HTMX/Tailwind via CDN, python-dotenv, stdlib only for data generation. No new dependencies without asking.
- **Anthropic API key lives in `.env` only.** Never write a key into code. `.gitignore` must exclude `.env`, `__pycache__/`, `*.pyc`, `venv/`, `data/claims_scored.json` before any code is written.
- **Out of scope:** auth, databases, charts beyond hero stats, additional fraud patterns, deployment config, tests, heavy logging, exhaustive type hints.
- **Simplicity is a feature.** The intellectual content is in fraud-pattern design and LLM integration, not model sophistication.

## Phases (from the brief)

1. **Data generation** (`generate_data.py`) — 500 synthetic claims, 3 upcoding-fraudster providers, ~3–5% injected fraud rate. Checkpoint: summary output.
2. **Detection** (`detection.py`) — provider-level upcoding detector via z-score on `premium_progressive` rate, >2σ flags. Output: `data/claims_scored.json` + precision/recall metrics. Target: ~70%+ recall.
3. **App** (`main.py` + `templates/index.html` + `README.md`) — dashboard with 4 hero stats, sortable flagged-claim table, HTMX-driven `/explain/{claim_id}` endpoint that calls Claude (`claude-sonnet-4-5`, max 300 tokens, temp 0.3) for a 2–3 sentence analyst narrative. Cache explanations in-memory.

## Success criteria

Clone → follow README → running in <5 min; dashboard shows accurate hero stats; clicking a flagged row returns a Claude narrative in 3–5s; detection metrics demonstrably catch the injected fraud.

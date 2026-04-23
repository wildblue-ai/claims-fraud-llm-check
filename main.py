"""Insurance Fraud Detection POC — FastAPI app."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_PATH = BASE_DIR / "data" / "claims_scored.json"

# Load claims at import time
with open(DATA_PATH) as f:
    _claims_list = json.load(f)

claims: dict[str, dict] = {c["claim_id"]: c for c in _claims_list}

# Precompute provider summary
_provider_agg: dict[str, dict] = defaultdict(
    lambda: {"claim_count": 0, "premium_count": 0, "total_billed": 0.0}
)
for c in _claims_list:
    pid = c["provider_id"]
    _provider_agg[pid]["claim_count"] += 1
    if c.get("lens_type") == "premium_progressive":
        _provider_agg[pid]["premium_count"] += 1
    _provider_agg[pid]["total_billed"] += c.get("billed_amount", 0) or 0

provider_summary: dict[str, dict] = {}
for pid, agg in _provider_agg.items():
    cc = agg["claim_count"]
    provider_summary[pid] = {
        "claim_count": cc,
        "premium_rate": int(round(100 * agg["premium_count"] / cc)) if cc else 0,
        "total_billed": int(round(agg["total_billed"])),
    }

explanation_cache: dict[str, str] = {}

client = anthropic.Anthropic()

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Insurance Fraud Detection POC")


PROMPT_TEMPLATE = """You are a senior fraud analyst at a vision insurance company reviewing
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
assessment. Do not speculate beyond the data provided."""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    total_claims = len(claims)
    flagged = [c for c in claims.values() if c.get("triggered")]
    flagged_count = len(flagged)
    dollars_at_risk = int(round(sum(c.get("billed_amount", 0) or 0 for c in flagged)))

    fraud_claims = [c for c in claims.values() if c.get("is_fraud")]
    if fraud_claims:
        detected = sum(1 for c in fraud_claims if c.get("triggered"))
        detection_rate = detected / len(fraud_claims)
    else:
        detection_rate = 0.0

    flagged_sorted = sorted(flagged, key=lambda c: c.get("risk_score", 0), reverse=True)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "total_claims": total_claims,
            "flagged_count": flagged_count,
            "dollars_at_risk": dollars_at_risk,
            "detection_rate": detection_rate,
            "flagged_claims": flagged_sorted,
        },
    )


@app.get("/explain/{claim_id}", response_class=HTMLResponse)
def explain(claim_id: str):
    claim = claims.get(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")

    if claim_id in explanation_cache:
        return HTMLResponse(content=explanation_cache[claim_id])

    psum = provider_summary.get(
        claim["provider_id"],
        {"claim_count": 0, "premium_rate": 0, "total_billed": 0},
    )

    prompt = PROMPT_TEMPLATE.format(
        claim_id=claim["claim_id"],
        provider_id=claim["provider_id"],
        provider_state=claim.get("provider_state", ""),
        member_id=claim.get("member_id", ""),
        service_date=claim.get("service_date", ""),
        exam_cpt=claim.get("exam_cpt", ""),
        lens_type=claim.get("lens_type", ""),
        billed_amount=claim.get("billed_amount", 0),
        rule_reason=claim.get("rule_reason", ""),
        provider_claim_count=psum["claim_count"],
        provider_premium_rate=psum["premium_rate"],
        provider_total_billed=psum["total_billed"],
    )

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=300,
        temperature=0.3,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text

    html = (
        '<div class="bg-amber-50 border-l-4 border-amber-400 p-4 my-2 rounded">'
        '<div class="text-xs uppercase tracking-wide text-amber-700 mb-1">AI Fraud Analyst Review</div>'
        f'<p class="text-gray-800">{text}</p>'
        '</div>'
    )

    explanation_cache[claim_id] = html
    return HTMLResponse(content=html)

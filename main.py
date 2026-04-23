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
PROMPTS_DIR = BASE_DIR / "prompts"

# Load claims at import time
with open(DATA_PATH) as f:
    _claims_list = json.load(f)

claims: dict[str, dict] = {c["claim_id"]: c for c in _claims_list}

LENS_TYPES = ("single", "bifocal", "progressive", "premium_progressive")

# Precompute provider summary (including per-lens counts)
_provider_agg: dict[str, dict] = defaultdict(
    lambda: {
        "claim_count": 0,
        "total_billed": 0.0,
        "lens_counts": {lt: 0 for lt in LENS_TYPES},
    }
)
for c in _claims_list:
    pid = c["provider_id"]
    _provider_agg[pid]["claim_count"] += 1
    _provider_agg[pid]["total_billed"] += c.get("billed_amount", 0) or 0
    lt = c.get("lens_type")
    if lt in _provider_agg[pid]["lens_counts"]:
        _provider_agg[pid]["lens_counts"][lt] += 1

provider_summary: dict[str, dict] = {}
for pid, agg in _provider_agg.items():
    cc = agg["claim_count"]
    lens_pct = {
        lt: (100 * agg["lens_counts"][lt] / cc) if cc else 0 for lt in LENS_TYPES
    }
    provider_summary[pid] = {
        "claim_count": cc,
        "premium_rate": int(round(lens_pct["premium_progressive"])),
        "total_billed": int(round(agg["total_billed"])),
        "lens_pct": lens_pct,
        "lens_counts": dict(agg["lens_counts"]),
    }

# Population lens distribution across all 500 claims
_pop_counts = {lt: 0 for lt in LENS_TYPES}
for c in _claims_list:
    lt = c.get("lens_type")
    if lt in _pop_counts:
        _pop_counts[lt] += 1
_total = sum(_pop_counts.values()) or 1
population_lens_pct = {lt: 100 * _pop_counts[lt] / _total for lt in LENS_TYPES}

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


LENS_COLORS = {
    "single": ("bg-sky-400", "text-sky-700"),
    "bifocal": ("bg-indigo-400", "text-indigo-700"),
    "progressive": ("bg-emerald-400", "text-emerald-700"),
    "premium_progressive": ("bg-rose-500", "text-rose-700"),
}


def _render_stacked_bar(pct_by_lens: dict[str, float]) -> str:
    """Horizontal stacked bar, 100% wide. Highlights premium_progressive."""
    segs = []
    for lt in LENS_TYPES:
        pct = pct_by_lens.get(lt, 0)
        if pct <= 0:
            continue
        bg, _ = LENS_COLORS[lt]
        segs.append(
            f'<div class="{bg} h-5 flex items-center justify-center text-[10px] '
            f'text-white font-semibold" style="width: {pct:.1f}%" '
            f'title="{lt}: {pct:.1f}%">'
            f'{(f"{pct:.0f}%" if pct >= 10 else "")}</div>'
        )
    return (
        '<div class="flex w-full rounded overflow-hidden border border-slate-200">'
        + "".join(segs)
        + "</div>"
    )


def _render_lens_legend() -> str:
    items = []
    labels = {
        "single": "Single",
        "bifocal": "Bifocal",
        "progressive": "Progressive",
        "premium_progressive": "Premium Progressive (upcoded)",
    }
    for lt in LENS_TYPES:
        bg, _ = LENS_COLORS[lt]
        items.append(
            f'<span class="flex items-center gap-1.5"><span class="inline-block '
            f'w-3 h-3 rounded-sm {bg}"></span>{labels[lt]}</span>'
        )
    return (
        '<div class="flex flex-wrap gap-4 text-xs text-slate-600 mt-2">'
        + "".join(items)
        + "</div>"
    )


def _render_provider_profile(provider_id: str, current_claim_id: str) -> str:
    """Bar chart: this provider vs population. + claim table with highlight."""
    psum = provider_summary.get(provider_id)
    if not psum:
        return ""

    this_bar = _render_stacked_bar(psum["lens_pct"])
    pop_bar = _render_stacked_bar(population_lens_pct)

    # Provider's claims (all), newest first, up to 20
    p_claims = [c for c in claims.values() if c["provider_id"] == provider_id]
    p_claims.sort(key=lambda c: c.get("service_date", ""), reverse=True)
    p_claims = p_claims[:20]

    rows = []
    for c in p_claims:
        is_current = c["claim_id"] == current_claim_id
        row_cls = (
            "bg-amber-100 ring-2 ring-amber-400"
            if is_current
            else "hover:bg-slate-50"
        )
        marker = (
            '<span class="text-amber-700 font-bold">▸ THIS CLAIM</span>'
            if is_current
            else ""
        )
        lens_bg, lens_text = LENS_COLORS.get(
            c.get("lens_type", ""), ("bg-slate-300", "text-slate-700")
        )
        rows.append(
            f'<tr class="{row_cls}">'
            f'<td class="px-3 py-1.5 font-mono text-[11px] text-slate-500">{c["claim_id"][:8]}</td>'
            f'<td class="px-3 py-1.5 text-slate-700">{c.get("service_date", "")}</td>'
            f'<td class="px-3 py-1.5"><span class="inline-block px-1.5 py-0.5 rounded text-white text-[11px] font-medium {lens_bg}">{c.get("lens_type", "")}</span></td>'
            f'<td class="px-3 py-1.5 text-slate-700">{c.get("exam_cpt", "")}</td>'
            f'<td class="px-3 py-1.5 text-right font-mono text-slate-700">${int(c.get("billed_amount", 0) or 0):,}</td>'
            f'<td class="px-3 py-1.5 text-right">{marker}</td>'
            f"</tr>"
        )
    table_rows = "".join(rows)

    claim = claims[current_claim_id]
    member_id = claim.get("member_id", "")
    provider_state = claim.get("provider_state", "")

    return f'''
<!-- Panel 2: provider profile -->
<div class="bg-white border border-slate-200 rounded p-4 my-3 shadow-sm">
  <div class="flex items-start justify-between mb-3">
    <div>
      <div class="text-xs uppercase tracking-wide text-slate-500">Provider profile</div>
      <div class="text-base font-semibold text-slate-800 mt-0.5">
        Provider {provider_id} <span class="text-slate-400 font-normal">({provider_state})</span>
      </div>
      <div class="text-xs text-slate-500 mt-0.5">
        {psum["claim_count"]} claims in period · ${psum["total_billed"]:,} total billed
        · <span class="text-rose-700 font-semibold">{psum["premium_rate"]}% premium progressive</span>
        <span class="text-slate-400">(population mean: {population_lens_pct["premium_progressive"]:.0f}%)</span>
      </div>
    </div>
    <div class="flex gap-2 text-xs">
      <a href="/provider/{provider_id}" class="text-blue-700 hover:text-blue-900 font-medium whitespace-nowrap">View provider →</a>
      <span class="text-slate-300">|</span>
      <a href="/member/{member_id}" class="text-blue-700 hover:text-blue-900 font-medium whitespace-nowrap">View member →</a>
    </div>
  </div>

  <!-- Stacked bar comparison -->
  <div class="space-y-2 mb-3">
    <div>
      <div class="text-[11px] text-slate-500 font-semibold uppercase tracking-wide mb-1">This provider ({provider_id})</div>
      {this_bar}
    </div>
    <div>
      <div class="text-[11px] text-slate-500 font-semibold uppercase tracking-wide mb-1">Population (all 30 providers)</div>
      {pop_bar}
    </div>
    {_render_lens_legend()}
  </div>

  <div class="text-xs text-slate-500 italic mb-2">The rule fired because this provider bills <span class="font-semibold text-rose-700">premium_progressive at {psum["premium_rate"]}%</span> — far above the population mean of {population_lens_pct["premium_progressive"]:.0f}%.</div>

  <!-- Claim-level table -->
  <div class="mt-3">
    <div class="text-[11px] text-slate-500 font-semibold uppercase tracking-wide mb-1">Recent claims from this provider (up to 20, newest first)</div>
    <div class="overflow-hidden rounded border border-slate-200">
      <table class="w-full text-xs">
        <thead class="bg-slate-50 text-slate-600 text-left">
          <tr>
            <th class="px-3 py-1.5 font-medium">Claim</th>
            <th class="px-3 py-1.5 font-medium">Date</th>
            <th class="px-3 py-1.5 font-medium">Lens</th>
            <th class="px-3 py-1.5 font-medium">CPT</th>
            <th class="px-3 py-1.5 font-medium text-right">Billed</th>
            <th class="px-3 py-1.5 font-medium text-right"></th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 bg-white">{table_rows}</tbody>
      </table>
    </div>
    <div class="text-[11px] text-slate-400 mt-1.5">The highlighted row is the claim under review. Scan the Lens column — a provider legitimately doing premium_progressive should show varied lens mix; upcoding shows dominance of the rose-red segment.</div>
  </div>
</div>
'''


def _build_explain_html(claim_id: str) -> str:
    """Return the full HTML snippet (narrative + provider profile) for a claim.
    Used by /explain (HTMX) and /demo (pre-loaded)."""
    if claim_id in explanation_cache:
        return explanation_cache[claim_id]

    claim = claims[claim_id]
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

    narrative = (
        '<div class="bg-amber-50 border-l-4 border-amber-400 p-4 my-2 rounded">'
        '<div class="text-xs uppercase tracking-wide text-amber-700 mb-1 font-semibold">AI Fraud Analyst Review</div>'
        f'<p class="text-gray-800">{text}</p>'
        "</div>"
    )
    profile = _render_provider_profile(claim["provider_id"], claim_id)
    html = narrative + profile
    explanation_cache[claim_id] = html
    return html


@app.get("/explain/{claim_id}", response_class=HTMLResponse)
def explain(claim_id: str):
    if claim_id not in claims:
        raise HTTPException(status_code=404, detail="Claim not found")
    return HTMLResponse(content=_build_explain_html(claim_id))


def _shared_header(active: str = "") -> str:
    """Compact header for the detail + framing pages."""
    links = [
        ("dashboard", "/", "Dashboard"),
        ("demo", "/demo", "Guided Demo"),
        ("product", "/product", "Product"),
        ("prompts", "/prompts", "Prompts"),
        ("flow", "/flow", "Flow"),
    ]
    active_cls = "text-white border-b-2 border-teal-300 pb-1 font-medium"
    idle_cls = "text-teal-100 hover:text-white pb-1"
    nav_items = "".join(
        f'<a href="{href}" class="{active_cls if active == key else idle_cls}">{label}</a>'
        for key, href, label in links
    )
    return f'''<header class="bg-[#003b71] text-white shadow">
  <div class="max-w-7xl mx-auto px-6 pt-6 pb-2 flex items-center justify-between">
    <h1 class="text-2xl font-semibold">Insurance Fraud Detection POC</h1>
    <span class="text-sm text-teal-200 uppercase tracking-wider">Fraud, Waste &amp; Abuse Prototype</span>
  </div>
  <nav class="max-w-7xl mx-auto px-6 pb-3 flex gap-5 text-sm flex-wrap">
    {nav_items}
  </nav>
</header>'''


def _claim_detail_table(claim_list: list[dict], highlight_id: str = "") -> str:
    rows = []
    for c in claim_list:
        is_highlight = c["claim_id"] == highlight_id
        row_cls = (
            "bg-amber-100 ring-2 ring-amber-400"
            if is_highlight
            else "hover:bg-slate-50"
        )
        lens_bg, _ = LENS_COLORS.get(c.get("lens_type", ""), ("bg-slate-300", ""))
        triggered_badge = (
            '<span class="inline-block px-1.5 py-0.5 rounded bg-red-100 text-red-700 text-[10px] font-semibold">FLAGGED</span>'
            if c.get("triggered")
            else '<span class="text-slate-300 text-[10px]">—</span>'
        )
        rows.append(
            f'<tr class="{row_cls}">'
            f'<td class="px-3 py-2 font-mono text-[11px] text-slate-500">{c["claim_id"][:8]}</td>'
            f'<td class="px-3 py-2 text-slate-700">{c.get("service_date", "")}</td>'
            f'<td class="px-3 py-2"><a href="/provider/{c["provider_id"]}" class="text-blue-700 hover:text-blue-900">{c["provider_id"]}</a></td>'
            f'<td class="px-3 py-2"><a href="/member/{c.get("member_id", "")}" class="text-blue-700 hover:text-blue-900">{c.get("member_id", "")}</a></td>'
            f'<td class="px-3 py-2"><span class="inline-block px-1.5 py-0.5 rounded text-white text-[11px] font-medium {lens_bg}">{c.get("lens_type", "")}</span></td>'
            f'<td class="px-3 py-2 text-slate-700">{c.get("exam_cpt", "")}</td>'
            f'<td class="px-3 py-2 text-right font-mono text-slate-700">${int(c.get("billed_amount", 0) or 0):,}</td>'
            f'<td class="px-3 py-2 text-center">{triggered_badge}</td>'
            f"</tr>"
        )
    return (
        '<div class="overflow-hidden rounded-lg shadow bg-white">'
        '<table class="w-full text-sm">'
        '<thead class="bg-slate-100 text-slate-600 text-left"><tr>'
        '<th class="px-3 py-2 font-medium">Claim</th>'
        '<th class="px-3 py-2 font-medium">Date</th>'
        '<th class="px-3 py-2 font-medium">Provider</th>'
        '<th class="px-3 py-2 font-medium">Member</th>'
        '<th class="px-3 py-2 font-medium">Lens</th>'
        '<th class="px-3 py-2 font-medium">CPT</th>'
        '<th class="px-3 py-2 font-medium text-right">Billed</th>'
        '<th class="px-3 py-2 font-medium text-center">Status</th>'
        "</tr></thead>"
        '<tbody class="divide-y divide-slate-100">'
        + "".join(rows)
        + "</tbody></table></div>"
    )


@app.get("/provider/{provider_id}", response_class=HTMLResponse)
def provider_page(provider_id: str):
    psum = provider_summary.get(provider_id)
    if not psum:
        raise HTTPException(status_code=404, detail="Provider not found")

    p_claims = [c for c in claims.values() if c["provider_id"] == provider_id]
    p_claims.sort(key=lambda c: c.get("service_date", ""), reverse=True)
    flagged_count = sum(1 for c in p_claims if c.get("triggered"))
    any_fraud = sum(1 for c in p_claims if c.get("is_fraud"))

    this_bar = _render_stacked_bar(psum["lens_pct"])
    pop_bar = _render_stacked_bar(population_lens_pct)

    body = f'''<!DOCTYPE html>
<html><head>
  <title>Provider {provider_id} — Insurance Fraud POC</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
{_shared_header("dashboard")}
<main class="max-w-7xl mx-auto p-6">
  <div class="mb-4 flex items-start justify-between">
    <div>
      <div class="text-xs uppercase tracking-wide text-slate-500">Provider profile</div>
      <h2 class="text-2xl font-semibold text-slate-800">Provider {provider_id}</h2>
      <div class="text-sm text-slate-600 mt-1">
        {psum["claim_count"]} claims · ${psum["total_billed"]:,} total billed
        · <span class="text-rose-700 font-semibold">{psum["premium_rate"]}% premium progressive</span>
        · {flagged_count} flagged
        {f'· <span class="text-red-700 font-semibold">{any_fraud} seeded fraud</span>' if any_fraud else ""}
      </div>
    </div>
    <a href="/" class="text-sm text-blue-700 hover:text-blue-900">← Back to dashboard</a>
  </div>

  <section class="bg-white rounded-lg shadow p-5 mb-6">
    <div class="text-sm font-semibold text-slate-700 mb-3">Lens-mix comparison</div>
    <div class="space-y-3">
      <div>
        <div class="text-[11px] text-slate-500 font-semibold uppercase tracking-wide mb-1">This provider ({provider_id})</div>
        {this_bar}
      </div>
      <div>
        <div class="text-[11px] text-slate-500 font-semibold uppercase tracking-wide mb-1">Population mean</div>
        {pop_bar}
      </div>
      {_render_lens_legend()}
    </div>
  </section>

  <section>
    <div class="text-sm font-semibold text-slate-700 mb-3">All claims from this provider ({len(p_claims)})</div>
    {_claim_detail_table(p_claims)}
  </section>
</main></body></html>'''
    return HTMLResponse(content=body)


@app.get("/member/{member_id}", response_class=HTMLResponse)
def member_page(member_id: str):
    m_claims = [c for c in claims.values() if c.get("member_id") == member_id]
    if not m_claims:
        raise HTTPException(status_code=404, detail="Member not found")

    m_claims.sort(key=lambda c: c.get("service_date", ""), reverse=True)
    total_billed = int(round(sum(c.get("billed_amount", 0) or 0 for c in m_claims)))
    flagged_count = sum(1 for c in m_claims if c.get("triggered"))
    distinct_providers = sorted({c["provider_id"] for c in m_claims})

    body = f'''<!DOCTYPE html>
<html><head>
  <title>Member {member_id} — Insurance Fraud POC</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
{_shared_header("dashboard")}
<main class="max-w-7xl mx-auto p-6">
  <div class="mb-4 flex items-start justify-between">
    <div>
      <div class="text-xs uppercase tracking-wide text-slate-500">Member history</div>
      <h2 class="text-2xl font-semibold text-slate-800">Member {member_id}</h2>
      <div class="text-sm text-slate-600 mt-1">
        {len(m_claims)} claims across {len(distinct_providers)} provider{"s" if len(distinct_providers) != 1 else ""}
        · ${total_billed:,} total billed · {flagged_count} flagged
      </div>
    </div>
    <a href="/" class="text-sm text-blue-700 hover:text-blue-900">← Back to dashboard</a>
  </div>

  <section>
    <div class="text-sm font-semibold text-slate-700 mb-3">All claims for this member (newest first)</div>
    {_claim_detail_table(m_claims)}
  </section>
</main></body></html>'''
    return HTMLResponse(content=body)


@app.get("/prompts", response_class=HTMLResponse)
def prompts_page(request: Request):
    build_prompts = []
    for name, label in [
        ("phase-1-data.md", "Phase 1 — Data generation"),
        ("phase-2-detect.md", "Phase 2 — Detection"),
        ("phase-3-app.md", "Phase 3 — App (halt-and-escalate)"),
    ]:
        path = PROMPTS_DIR / name
        build_prompts.append(
            {"filename": name, "label": label, "content": path.read_text()}
        )
    return templates.TemplateResponse(
        request,
        "prompts.html",
        {"build_prompts": build_prompts, "explain_prompt": PROMPT_TEMPLATE},
    )


@app.get("/flow", response_class=HTMLResponse)
def flow_page(request: Request):
    return templates.TemplateResponse(request, "flow.html", {})


def _pick_demo_claims() -> list[dict]:
    """Select 3 contrasting flagged claims for the guided demo."""
    triggered = [c for c in claims.values() if c.get("triggered")]

    def best(filter_fn, fallback_fn=None):
        matches = [c for c in triggered if filter_fn(c)]
        matches.sort(key=lambda c: c.get("risk_score", 0), reverse=True)
        if not matches and fallback_fn:
            matches = [c for c in triggered if fallback_fn(c)]
            matches.sort(key=lambda c: c.get("risk_score", 0), reverse=True)
        return matches[0] if matches else None

    clear_fraud = best(
        lambda c: c.get("is_fraud") and c.get("lens_type") == "premium_progressive"
    )
    correct_fp = best(
        lambda c: (not c.get("is_fraud")) and c.get("lens_type") == "bifocal",
        fallback_fn=lambda c: (not c.get("is_fraud"))
        and c.get("lens_type") in ("single", "bifocal", "progressive"),
    )
    ambiguous = best(
        lambda c: (not c.get("is_fraud"))
        and c.get("lens_type") == "premium_progressive",
        fallback_fn=lambda c: not c.get("is_fraud"),
    )

    picks = []
    seen: set[str] = set()
    for label, subtitle, claim in [
        (
            "Clear upcoding fraud",
            "Seeded fraudster provider · this specific claim is a premium progressive. Rule fires, narrative should agree.",
            clear_fraud,
        ),
        (
            "Correct false positive",
            "Flagged because the provider has an upcoding pattern overall, but this claim is a bifocal — not the thing the rule is targeting. The narrative should catch this.",
            correct_fp,
        ),
        (
            "Ambiguous case",
            "Premium progressive at a flagged provider, but this claim was not seeded as fraud. Could be legitimate premium lens demand, could be upcoding on a real need. The narrative has to reason carefully.",
            ambiguous,
        ),
    ]:
        if claim is None or claim["claim_id"] in seen:
            continue
        seen.add(claim["claim_id"])
        picks.append({"label": label, "subtitle": subtitle, "claim": claim})
    return picks


@app.get("/demo", response_class=HTMLResponse)
def demo_page():
    picks = _pick_demo_claims()

    sections = []
    for i, p in enumerate(picks, 1):
        c = p["claim"]
        explain_html = _build_explain_html(c["claim_id"])
        risk = int(round(c.get("risk_score", 0)))
        risk_cls = (
            "bg-red-100 text-red-700"
            if risk > 80
            else "bg-amber-100 text-amber-700"
            if risk > 60
            else "bg-yellow-100 text-yellow-700"
        )
        sections.append(f'''
<section class="mb-10">
  <div class="mb-3 flex items-baseline gap-3 flex-wrap">
    <span class="text-xs font-semibold uppercase tracking-widest text-teal-700 bg-teal-50 px-2 py-0.5 rounded">Case {i} of {len(picks)}</span>
    <h2 class="text-xl font-semibold text-slate-800">{p["label"]}</h2>
    <span class="inline-block px-2 py-0.5 rounded font-mono text-xs {risk_cls}">risk {risk}</span>
  </div>
  <p class="text-sm text-slate-600 mb-3 max-w-3xl">{p["subtitle"]}</p>
  <div class="bg-white rounded-lg shadow p-4 text-xs text-slate-600 mb-3">
    <span class="font-semibold text-slate-700">Claim:</span>
    <span class="font-mono">{c["claim_id"][:8]}</span> ·
    <span class="font-semibold">Provider:</span>
    <a href="/provider/{c["provider_id"]}" class="text-blue-700 hover:text-blue-900 font-mono">{c["provider_id"]}</a> ·
    <span class="font-semibold">Member:</span>
    <a href="/member/{c.get("member_id", "")}" class="text-blue-700 hover:text-blue-900 font-mono">{c.get("member_id", "")}</a> ·
    <span class="font-semibold">Lens:</span> {c.get("lens_type", "")} ·
    <span class="font-semibold">Billed:</span> ${int(c.get("billed_amount", 0) or 0):,} ·
    <span class="font-semibold">Seeded fraud:</span> <span class="{"text-red-700 font-semibold" if c.get("is_fraud") else "text-slate-500"}">{"yes" if c.get("is_fraud") else "no"}</span>
  </div>
  {explain_html}
</section>''')

    body = f'''<!DOCTYPE html>
<html><head>
  <title>Guided Demo — Insurance Fraud POC</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
{_shared_header("demo")}
<main class="max-w-7xl mx-auto p-6">
  <div class="mb-8 max-w-3xl">
    <div class="text-xs uppercase tracking-wide text-slate-500">3-minute guided demo</div>
    <h2 class="text-2xl font-semibold text-slate-800 mt-1">Three contrasting claims, three judgments</h2>
    <p class="text-sm text-slate-600 mt-2 leading-relaxed">
      The point of this POC is not that the rule catches fraud — a two-line statistical rule does that. The point is what happens <em>after</em> a claim is flagged. An investigator reviews each flag, decides fraud/error/false-positive, and files recovery. That review is where 10–15 minutes per claim goes today.
    </p>
    <p class="text-sm text-slate-600 mt-2 leading-relaxed">
      Below are three flagged claims chosen to show the range of Claude's judgment: a clear fraud, a correct false-positive (where the rule fired on a claim that actually isn't the thing it was looking for), and a genuinely ambiguous case. Each one has the full three-panel reviewer view: narrative, pattern viz, evidence.
    </p>
  </div>

  {"".join(sections)}

  <div class="mt-12 bg-slate-100 rounded-lg p-5 text-sm text-slate-700 max-w-3xl">
    <div class="font-semibold text-slate-800 mb-1">What to notice</div>
    <ul class="list-disc pl-5 space-y-1 leading-relaxed">
      <li>The narrative stays measured across all three — it doesn't call everything fraud. That framing (three-category triage) is a product decision, not a model behavior.</li>
      <li>The bar chart and claim table give the investigator <em>their own</em> way to verify the narrative. That's what makes this auditable in a regulated industry.</li>
      <li>The same pattern (narrative + aggregate viz + evidence table) generalizes to other FWA types — see the <a href="/product" class="text-blue-700 hover:text-blue-900">Product page</a> for the full landscape.</li>
    </ul>
  </div>
</main></body></html>'''
    return HTMLResponse(content=body)


@app.get("/product", response_class=HTMLResponse)
def product_page():
    body = f'''<!DOCTYPE html>
<html><head>
  <title>Product framing — Insurance Fraud POC</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
{_shared_header("product")}
<main class="max-w-4xl mx-auto p-6 prose prose-slate">
  <div class="mb-2">
    <div class="text-xs uppercase tracking-wide text-slate-500">Product framing</div>
    <h2 class="text-3xl font-semibold text-slate-800 mt-1">Where this POC fits</h2>
  </div>

  <section class="mt-6 bg-[#003b71] text-white rounded-lg p-6 shadow">
    <div class="text-xs uppercase tracking-widest text-teal-200 font-semibold">The product claim</div>
    <p class="mt-2 text-lg leading-relaxed">
      A vision-insurance fraud team reviews ~50 flags per investigator per day. Each triage takes 10–15 minutes of reading provider history, member history, and coding patterns to decide <em>fraud / documentation error / false positive</em>. An LLM narrative front-loaded onto every flag cuts that first-pass triage to ~2 minutes and concentrates human time on the ambiguous cases.
    </p>
    <p class="mt-3 text-sm text-teal-100">
      The detection rule is not the product. The <strong>reviewer workflow</strong> is.
    </p>
  </section>

  <section class="mt-8 grid grid-cols-2 gap-4">
    <div class="bg-white rounded-lg shadow p-5">
      <div class="text-xs uppercase tracking-wider text-emerald-700 font-semibold">What this is</div>
      <ul class="mt-2 text-sm text-slate-700 space-y-1 list-disc pl-5">
        <li>Rule-based detection on one FWA pattern (provider-level lens upcoding)</li>
        <li>LLM narrative layer per flagged claim — triage-first framing: fraud / doc error / false positive</li>
        <li>Three-panel reviewer UI: narrative + aggregate viz + evidence table</li>
        <li>Pivotable views: provider profile, member history</li>
      </ul>
    </div>
    <div class="bg-white rounded-lg shadow p-5">
      <div class="text-xs uppercase tracking-wider text-rose-700 font-semibold">What this is not</div>
      <ul class="mt-2 text-sm text-slate-700 space-y-1 list-disc pl-5">
        <li>Not a fraud-detection model (a two-line statistical rule does the detection)</li>
        <li>Not usable without the rule — LLMs don't replace auditable logic in regulated industries</li>
        <li>Not evidence of generalization — synthetic data validates the pipeline, not model behavior on real claims</li>
        <li>Not a production system — no authn, no audit log, no retraining loop</li>
      </ul>
    </div>
  </section>

  <section class="mt-10">
    <h3 class="text-xl font-semibold text-slate-800">Where the LLM leverage actually lives</h3>
    <p class="mt-2 text-slate-700 leading-relaxed">
      The instinct in most AI pitches is "let the LLM decide fraud/not-fraud." That's the wrong split of labor. Rules are auditable, cheap, and stable; handing binary fraud decisions to a generative model sacrifices all three without gaining much.
    </p>
    <p class="mt-2 text-slate-700 leading-relaxed">
      The actual leverage is on the <strong>triage layer</strong> between "flagged by a rule" and "human files a recovery." That's where human hours compound — investigators reading the same provider history fifty times, doing the same coding comparisons, writing the same one-paragraph summaries. An LLM that produces a measured triage narrative with the specific pattern cited is a force multiplier there.
    </p>
    <div class="mt-4 bg-amber-50 border-l-4 border-amber-400 p-4 rounded text-sm text-amber-900">
      <strong>The framing:</strong> rules for the gate, LLMs for the triage.
      The prompt taxonomy — likely fraud / doc error / false positive — is a product decision, not a generative flourish. It maps directly onto the investigator's next action.
    </div>
  </section>

  <section class="mt-10">
    <h3 class="text-xl font-semibold text-slate-800">FWA patterns — the full landscape</h3>
    <p class="mt-2 text-slate-700">
      This POC implements the first row. The same three-panel reviewer pattern generalizes to the others — the <em>aggregate viz</em> is the panel that changes per pattern; narrative and evidence stay consistent.
    </p>
    <div class="mt-4 overflow-hidden rounded-lg shadow bg-white">
      <table class="w-full text-sm">
        <thead class="bg-slate-100 text-slate-600 text-left">
          <tr>
            <th class="px-3 py-2 font-medium">Pattern</th>
            <th class="px-3 py-2 font-medium">Forensic shape</th>
            <th class="px-3 py-2 font-medium">Best aggregate viz</th>
            <th class="px-3 py-2 font-medium">In this POC</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 text-slate-700">
          <tr><td class="px-3 py-2 font-semibold">Upcoding (lens)</td><td class="px-3 py-2">distribution</td><td class="px-3 py-2">stacked bar: provider vs population</td><td class="px-3 py-2 text-emerald-700 font-semibold">✓ demoed</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Phantom add-ons</td><td class="px-3 py-2">distribution</td><td class="px-3 py-2">add-on rate vs peers</td><td class="px-3 py-2 text-slate-500">same pattern, new column</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Exam CPT upcoding</td><td class="px-3 py-2">distribution</td><td class="px-3 py-2">CPT mix vs peers</td><td class="px-3 py-2 text-slate-500">same pattern</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Frequency abuse</td><td class="px-3 py-2">temporal</td><td class="px-3 py-2">member timeline with gap annotations</td><td class="px-3 py-2 text-slate-400">different viz</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Medical necessity</td><td class="px-3 py-2">Rx-claim join</td><td class="px-3 py-2">side-by-side claim vs Rx</td><td class="px-3 py-2 text-slate-400">needs Rx data</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Provider rings</td><td class="px-3 py-2">graph</td><td class="px-3 py-2">node-link of shared members</td><td class="px-3 py-2 text-slate-400">different viz</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Member shopping</td><td class="px-3 py-2">multi-provider timeline</td><td class="px-3 py-2">swimlane per provider</td><td class="px-3 py-2 text-slate-400">different viz</td></tr>
          <tr><td class="px-3 py-2 font-semibold">Chart-note mismatch</td><td class="px-3 py-2">document</td><td class="px-3 py-2">annotated note + billed lines</td><td class="px-3 py-2 text-slate-400">where LLM wins biggest</td></tr>
        </tbody>
      </table>
    </div>
    <p class="mt-3 text-sm text-slate-600 italic">
      The last row is where AI produces the most leverage per dollar. Investigators reading 50 encounter notes × 5 pages each is where days disappear. An LLM pre-reading those notes and highlighting inconsistencies with the billed line items would extend this POC's pattern from statistical flags to document-heavy review.
    </p>
  </section>

  <section class="mt-10">
    <h3 class="text-xl font-semibold text-slate-800">The three-panel reviewer pattern</h3>
    <p class="mt-2 text-slate-700 leading-relaxed">
      Every flagged claim gets three panels in the same order. This is the product insight — not the rule, not the prompt, the <em>UI contract</em>:
    </p>
    <div class="mt-4 grid grid-cols-3 gap-3">
      <div class="bg-white rounded-lg shadow p-4 border-t-4 border-amber-400">
        <div class="text-xs uppercase tracking-wider text-amber-700 font-semibold">Panel 1</div>
        <div class="font-semibold text-slate-800 mt-1">LLM narrative</div>
        <p class="text-xs text-slate-600 mt-2 leading-relaxed">Triage judgment in plain English. Shape stays the same across every FWA pattern.</p>
      </div>
      <div class="bg-white rounded-lg shadow p-4 border-t-4 border-blue-500">
        <div class="text-xs uppercase tracking-wider text-blue-700 font-semibold">Panel 2</div>
        <div class="font-semibold text-slate-800 mt-1">Aggregate viz</div>
        <p class="text-xs text-slate-600 mt-2 leading-relaxed">Shape <em>changes</em> per pattern — bar chart, timeline, graph, side-by-side. Matches the anomaly's forensic shape.</p>
      </div>
      <div class="bg-white rounded-lg shadow p-4 border-t-4 border-emerald-500">
        <div class="text-xs uppercase tracking-wider text-emerald-700 font-semibold">Panel 3</div>
        <div class="font-semibold text-slate-800 mt-1">Evidence drill-down</div>
        <p class="text-xs text-slate-600 mt-2 leading-relaxed">Claim-level table with the current row highlighted. The audit trail the investigator would cite. "Compared to what?" is first-class: provider, member, peer group.</p>
      </div>
    </div>
  </section>

  <section class="mt-10">
    <h3 class="text-xl font-semibold text-slate-800">What production would extend to</h3>
    <ul class="mt-2 text-slate-700 space-y-2 list-disc pl-5 leading-relaxed">
      <li><strong>Real claim data + Rx + member history.</strong> The rule needs real distributions; the narrative needs real context.</li>
      <li><strong>Held-out fraud set for out-of-distribution evaluation.</strong> Synthetic data validates the pipeline, not the model's behavior on unfamiliar fraud.</li>
      <li><strong>Audit logging.</strong> Every LLM call recorded: input, output, model, timestamp. Regulated industries require replay.</li>
      <li><strong>Investigator feedback loop.</strong> Thumbs up/down on narratives, fed back into prompt evaluation. Not fine-tuning — prompt iteration driven by real judgment.</li>
      <li><strong>Second fraud pattern</strong> — phantom add-ons or frequency abuse — to prove the three-panel frame generalizes.</li>
      <li><strong>Chart-note review.</strong> The biggest LLM unlock — extending the same pattern to document-heavy cases.</li>
    </ul>
  </section>

  <section class="mt-10 mb-10 bg-slate-900 text-slate-100 rounded-lg p-6">
    <div class="text-xs uppercase tracking-widest text-amber-300 font-semibold">Honest caveats</div>
    <ul class="mt-2 space-y-1 text-sm leading-relaxed list-disc pl-5">
      <li>500 synthetic claims, 28 seeded fraud (5.6%). Structural plausibility, not real-world fidelity.</li>
      <li>Detection recall on seeded fraud: 1.00. Precision: 0.57. Precision is reported, not gated — low precision is expected at this scale and with one rule.</li>
      <li>The LLM narrative is not a substitute for the rule. If the rule is wrong, the narrative dutifully explains why the rule is wrong — which is the correct behavior but not a magic bullet.</li>
      <li>No authentication, no rate limiting, no cost controls. This is a POC; it costs ~$0.01 per /explain call at current pricing.</li>
    </ul>
  </section>
</main></body></html>'''
    return HTMLResponse(content=body)

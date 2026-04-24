"""Insurance Fraud Detection POC — FastAPI app."""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import statistics as _stats
import subprocess
import threading
import uuid
from collections import defaultdict
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

load_dotenv()

BASE_DIR = Path(__file__).parent
DATA_PATH = BASE_DIR / "data" / "claims_scored.json"
PROMPTS_DIR = BASE_DIR / "prompts"

LENS_TYPES = ("single", "bifocal", "progressive", "premium_progressive")

# Mutable module-level state, refreshed by _reload_state()
claims: dict[str, dict] = {}
provider_summary: dict[str, dict] = {}
population_lens_pct: dict[str, float] = {}
population_premium_mean: float = 0.0
population_premium_stdev: float = 0.0
explanation_cache: dict[str, str] = {}
triage_by_claim: dict[str, str] = {}
_CLEAN_PROVIDER_ID: str = ""
_FRAUDSTER_PROVIDER_ID: str = ""
_reload_lock = threading.Lock()
_data_generation: int = 0  # bumped by _reload_state(); stale warmup threads exit when this changes


def _reload_state() -> None:
    """(Re)load claims + recompute all derived state. Safe to call repeatedly."""
    global claims, provider_summary, population_lens_pct
    global population_premium_mean, population_premium_stdev
    global _CLEAN_PROVIDER_ID, _FRAUDSTER_PROVIDER_ID
    global _data_generation

    with _reload_lock:
        _data_generation += 1
        with open(DATA_PATH) as f:
            claims_list = json.load(f)

        claims = {c["claim_id"]: c for c in claims_list}

        provider_agg: dict[str, dict] = defaultdict(
            lambda: {
                "claim_count": 0,
                "total_billed": 0.0,
                "lens_counts": {lt: 0 for lt in LENS_TYPES},
            }
        )
        for c in claims_list:
            pid = c["provider_id"]
            provider_agg[pid]["claim_count"] += 1
            provider_agg[pid]["total_billed"] += c.get("billed_amount", 0) or 0
            lt = c.get("lens_type")
            if lt in provider_agg[pid]["lens_counts"]:
                provider_agg[pid]["lens_counts"][lt] += 1

        ps: dict[str, dict] = {}
        for pid, agg in provider_agg.items():
            cc = agg["claim_count"]
            lens_pct = {
                lt: (100 * agg["lens_counts"][lt] / cc) if cc else 0
                for lt in LENS_TYPES
            }
            ps[pid] = {
                "claim_count": cc,
                "premium_rate": int(round(lens_pct["premium_progressive"])),
                "total_billed": int(round(agg["total_billed"])),
                "lens_pct": lens_pct,
                "lens_counts": dict(agg["lens_counts"]),
            }
        provider_summary = ps

        pop_counts = {lt: 0 for lt in LENS_TYPES}
        for c in claims_list:
            lt = c.get("lens_type")
            if lt in pop_counts:
                pop_counts[lt] += 1
        total = sum(pop_counts.values()) or 1
        population_lens_pct = {lt: 100 * pop_counts[lt] / total for lt in LENS_TYPES}

        rates = [
            s["lens_pct"]["premium_progressive"] / 100.0
            for s in provider_summary.values()
        ]
        population_premium_mean = _stats.fmean(rates) if rates else 0.0
        population_premium_stdev = _stats.pstdev(rates) if len(rates) > 1 else 0.0

        clean_candidates = sorted(
            [
                pid
                for pid, s in provider_summary.items()
                if s["claim_count"] >= 10 and s["premium_rate"] <= 20
            ],
            key=lambda p: provider_summary[p]["premium_rate"],
        )
        _CLEAN_PROVIDER_ID = (
            clean_candidates[0]
            if clean_candidates
            else next(iter(provider_summary), "")
        )
        _FRAUDSTER_PROVIDER_ID = (
            max(provider_summary.items(), key=lambda kv: kv[1]["premium_rate"])[0]
            if provider_summary
            else ""
        )

        # Fresh data invalidates prior triage + explanation cache
        explanation_cache.clear()
        triage_by_claim.clear()


DETECTION_THRESHOLD_SIGMA = 2.0  # must match detection.py's chosen threshold

_reload_state()

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

Output format (exactly):
  First line: TRIAGE: <one of: likely_fraud | doc_error | false_positive>
  Then 2-3 sentences of reasoning that cite the specific pattern
  driving your assessment. Do not speculate beyond the data provided.

Use these category definitions:
- likely_fraud      intentional upcoding or other clear fraud signal
- doc_error         miscoding or billing anomaly that warrants manual review but isn't clearly fraud
- false_positive    flag fired on provider pattern but this specific claim doesn't match the pattern"""


TRIAGE_VALUES = ("likely_fraud", "doc_error", "false_positive")
triage_by_claim: dict[str, str] = {}


def _parse_triage(text: str) -> tuple[str, str]:
    """Return (triage_category, remaining_narrative). Falls back to 'unknown'."""
    m = re.match(r"^\s*TRIAGE:\s*(\w+)\s*\n?", text)
    if m and m.group(1) in TRIAGE_VALUES:
        return m.group(1), text[m.end():].strip()
    # Fallback: scan first few lines for any keyword match
    lowered = text[:200].lower()
    for t in TRIAGE_VALUES:
        if t.replace("_", " ") in lowered or t in lowered:
            return t, text
    return "unknown", text


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

    # Triage summary counts (for a small dashboard status strip)
    triage_counts = {"likely_fraud": 0, "doc_error": 0, "false_positive": 0, "unknown": 0, "pending": 0}
    for c in flagged_sorted:
        t = triage_by_claim.get(c["claim_id"])
        if t is None:
            triage_counts["pending"] += 1
        else:
            triage_counts[t] = triage_counts.get(t, 0) + 1

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "total_claims": total_claims,
            "flagged_count": flagged_count,
            "dollars_at_risk": dollars_at_risk,
            "detection_rate": detection_rate,
            "flagged_claims": flagged_sorted,
            "triage_by_claim": triage_by_claim,
            "triage_counts": triage_counts,
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

    model_name = "claude-sonnet-4-5"
    max_tokens = 300
    temperature = 0.3

    response = client.messages.create(
        model=model_name,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = response.content[0].text
    triage, narrative_text = _parse_triage(raw_text)
    triage_by_claim[claim_id] = triage

    input_tokens = getattr(response.usage, "input_tokens", "?")
    output_tokens = getattr(response.usage, "output_tokens", "?")
    stop_reason = getattr(response, "stop_reason", "?")

    triage_labels = {
        "likely_fraud": ("Likely fraud", "bg-red-100 text-red-800 border-red-300"),
        "doc_error": ("Doc/coding error", "bg-amber-100 text-amber-800 border-amber-300"),
        "false_positive": ("False positive", "bg-emerald-100 text-emerald-800 border-emerald-300"),
        "unknown": ("Unclassified", "bg-slate-100 text-slate-700 border-slate-300"),
    }
    t_label, t_cls = triage_labels.get(triage, triage_labels["unknown"])
    triage_pill = (
        f'<span class="inline-block px-2 py-0.5 rounded text-xs font-semibold border {t_cls} mb-2">{t_label}</span>'
    )

    narrative = (
        '<div class="bg-amber-50 border-l-4 border-amber-400 p-4 my-2 rounded">'
        '<div class="text-xs uppercase tracking-wide text-amber-700 mb-1 font-semibold">AI Fraud Analyst Review</div>'
        f'{triage_pill}'
        f'<p class="text-gray-800">{html_lib.escape(narrative_text)}</p>'
        "</div>"
    )
    # Use the raw (including the TRIAGE line) in the receipts — shows exactly what Claude emitted
    text = raw_text
    rule_receipt = _render_rule_receipt(claim)
    receipts = _render_receipts(
        prompt=prompt,
        raw_response=text,
        model=model_name,
        max_tokens=max_tokens,
        temperature=temperature,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        stop_reason=stop_reason,
    )
    profile = _render_provider_profile(claim["provider_id"], claim_id)
    html = narrative + rule_receipt + receipts + profile
    explanation_cache[claim_id] = html
    return html


def _render_rule_receipt(claim: dict) -> str:
    """Expandable card showing the deterministic rule that fired. Not an LLM call."""
    pid = claim["provider_id"]
    psum = provider_summary.get(pid, {})
    rate = psum.get("lens_pct", {}).get("premium_progressive", 0) / 100.0
    mean = population_premium_mean
    stdev = population_premium_stdev
    z = (rate - mean) / stdev if stdev > 0 else 0.0
    risk = int(round(max(0.0, min(100.0, z * 25.0))))
    triggered = bool(claim.get("triggered"))
    triggered_rule = claim.get("triggered_rule") or "—"
    rule_reason = claim.get("rule_reason") or "—"

    verdict_cls = "text-red-700" if triggered else "text-slate-500"
    verdict_label = "TRIGGERED" if triggered else "not triggered"

    code_excerpt = '''# detection.py — simplified
rates = {pid: premium_count[pid] / claim_count[pid] for pid in providers}
mean  = fmean(rates.values())
stdev = pstdev(rates.values())

for pid, rate in rates.items():
    z = (rate - mean) / stdev
    triggered = z > THRESHOLD          # THRESHOLD = 2.0 σ
    risk_score = clamp(z * 25, 0, 100) # scale z to 0-100 cap'''

    return f'''
<div class="my-3">
  <details class="bg-blue-50 border border-blue-200 rounded group">
    <summary class="cursor-pointer px-3 py-2 text-xs font-semibold text-blue-900 flex items-center justify-between select-none">
      <span class="flex items-center gap-2">
        <span class="inline-block w-4 h-4 rounded-full bg-blue-600 text-white text-center text-[10px] leading-4 font-bold">1</span>
        See the rule that fired for this claim
        <span class="ml-1 text-[10px] text-blue-700 bg-blue-100 px-1.5 py-0.5 rounded font-normal normal-case">deterministic · no LLM</span>
      </span>
      <span class="text-blue-400 text-[10px] group-open:rotate-180 transition-transform">▼</span>
    </summary>
    <div class="px-3 pb-3">
      <div class="text-[11px] text-slate-600 mb-2">Flagging is handled by a statistical rule in <code class="font-mono bg-white px-1 rounded border border-blue-200">detection.py</code> — not a prompt. This keeps the fraud/not-fraud decision auditable.</div>

      <div class="grid grid-cols-2 gap-3 text-xs">
        <div class="bg-white border border-blue-200 rounded p-3">
          <div class="text-[10px] uppercase tracking-wider text-blue-700 font-semibold mb-1">Inputs observed</div>
          <table class="w-full font-mono text-[11px] text-slate-700">
            <tr><td class="py-0.5 pr-2">provider_id</td><td>{pid}</td></tr>
            <tr><td class="py-0.5 pr-2">provider_premium_rate</td><td>{rate*100:.0f}%</td></tr>
            <tr><td class="py-0.5 pr-2">population_mean</td><td>{mean*100:.1f}%</td></tr>
            <tr><td class="py-0.5 pr-2">population_stdev</td><td>{stdev*100:.1f}pp</td></tr>
            <tr><td class="py-0.5 pr-2">z_score</td><td class="font-semibold">{z:+.2f}</td></tr>
            <tr><td class="py-0.5 pr-2">threshold</td><td>{DETECTION_THRESHOLD_SIGMA} σ</td></tr>
          </table>
        </div>
        <div class="bg-white border border-blue-200 rounded p-3">
          <div class="text-[10px] uppercase tracking-wider text-blue-700 font-semibold mb-1">Output written to claims_scored.json</div>
          <table class="w-full font-mono text-[11px] text-slate-700">
            <tr><td class="py-0.5 pr-2">triggered</td><td class="font-semibold {verdict_cls}">{verdict_label}</td></tr>
            <tr><td class="py-0.5 pr-2">triggered_rule</td><td>{html_lib.escape(str(triggered_rule))}</td></tr>
            <tr><td class="py-0.5 pr-2">risk_score</td><td class="font-semibold">{risk}/100</td></tr>
          </table>
          <div class="mt-2 text-[11px] text-slate-600 leading-relaxed">
            <span class="font-semibold">rule_reason:</span> {html_lib.escape(str(rule_reason))}
          </div>
        </div>
      </div>

      <div class="mt-3">
        <div class="text-[10px] uppercase tracking-wider text-blue-700 font-semibold mb-1">Scoring logic (excerpt from detection.py)</div>
        <pre class="bg-slate-900 text-slate-100 text-[11px] leading-relaxed p-3 rounded overflow-x-auto whitespace-pre">{html_lib.escape(code_excerpt)}</pre>
      </div>
    </div>
  </details>
</div>
'''


def _render_receipts(
    prompt: str,
    raw_response: str,
    model: str,
    max_tokens: int,
    temperature: float,
    input_tokens,
    output_tokens,
    stop_reason: str,
) -> str:
    """Inline expandable 'show the receipts' sections — exact prompt + raw response."""
    esc_prompt = html_lib.escape(prompt)
    esc_response = html_lib.escape(raw_response)
    return f'''
<div class="my-3 grid gap-2">
  <details class="bg-slate-50 border border-slate-200 rounded group">
    <summary class="cursor-pointer px-3 py-2 text-xs font-semibold text-slate-700 flex items-center justify-between select-none">
      <span class="flex items-center gap-2">
        <span class="inline-block w-4 h-4 rounded-full bg-slate-500 text-white text-center text-[10px] leading-4 font-bold">2</span>
        See the exact prompt Claude received
        <span class="ml-1 text-[10px] text-slate-600 bg-slate-200 px-1.5 py-0.5 rounded font-normal normal-case">LLM input</span>
      </span>
      <span class="text-slate-400 text-[10px] group-open:rotate-180 transition-transform">▼</span>
    </summary>
    <div class="px-3 pb-3">
      <div class="text-[11px] text-slate-500 mb-1">Substituted for this specific claim — not the template.</div>
      <pre class="bg-white border border-slate-200 rounded p-3 text-[11px] leading-relaxed text-slate-800 whitespace-pre-wrap overflow-x-auto">{esc_prompt}</pre>
    </div>
  </details>

  <details class="bg-slate-50 border border-slate-200 rounded group">
    <summary class="cursor-pointer px-3 py-2 text-xs font-semibold text-slate-700 flex items-center justify-between select-none">
      <span class="flex items-center gap-2">
        <span class="inline-block w-4 h-4 rounded-full bg-slate-500 text-white text-center text-[10px] leading-4 font-bold">3</span>
        See the raw response from Claude
        <span class="ml-1 text-[10px] text-slate-600 bg-slate-200 px-1.5 py-0.5 rounded font-normal normal-case">LLM output</span>
      </span>
      <span class="text-slate-400 text-[10px] group-open:rotate-180 transition-transform">▼</span>
    </summary>
    <div class="px-3 pb-3">
      <div class="text-[11px] text-slate-500 mb-1 flex flex-wrap gap-x-4 gap-y-1">
        <span><span class="font-semibold">Model:</span> {html_lib.escape(model)}</span>
        <span><span class="font-semibold">max_tokens:</span> {max_tokens}</span>
        <span><span class="font-semibold">temperature:</span> {temperature}</span>
        <span><span class="font-semibold">input tokens:</span> {input_tokens}</span>
        <span><span class="font-semibold">output tokens:</span> {output_tokens}</span>
        <span><span class="font-semibold">stop reason:</span> {html_lib.escape(str(stop_reason))}</span>
      </div>
      <pre class="bg-white border border-slate-200 rounded p-3 text-[11px] leading-relaxed text-slate-800 whitespace-pre-wrap overflow-x-auto">{esc_response}</pre>
    </div>
  </details>
</div>
'''


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
        ("try", "/try", "Try a claim"),
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


@app.post("/reshuffle", response_class=HTMLResponse)
def reshuffle():
    """Regenerate synthetic data, re-score, invalidate caches, restart warmup.
    Returns a terminal-style trace of the commands + their stdout; the user
    dismisses it with an OK button that then reloads the dashboard."""
    import secrets
    fresh_seed = str(secrets.randbelow(1_000_000))
    env = {**os.environ, "DATA_SEED": fresh_seed}
    gen_out = det_out = ""
    try:
        gen = subprocess.run(
            ["python3", "generate_data.py"],
            check=True, cwd=BASE_DIR, timeout=60,
            capture_output=True, text=True, env=env,
        )
        gen_out = (gen.stdout + gen.stderr).strip()
        det = subprocess.run(
            ["python3", "detection.py"],
            check=True, cwd=BASE_DIR, timeout=60,
            capture_output=True, text=True,
        )
        det_out = (det.stdout + det.stderr).strip()
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e))[-1200:]
        return HTMLResponse(
            f'<div class="bg-red-50 border-l-4 border-red-400 p-3 rounded text-xs text-red-800">'
            f'Regeneration failed: <pre class="whitespace-pre-wrap mt-1">{html_lib.escape(err)}</pre></div>',
            status_code=500,
        )
    except subprocess.TimeoutExpired:
        return HTMLResponse(
            '<div class="bg-red-50 border-l-4 border-red-400 p-3 rounded text-xs text-red-800">Regeneration timed out.</div>',
            status_code=500,
        )

    _reload_state()
    _kick_off_warmup()

    new_total = len(claims)
    new_flagged = sum(1 for c in claims.values() if c.get("triggered"))
    new_fraud = sum(1 for c in claims.values() if c.get("is_fraud"))

    return HTMLResponse(
        f'''<div class="bg-slate-900 text-slate-100 rounded-lg p-5 shadow-2xl my-3">
  <div class="flex items-center justify-between mb-3">
    <div class="text-xs uppercase tracking-widest text-amber-300 font-semibold">Reshuffle trace — what just ran</div>
    <div class="text-[11px] text-slate-400 font-mono">DATA_SEED={fresh_seed}</div>
  </div>

  <div class="mb-3">
    <div class="text-xs text-green-400 font-mono">$ DATA_SEED={fresh_seed} python3 generate_data.py</div>
    <pre class="text-xs text-slate-300 mt-1 ml-3 whitespace-pre-wrap">{html_lib.escape(gen_out) or "(no output)"}</pre>
  </div>

  <div class="mb-3">
    <div class="text-xs text-green-400 font-mono">$ python3 detection.py</div>
    <pre class="text-xs text-slate-300 mt-1 ml-3 whitespace-pre-wrap">{html_lib.escape(det_out) or "(no output)"}</pre>
  </div>

  <div class="mb-3">
    <div class="text-xs text-green-400 font-mono"># server-side, in-process:</div>
    <pre class="text-xs text-slate-300 mt-1 ml-3 whitespace-pre-wrap">_reload_state()   # rebuild claims dict, provider_summary, population stats
_kick_off_warmup() # background thread, Claude triage on {new_flagged} flagged claims</pre>
  </div>

  <div class="mb-4 text-xs text-emerald-300 border-t border-slate-700 pt-3">
    ✓ State reloaded. <span class="font-semibold">{new_total}</span> claims · <span class="font-semibold">{new_fraud}</span> seeded fraud · <span class="font-semibold">{new_flagged}</span> flagged by the rule · AI triage running in background.
  </div>

  <div class="flex items-center justify-between">
    <div class="text-[11px] text-slate-500">No LLM was called for the reshuffle itself — just rule + rng. The triage call for each new flagged claim will run in the background once you return to the dashboard.</div>
    <button hx-get="/" hx-target="body" hx-swap="outerHTML"
            class="ml-4 bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-2 rounded font-semibold text-sm whitespace-nowrap">
      OK — reload dashboard
    </button>
  </div>
</div>'''
    )


def _build_synthetic_claim(
    provider_id: str, lens_type: str, billed_amount: float
) -> dict:
    """Build a synthetic claim dict that scores against existing population stats."""
    psum = provider_summary.get(provider_id, {})
    rate = psum.get("lens_pct", {}).get("premium_progressive", 0) / 100.0
    mean = population_premium_mean
    stdev = population_premium_stdev
    z = (rate - mean) / stdev if stdev > 0 else 0.0
    risk = max(0.0, min(100.0, z * 25.0))
    triggered = z > DETECTION_THRESHOLD_SIGMA

    # Use a matched member from this provider if any, else M999-test
    m_claims = [c for c in claims.values() if c["provider_id"] == provider_id]
    state = m_claims[0].get("provider_state", "CA") if m_claims else "CA"
    member_id = m_claims[0].get("member_id", "M999") if m_claims else "M999"

    rule_reason = None
    triggered_rule = None
    if triggered:
        triggered_rule = "provider_upcoding"
        rule_reason = (
            f"Provider {provider_id} bills premium_progressive at {rate*100:.0f}% "
            f"vs population mean of {mean*100:.0f}% (z={z:.1f})"
        )

    return {
        "claim_id": f"synth-{uuid.uuid4().hex[:12]}",
        "provider_id": provider_id,
        "provider_state": state,
        "member_id": member_id,
        "service_date": "2026-04-24",
        "exam_cpt": "92014",
        "lens_type": lens_type,
        "lens_addons": [],
        "billed_amount": billed_amount,
        "paid_amount": round(billed_amount * 0.78, 2),
        "is_fraud": False,  # ground-truth unknown for synthetic
        "fraud_type": None,
        "risk_score": round(risk, 2),
        "triggered": triggered,
        "triggered_rule": triggered_rule,
        "rule_reason": rule_reason,
    }


def _call_claude_for_synthetic(claim: dict) -> tuple[str, str, dict]:
    """Call Claude for a synthetic claim. Returns (triage, narrative_text, meta)."""
    psum = provider_summary.get(
        claim["provider_id"], {"claim_count": 0, "premium_rate": 0, "total_billed": 0}
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
        rule_reason=claim.get("rule_reason", "") or "(rule did not fire)",
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
    raw_text = response.content[0].text
    triage, narrative_text = _parse_triage(raw_text)
    meta = {
        "prompt": prompt,
        "raw_response": raw_text,
        "input_tokens": getattr(response.usage, "input_tokens", "?"),
        "output_tokens": getattr(response.usage, "output_tokens", "?"),
        "stop_reason": getattr(response, "stop_reason", "?"),
    }
    return triage, narrative_text, meta


@app.get("/try", response_class=HTMLResponse)
def try_page():
    # Provider dropdown: sort by premium_rate desc so fraudsters float to top
    provider_opts = sorted(
        provider_summary.items(),
        key=lambda kv: (-kv[1]["premium_rate"], kv[0]),
    )
    opt_html = "".join(
        f'<option value="{pid}">{pid} — {s["premium_rate"]}% premium · {s["claim_count"]} claims</option>'
        for pid, s in provider_opts
    )

    body = f'''<!DOCTYPE html>
<html><head>
  <title>Try a claim — Insurance Fraud POC</title>
  <script src="https://unpkg.com/htmx.org@2.0.3"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head><body class="bg-slate-50">
{_shared_header("try")}
<main class="max-w-7xl mx-auto p-6">
  <div class="mb-6 max-w-3xl">
    <div class="text-xs uppercase tracking-wide text-slate-500">Interactive sandbox</div>
    <h2 class="text-2xl font-semibold text-slate-800">Build a claim, watch it get triaged</h2>
    <p class="text-sm text-slate-600 mt-2 leading-relaxed">
      Pick any combination of provider + lens type + billed amount. The deterministic rule in <code class="bg-slate-100 px-1 rounded text-xs">detection.py</code> scores it against the existing population. If it fires, Claude triages it in real time with the exact same prompt used on the dashboard. Nothing is cached.
    </p>
  </div>

  <div class="grid grid-cols-12 gap-6">
    <!-- Left: form -->
    <section class="col-span-4">
      <div class="bg-white rounded-lg shadow p-5">
        <div class="text-xs uppercase tracking-wider text-slate-500 font-semibold mb-2">Presets</div>
        <div class="grid gap-2 mb-4">
          <button class="text-left px-3 py-2 rounded border border-red-300 bg-red-50 hover:bg-red-100 text-sm text-red-900" type="button"
                  hx-post="/try/triage" hx-target="#try-result" hx-swap="innerHTML" hx-indicator="#try-spinner"
                  hx-vals='{{"provider_id":"{_FRAUDSTER_PROVIDER_ID}","lens_type":"premium_progressive","billed_amount":"600"}}'>
            <div class="font-semibold">Clear upcoding</div>
            <div class="text-xs text-red-800 opacity-80">premium_progressive · $600 · at fraudster {_FRAUDSTER_PROVIDER_ID}</div>
          </button>
          <button class="text-left px-3 py-2 rounded border border-amber-300 bg-amber-50 hover:bg-amber-100 text-sm text-amber-900" type="button"
                  hx-post="/try/triage" hx-target="#try-result" hx-swap="innerHTML" hx-indicator="#try-spinner"
                  hx-vals='{{"provider_id":"{_FRAUDSTER_PROVIDER_ID}","lens_type":"premium_progressive","billed_amount":"450"}}'>
            <div class="font-semibold">Ambiguous premium</div>
            <div class="text-xs text-amber-800 opacity-80">premium_progressive · $450 · at fraudster {_FRAUDSTER_PROVIDER_ID}</div>
          </button>
          <button class="text-left px-3 py-2 rounded border border-emerald-300 bg-emerald-50 hover:bg-emerald-100 text-sm text-emerald-900" type="button"
                  hx-post="/try/triage" hx-target="#try-result" hx-swap="innerHTML" hx-indicator="#try-spinner"
                  hx-vals='{{"provider_id":"{_FRAUDSTER_PROVIDER_ID}","lens_type":"bifocal","billed_amount":"250"}}'>
            <div class="font-semibold">Bifocal at fraudster</div>
            <div class="text-xs text-emerald-800 opacity-80">bifocal · $250 · at fraudster {_FRAUDSTER_PROVIDER_ID}</div>
          </button>
          <button class="text-left px-3 py-2 rounded border border-slate-300 bg-slate-50 hover:bg-slate-100 text-sm text-slate-700" type="button"
                  hx-post="/try/triage" hx-target="#try-result" hx-swap="innerHTML" hx-indicator="#try-spinner"
                  hx-vals='{{"provider_id":"{_CLEAN_PROVIDER_ID}","lens_type":"single","billed_amount":"200"}}'>
            <div class="font-semibold">Clean / rule should not fire</div>
            <div class="text-xs text-slate-600 opacity-80">single · $200 · at clean provider {_CLEAN_PROVIDER_ID}</div>
          </button>
        </div>

        <div class="border-t border-slate-200 pt-4">
          <div class="text-xs uppercase tracking-wider text-slate-500 font-semibold mb-2">Or build your own</div>
          <form hx-post="/try/triage" hx-target="#try-result" hx-swap="innerHTML" hx-indicator="#try-spinner" class="space-y-3">
            <div>
              <label class="text-xs font-semibold text-slate-700 block mb-1">Provider</label>
              <select name="provider_id" class="w-full text-xs border border-slate-300 rounded px-2 py-1.5 bg-white">
                {opt_html}
              </select>
            </div>
            <div>
              <label class="text-xs font-semibold text-slate-700 block mb-1">Lens type</label>
              <select name="lens_type" class="w-full text-sm border border-slate-300 rounded px-2 py-1.5 bg-white">
                <option>single</option><option>bifocal</option><option>progressive</option>
                <option selected>premium_progressive</option>
              </select>
            </div>
            <div>
              <label class="text-xs font-semibold text-slate-700 block mb-1">Billed amount ($)</label>
              <input type="number" name="billed_amount" value="400" step="25" min="100" max="1000"
                     class="w-full text-sm border border-slate-300 rounded px-2 py-1.5 bg-white">
            </div>
            <button type="submit" class="w-full bg-[#003b71] hover:bg-blue-900 text-white rounded py-2 text-sm font-semibold">
              Triage this claim →
            </button>
          </form>
        </div>
      </div>

      <div class="mt-3 text-[11px] text-slate-500 leading-relaxed">
        Member, date, CPT, and addon fields use sensible defaults. The rule only looks at provider-level statistics, so only provider_id, lens_type, and billed_amount are meaningful inputs.
      </div>
    </section>

    <!-- Right: result -->
    <section class="col-span-8">
      <div id="try-spinner" class="htmx-indicator">
        <div class="bg-white rounded-lg shadow p-8 text-center">
          <div class="inline-block w-8 h-8 border-4 border-slate-200 border-t-[#003b71] rounded-full animate-spin"></div>
          <div class="mt-3 text-sm text-slate-600">Running detection rule, then calling Claude…</div>
          <div class="mt-1 text-xs text-slate-400">Typically 2–4 seconds.</div>
        </div>
      </div>
      <div id="try-result">
        <div class="bg-white rounded-lg shadow p-8 text-center text-slate-400 text-sm">
          Choose a preset or submit the form to see the rule + Claude triage in action.
        </div>
      </div>
    </section>
  </div>

  <style>
    .htmx-indicator {{ display: none; }}
    .htmx-request .htmx-indicator {{ display: block; }}
    .htmx-request #try-result {{ display: none; }}
  </style>
</main></body></html>'''
    return HTMLResponse(content=body)


@app.post("/try/triage", response_class=HTMLResponse)
def try_triage(
    provider_id: str = Form(...),
    lens_type: str = Form(...),
    billed_amount: float = Form(...),
):
    if provider_id not in provider_summary:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    if lens_type not in LENS_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown lens_type {lens_type}")
    if not (50 <= billed_amount <= 2000):
        raise HTTPException(status_code=400, detail="billed_amount out of range")

    claim = _build_synthetic_claim(provider_id, lens_type, billed_amount)

    # Build inputs summary card (always shown)
    inputs_card = f'''
<div class="bg-slate-900 text-slate-100 rounded-lg p-4 mb-3 text-xs font-mono">
  <div class="text-[11px] uppercase tracking-wider text-slate-400 font-semibold mb-2">Synthetic claim · {claim["claim_id"][:12]}</div>
  <div class="grid grid-cols-2 gap-x-6 gap-y-1">
    <span><span class="text-slate-400">provider_id:</span> {provider_id}</span>
    <span><span class="text-slate-400">provider_state:</span> {claim["provider_state"]}</span>
    <span><span class="text-slate-400">lens_type:</span> {lens_type}</span>
    <span><span class="text-slate-400">billed_amount:</span> ${billed_amount:,.0f}</span>
    <span><span class="text-slate-400">exam_cpt:</span> {claim["exam_cpt"]}</span>
    <span><span class="text-slate-400">service_date:</span> {claim["service_date"]}</span>
  </div>
</div>
'''

    # If rule didn't fire, show "not flagged" and skip Claude
    if not claim["triggered"]:
        rate = provider_summary[provider_id]["premium_rate"]
        z = (
            rate / 100.0 - population_premium_mean
        ) / population_premium_stdev if population_premium_stdev > 0 else 0.0
        return HTMLResponse(
            inputs_card
            + f'''
<div class="bg-emerald-50 border-l-4 border-emerald-400 p-4 rounded">
  <div class="text-xs uppercase tracking-wider text-emerald-700 font-semibold mb-1">Rule did not fire — no triage needed</div>
  <p class="text-slate-800 text-sm leading-relaxed">
    Provider {provider_id} bills premium_progressive at <strong>{rate}%</strong>,
    within population norms (mean {population_premium_mean*100:.0f}%, z={z:+.2f}, threshold {DETECTION_THRESHOLD_SIGMA}σ).
    This claim would not be surfaced to an investigator, and no Claude call is made. The demo works whether or not the rule fires — this is the "rule determines the gate" half of the product argument.
  </p>
</div>
'''
        )

    # Rule fired: call Claude
    triage, narrative_text, meta = _call_claude_for_synthetic(claim)

    triage_labels = {
        "likely_fraud": ("Likely fraud", "bg-red-100 text-red-800 border-red-300"),
        "doc_error": ("Doc/coding error", "bg-amber-100 text-amber-800 border-amber-300"),
        "false_positive": ("False positive", "bg-emerald-100 text-emerald-800 border-emerald-300"),
        "unknown": ("Unclassified", "bg-slate-100 text-slate-700 border-slate-300"),
    }
    t_label, t_cls = triage_labels.get(triage, triage_labels["unknown"])

    narrative = (
        '<div class="bg-amber-50 border-l-4 border-amber-400 p-4 rounded">'
        '<div class="text-xs uppercase tracking-wide text-amber-700 mb-1 font-semibold">AI Fraud Analyst Review</div>'
        f'<span class="inline-block px-2 py-0.5 rounded text-xs font-semibold border {t_cls} mb-2">{t_label}</span>'
        f'<p class="text-gray-800">{html_lib.escape(narrative_text)}</p>'
        "</div>"
    )
    rule_receipt = _render_rule_receipt(claim)
    receipts = _render_receipts(
        prompt=meta["prompt"],
        raw_response=meta["raw_response"],
        model="claude-sonnet-4-5",
        max_tokens=300,
        temperature=0.3,
        input_tokens=meta["input_tokens"],
        output_tokens=meta["output_tokens"],
        stop_reason=meta["stop_reason"],
    )
    return HTMLResponse(inputs_card + narrative + rule_receipt + receipts)


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


# -- Background warmup: precompute triage for every flagged claim --------------
# Runs once at module load, in a daemon thread so startup isn't blocked.
# Populates `explanation_cache` and `triage_by_claim`. After it completes,
# dashboard risk badges render in red/amber/green rather than neutral gray.

def _warmup_cache() -> None:
    my_generation = _data_generation
    flagged_ids = [c["claim_id"] for c in claims.values() if c.get("triggered")]
    triaged_here = 0
    for cid in flagged_ids:
        # Bail if a newer reshuffle has invalidated this generation
        if _data_generation != my_generation:
            print(f"[warmup gen={my_generation}] aborting — newer generation started")
            return
        # Claim may have disappeared between snapshot and now
        if cid not in claims:
            continue
        if cid in explanation_cache:
            continue
        try:
            _build_explain_html(cid)
            triaged_here += 1
        except Exception as e:
            print(f"[warmup gen={my_generation}] skipped {cid[:8]}: {type(e).__name__}: {e}")
    print(f"[warmup gen={my_generation}] done — triaged {triaged_here} of {len(flagged_ids)} flagged claims")


def _kick_off_warmup() -> None:
    t = threading.Thread(target=_warmup_cache, daemon=True, name="triage-warmup")
    t.start()
    print(f"[warmup] started for {sum(1 for c in claims.values() if c.get('triggered'))} flagged claims")


_kick_off_warmup()

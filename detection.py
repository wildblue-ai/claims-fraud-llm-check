"""Phase 2 detection: provider-level upcoding detector.

Flags claims from providers whose share of `premium_progressive` lens claims
is more than `threshold` standard deviations above the population mean.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLAIMS_PATH = ROOT / "data" / "claims.json"
OUT_PATH = ROOT / "data" / "claims_scored.json"


def score(claims: list[dict], threshold: float) -> tuple[list[dict], dict]:
    # Per-provider premium_progressive rate
    totals: dict[str, int] = defaultdict(int)
    premium: dict[str, int] = defaultdict(int)
    for c in claims:
        pid = c["provider_id"]
        totals[pid] += 1
        if c["lens_type"] == "premium_progressive":
            premium[pid] += 1

    rates = {pid: premium[pid] / totals[pid] for pid in totals}
    values = list(rates.values())
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0

    z_by_provider: dict[str, float] = {}
    triggered_providers: set[str] = set()
    for pid, rate in rates.items():
        z = (rate - mean) / stdev if stdev > 0 else 0.0
        z_by_provider[pid] = z
        if z > threshold:
            triggered_providers.add(pid)

    scored: list[dict] = []
    for c in claims:
        pid = c["provider_id"]
        z = z_by_provider[pid]
        rate = rates[pid]
        # Scale z to 0-100; clamp
        risk = max(0.0, min(100.0, z * 25.0))
        triggered = pid in triggered_providers
        new = dict(c)
        new["risk_score"] = round(risk, 2)
        new["triggered"] = triggered
        if triggered:
            new["triggered_rule"] = "provider_upcoding"
            new["rule_reason"] = (
                f"Provider {pid} bills premium_progressive at {rate*100:.0f}% "
                f"vs population mean of {mean*100:.0f}% (z={z:.1f})"
            )
        else:
            new["triggered_rule"] = None
            new["rule_reason"] = None
        scored.append(new)

    stats = {
        "mean": mean,
        "stdev": stdev,
        "rates": rates,
        "z": z_by_provider,
        "triggered_providers": triggered_providers,
    }
    return scored, stats


def precision_recall(scored: list[dict]) -> tuple[float, float, int, int, int]:
    tp = fp = fn = 0
    for c in scored:
        flagged = c["triggered"]
        fraud = c.get("is_fraud", False)
        if flagged and fraud:
            tp += 1
        elif flagged and not fraud:
            fp += 1
        elif not flagged and fraud:
            fn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return precision, recall, tp, fp, fn


def run(threshold: float) -> tuple[list[dict], float, float, dict]:
    claims = json.loads(CLAIMS_PATH.read_text())
    scored, stats = score(claims, threshold)
    precision, recall, tp, fp, fn = precision_recall(scored)
    print(
        f"threshold={threshold} recall={recall:.2f} precision={precision:.2f} "
        f"tp={tp} fp={fp} fn={fn}"
    )
    return scored, precision, recall, stats


def main() -> int:
    thresholds = [2.0, 1.75, 1.5]
    attempts: list[dict] = []
    chosen = None

    for i, t in enumerate(thresholds):
        scored, precision, recall, stats = run(t)
        note = "baseline" if i == 0 else "lowered for recall"
        attempts.append(
            {
                "threshold": t,
                "recall": round(recall, 2),
                "precision": round(precision, 2),
                "note": note,
            }
        )
        if recall >= 0.70:
            chosen = (t, scored, precision, recall, stats)
            break

    if chosen is None:
        # Use the last attempt (threshold 1.5) for output
        t = thresholds[-1]
        scored, precision, recall, stats = run(t)
        chosen = (t, scored, precision, recall, stats)
        verdict = "FAIL"
    else:
        verdict = "PASS"

    t, scored, precision, recall, stats = chosen

    # Persist scored claims
    OUT_PATH.write_text(json.dumps(scored, indent=2))

    # Sample 3 triggered claim_ids
    flagged_ids = [c["claim_id"] for c in scored if c["triggered"]][:3]

    # Write report
    report_path = ROOT / "reports" / "phase-2.md"
    lines = [
        f"verdict: {verdict}",
        f"recall: {recall:.2f}",
        f"precision: {precision:.2f}",
        f"threshold_used: {t}",
        "attempts:",
    ]
    for a in attempts:
        lines.append(
            f"  - {{threshold: {a['threshold']}, recall: {a['recall']:.2f}, "
            f"precision: {a['precision']:.2f}, note: \"{a['note']}\"}}"
        )
    lines.append(f"flagged_sample: {flagged_ids}")
    if verdict == "FAIL":
        lines.append("diagnosis: |")
        lines.append(
            "  Lowered threshold from 2.0 down to 1.5 (allowed minimum) without"
        )
        lines.append(
            "  reaching recall >= 0.70. Injected fraud is distributed across"
        )
        lines.append(
            "  providers whose premium_progressive rate does not stand out"
        )
        lines.append(
            "  enough vs the population. A human would need to either change"
        )
        lines.append(
            "  the rule (e.g., per-claim lens_type + billed_amount signals,"
        )
        lines.append(
            "  member-level duplicate detection) or lower the threshold below"
        )
        lines.append("  1.5 -- outside the allowed tuning range.")

    report_path.write_text("\n".join(lines) + "\n")

    summary = (
        f"Phase 2 {verdict}: recall={recall:.2f}, precision={precision:.2f}, "
        f"threshold={t}"
    )
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

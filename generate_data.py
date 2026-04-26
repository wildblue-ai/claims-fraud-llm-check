"""Phase 1 — Synthetic vision-insurance claims generator.

Python 3.11+ stdlib only. Generates 500 claims with a hidden upcoding
fraud pattern injected across exactly 3 fraudster providers.
"""

from __future__ import annotations

import json
import random
import os
import uuid
from datetime import date, timedelta
from pathlib import Path

# SEED=env override lets /reshuffle produce a different dataset each call;
# default remains the original 42 so the committed build is reproducible.
SEED = int(os.environ.get("DATA_SEED", "42"))
NUM_CLAIMS = 500
NUM_PROVIDERS = 30
NUM_MEMBERS = 200
NUM_FRAUDSTERS = 3
FLIP_RATE = 0.60  # tuning knob per §5 retry policy (at floor of allowed [0.60, 0.85])

PROVIDERS = [f"P{i:03d}" for i in range(1, NUM_PROVIDERS + 1)]
MEMBERS = [f"M{i:03d}" for i in range(1, NUM_MEMBERS + 1)]

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]

EXAM_CPTS = [92002, 92004, 92012, 92014]

LENS_TYPES = ["single", "bifocal", "progressive", "premium_progressive"]
LENS_WEIGHTS = [0.35, 0.15, 0.35, 0.15]

ADDONS = ["AR", "photochromic", "blue_light", "high_index"]

# Realistic billed ranges by lens type (pre-addon)
BILLED_RANGES = {
    "single": (150, 230),
    "bifocal": (230, 330),
    "progressive": (330, 460),
    "premium_progressive": (430, 620),
}

ADDON_COSTS = {
    "AR": 40,
    "photochromic": 80,
    "blue_light": 35,
    "high_index": 60,
}


def pick_provider_state(rng: random.Random, provider_state_map: dict[str, str], pid: str) -> str:
    if pid not in provider_state_map:
        provider_state_map[pid] = rng.choice(US_STATES)
    return provider_state_map[pid]


def random_date(rng: random.Random, start: date, end: date) -> date:
    delta_days = (end - start).days
    return start + timedelta(days=rng.randint(0, delta_days))


def pick_lens_type(rng: random.Random) -> str:
    return rng.choices(LENS_TYPES, weights=LENS_WEIGHTS, k=1)[0]


def pick_addons(rng: random.Random) -> list[str]:
    n = rng.choices([0, 1, 2, 3], weights=[0.35, 0.35, 0.22, 0.08], k=1)[0]
    return rng.sample(ADDONS, k=n)


def compute_billed(rng: random.Random, lens_type: str, addons: list[str]) -> float:
    low, high = BILLED_RANGES[lens_type]
    base = rng.randint(low, high)
    addon_total = sum(ADDON_COSTS[a] for a in addons)
    # small jitter
    return float(base + addon_total + rng.choice([0, 0, 5, -5, 10]))


def generate_member_exam_dates(rng: random.Random, end: date, start: date) -> dict[str, list[date]]:
    """Each member gets roughly annual exams in the 18-month window."""
    schedule: dict[str, list[date]] = {}
    for m in MEMBERS:
        dates: list[date] = []
        # First exam somewhere in the window
        first = random_date(rng, start, end)
        dates.append(first)
        # ~annual cadence: 50% chance of a second exam ~12 months later
        if rng.random() < 0.5:
            jitter_days = rng.randint(-45, 60)
            second = first + timedelta(days=365 + jitter_days)
            if start <= second <= end:
                dates.append(second)
        schedule[m] = dates
    return schedule


def generate_claims(seed: int = SEED, flip_rate: float = FLIP_RATE) -> tuple[list[dict], list[str]]:
    rng = random.Random(seed)

    end_date = date.today()
    start_date = end_date - timedelta(days=int(365 * 1.5))

    # Choose fraudster providers deterministically-but-random
    fraudsters = sorted(rng.sample(PROVIDERS, k=NUM_FRAUDSTERS))

    provider_state_map: dict[str, str] = {}
    claims: list[dict] = []

    # Build a pool of (member, date) exam events and expand until ~NUM_CLAIMS.
    member_schedule = generate_member_exam_dates(rng, end_date, start_date)
    events: list[tuple[str, date]] = []
    for m, ds in member_schedule.items():
        for d in ds:
            events.append((m, d))

    # Top up with extra random events to reach NUM_CLAIMS
    while len(events) < NUM_CLAIMS:
        m = rng.choice(MEMBERS)
        d = random_date(rng, start_date, end_date)
        events.append((m, d))

    rng.shuffle(events)
    events = events[:NUM_CLAIMS]

    for member_id, service_date in events:
        provider_id = rng.choice(PROVIDERS)
        provider_state = pick_provider_state(rng, provider_state_map, provider_id)
        exam_cpt = rng.choice(EXAM_CPTS)
        lens_type = pick_lens_type(rng)
        addons = pick_addons(rng)

        is_fraud = False
        fraud_type = None

        # Upcoding injection for fraudster providers
        if provider_id in fraudsters and rng.random() < flip_rate:
            lens_type = "premium_progressive"
            # Elevated billed amount $500–650
            billed = float(rng.randint(500, 650))
            # keep addons but don't further inflate
            is_fraud = True
            fraud_type = "upcoding"
        else:
            billed = compute_billed(rng, lens_type, addons)

        paid_ratio = rng.uniform(0.70, 0.80)
        paid = round(billed * paid_ratio, 2)

        claim = {
            "claim_id": str(uuid.UUID(int=rng.getrandbits(128))),
            "provider_id": provider_id,
            "provider_state": provider_state,
            "member_id": member_id,
            "service_date": service_date.isoformat(),
            "exam_cpt": exam_cpt,
            "lens_type": lens_type,
            "lens_addons": addons,
            "billed_amount": billed,
            "paid_amount": paid,
            "is_fraud": is_fraud,
            "fraud_type": fraud_type,
        }
        claims.append(claim)

    return claims, fraudsters


def summarize(claims: list[dict]) -> tuple[int, int, float, list[str]]:
    total = len(claims)
    fraud = sum(1 for c in claims if c["is_fraud"])
    rate = (fraud / total * 100) if total else 0.0
    fraudster_ids = sorted({c["provider_id"] for c in claims if c["is_fraud"]})
    return total, fraud, rate, fraudster_ids


def main() -> None:
    root = Path(__file__).resolve().parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    claims, fraudsters = generate_claims()
    total, fraud, rate, fraudster_ids = summarize(claims)

    out_path = data_dir / "claims.json"
    with out_path.open("w") as f:
        json.dump(claims, f, indent=2)

    print(f"total_claims: {total}")
    print(f"fraud_count: {fraud}")
    print(f"fraud_rate: {rate:.1f}%")
    print(f"intended_fraudsters: {fraudsters}")
    print(f"observed_fraudster_provider_ids: {fraudster_ids}")


if __name__ == "__main__":
    main()

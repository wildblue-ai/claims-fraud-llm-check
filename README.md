# Insurance Fraud Detection POC

A prototype fraud-waste-and-abuse (FWA) detection pipeline for vision-insurance claims. It generates a synthetic claims dataset, scores claims with simple rule-based detectors (provider upcoding, billing outliers, etc.), and surfaces flagged claims in a FastAPI + HTMX dashboard. Clicking a flagged claim calls the Anthropic API to produce a 2-3 sentence senior-fraud-analyst narrative, rendered in-place.

**Built in 2 hours.**

## Setup

```bash
python -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit .env and set ANTHROPIC_API_KEY=sk-...
python generate_data.py            # creates data/claims.json
python detection.py                # creates data/claims_scored.json
uvicorn main:app --reload
```

Then open http://localhost:8000.

## Detection metrics (current run)

From `reports/phase-2.md`:

- **Recall: 1.00** - every known-fraud claim in the synthetic set was flagged.
- **Precision: 0.57** - roughly 43% of flags are false positives at the current threshold (2.0).

High recall with moderate precision is the right prototype tradeoff: a human analyst reviews each flag, so missed fraud is more expensive than extra review.

## What production would extend to

The current rule set catches one fraud archetype (upcoding premium lens types and billing outliers). Real deployment needs:

- **Frequency anomalies** - same member receiving multiple exams in a short window, or a provider submitting an implausible daily claim volume.
- **Provider rings** - collusion networks billing similar unusual patterns across shared members.
- **Phantom add-ons** - lens coatings, UV, anti-reflective that were billed but never documented in the exam record.
- **Temporal drift** - fraudsters adapt; models need scheduled retraining and out-of-distribution monitoring.
- **Real data** - this needs to be validated against actual production claims, plus held-out fraud cases the model has never seen, to measure true generalization.

## Caveat

Synthetic data validates the detection pipeline, not model generalization. The fraud signals in the generated data are the same signals the detectors look for, so the numbers above measure pipeline wiring, not real-world detection performance.

# Test Baselines

Baseline snapshots used by the improvement verification gate.

## What are baselines?

Baselines are JSON fixtures that capture a metric at a known-good state. Before a feature branch lands,
its DeepEval tests compare the new behavior against these baselines to confirm the change actually
improves the metric it claims to improve (not just that it doesn't regress).

## Files

| File | Branch | Metric captured |
|---|---|---|
| `score_distribution.json` | C | Mean, std_dev, clustering coefficient of suitability scores across fixture jobs |
| `feedback_summary_quality.json` | B/E | Pass rates from existing feedback summary and insights prompt tests |

## How to update baselines

Run the relevant test suite against the current production state and capture output:

```bash
# Update score distribution baseline (run against a populated jobs.db)
pytest tests/eval/ -k "score_distribution" --baseline-update

# Or manually: capture mean/std_dev from a real run and write to the JSON file
```

Baselines should only be updated when:
1. You have intentionally changed the behavior and the old baseline is no longer valid.
2. The change has been verified to improve the metric (not just shift it).

Never update a baseline to make a failing test pass without understanding why it was failing.

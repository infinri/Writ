"""Summary statistics. Percentiles use the nearest-rank method on the sorted
samples (no interpolation): pXX is the smallest sample with at least XX% of
samples at or below it."""
from __future__ import annotations

import math
import statistics


def percentile(sorted_samples: list, q: float):
    if not sorted_samples:
        return None
    rank = max(1, math.ceil(q / 100 * len(sorted_samples)))
    return sorted_samples[rank - 1]


def summarize(samples: list) -> dict:
    s = sorted(samples)
    n = len(s)
    return {
        "n": n,
        "min": s[0] if n else None,
        "p50": percentile(s, 50),
        "p95": percentile(s, 95),
        "p99": percentile(s, 99),
        "max": s[-1] if n else None,
        "mean": statistics.fmean(s) if n else None,
        "stdev": statistics.stdev(s) if n > 1 else None,
    }


def run_to_run(per_run_values: list) -> dict:
    """Variation between runs of a per-run statistic (e.g. each run's p50)."""
    n = len(per_run_values)
    mean = statistics.fmean(per_run_values) if n else None
    var = statistics.variance(per_run_values) if n > 1 else None
    return {"n_runs": n, "values": list(per_run_values), "mean": mean, "variance": var,
            "cv": (math.sqrt(var) / mean) if var is not None and mean else None}


def redact(value):
    """Dry runs keep structure and counts but no timing or memory values."""
    if isinstance(value, dict):
        return {k: (v if k in ("n", "n_runs") else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return None if isinstance(value, (int, float)) and not isinstance(value, bool) else value

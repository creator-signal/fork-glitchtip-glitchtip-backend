"""
Fixed-bucket duration histogram for p50/p95 approximation.

Uses ~50 exponentially-spaced buckets from 1ms to 60,000ms.
Accuracy is within one bucket width — adequate for dashboards.
"""

import bisect
import math

# Generate exponentially-spaced bucket boundaries from 1ms to 60,000ms
_MIN_MS = 1.0
_MAX_MS = 60_000.0
_NUM_BUCKETS = 50

BUCKET_BOUNDARIES: list[float] = [
    _MIN_MS * (_MAX_MS / _MIN_MS) ** (i / _NUM_BUCKETS) for i in range(_NUM_BUCKETS + 1)
]


def get_bucket_index(duration_ms: float) -> int:
    """Return the bucket index for a given duration in milliseconds."""
    if duration_ms <= _MIN_MS:
        return 0
    if duration_ms >= _MAX_MS:
        return _NUM_BUCKETS - 1
    idx = bisect.bisect_right(BUCKET_BOUNDARIES, duration_ms) - 1
    return max(0, min(idx, _NUM_BUCKETS - 1))


def merge_durations(
    histogram: dict[str, int], durations: list[float]
) -> dict[str, int]:
    """Merge a list of durations into an existing histogram. Returns updated histogram."""
    for d in durations:
        key = str(get_bucket_index(d))
        histogram[key] = histogram.get(key, 0) + 1
    return histogram


def percentile_from_histogram(
    histogram: dict[str, int], total_count: int, pct: float
) -> float | None:
    """Approximate a percentile value from a histogram.

    Returns the midpoint of the bucket containing the target percentile,
    or None if the histogram is empty.
    """
    if total_count <= 0 or not histogram:
        return None

    target = math.ceil(total_count * pct / 100.0)
    cumulative = 0

    # Iterate buckets in order
    for i in range(_NUM_BUCKETS):
        count = histogram.get(str(i), 0)
        if count <= 0:
            continue
        cumulative += count
        if cumulative >= target:
            # Return midpoint of this bucket
            low = BUCKET_BOUNDARIES[i]
            high = BUCKET_BOUNDARIES[i + 1] if i + 1 <= _NUM_BUCKETS else _MAX_MS
            return (low + high) / 2.0

    return None

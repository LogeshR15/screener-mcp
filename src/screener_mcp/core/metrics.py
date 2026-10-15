"""Process-wide request counters, so "is it slow, throttled or cached?" has an answer.

Cheap integers only — no per-request records. ``snapshot()`` backs
``screener-mcp --selfcheck`` and is logged at DEBUG when the client closes.
"""

import time
from collections import Counter

_counters: Counter = Counter()
_latency_total = 0.0
_started = time.monotonic()


def incr(name: str, n: int = 1) -> None:
    _counters[name] += n


def observe_latency(seconds: float) -> None:
    global _latency_total
    _latency_total += seconds


def snapshot() -> dict:
    requests = _counters["http_requests"]
    lookups = _counters["page_cache_hits"] + _counters["page_cache_misses"]
    return {
        "uptime_s": round(time.monotonic() - _started, 1),
        **dict(_counters),
        "avg_latency_ms": round(_latency_total / requests * 1000) if requests else None,
        "page_cache_hit_rate": round(_counters["page_cache_hits"] / lookups, 2) if lookups else None,
    }


def reset() -> None:
    global _latency_total
    _counters.clear()
    _latency_total = 0.0

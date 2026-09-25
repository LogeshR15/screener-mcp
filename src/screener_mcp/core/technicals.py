"""
Price-action metrics computed from Screener.in's public chart API.

  GET /api/company/{company_id}/chart/?q=Price-DMA50-DMA200-Volume&days=365

returns daily closes, 50/200-day moving averages, and volumes — no login
needed. Everything else (52-week range, DMA 20, RSI 14, volume vs its 20-day
average) is derived here, so technical screens work identically whether or not
Screener credentials are configured.

Note: the 52-week high/low here is on a *closing-price* basis. The overview's
"High / Low" on Screener.in may use intraday extremes, so they can differ a bit.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ..client import get_client

CHART_METRICS = "Price-DMA50-DMA200-Volume"
_STALE_AFTER_DAYS = 7


@dataclass
class PriceHistory:
    dates: list[str] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    volumes: list[Optional[float]] = field(default_factory=list)
    dma50: dict[str, float] = field(default_factory=dict)
    dma200: dict[str, float] = field(default_factory=dict)


def parse_chart(payload: dict) -> PriceHistory:
    hist = PriceHistory()
    volumes: dict[str, float] = {}
    for ds in (payload or {}).get("datasets", []):
        metric = ds.get("metric")
        values = ds.get("values", [])
        if metric == "Price":
            for point in values:
                try:
                    hist.dates.append(point[0])
                    hist.closes.append(float(point[1]))
                except (TypeError, ValueError, IndexError):
                    continue
        elif metric in ("DMA50", "DMA200"):
            target = hist.dma50 if metric == "DMA50" else hist.dma200
            for point in values:
                try:
                    target[point[0]] = float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
        elif metric == "Volume":
            for point in values:
                try:
                    volumes[point[0]] = float(point[1])
                except (TypeError, ValueError, IndexError):
                    continue
    hist.volumes = [volumes.get(d) for d in hist.dates]
    return hist


async def fetch_price_history(company_id: str, days: int = 365) -> PriceHistory:
    client = await get_client()
    payload = await client.get_json(
        f"/api/company/{company_id}/chart/",
        params={"q": CHART_METRICS, "days": str(days)},
    )
    return parse_chart(payload if isinstance(payload, dict) else {})


# ─── indicators ────────────────────────────────────────────────────────────────

def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI over the full series (needs period + 1 closes)."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def pct_return(closes: list[float], sessions: int) -> Optional[float]:
    if len(closes) <= sessions or closes[-1 - sessions] == 0:
        return None
    return (closes[-1] / closes[-1 - sessions] - 1) * 100


def _r(x: Optional[float], nd: int = 2) -> Optional[float]:
    return None if x is None else round(x, nd)


def compute_technicals(hist: PriceHistory, today: Optional[date] = None) -> tuple[dict, list[str]]:
    """Return (metrics, warnings). Metrics that can't be computed are None."""
    warnings: list[str] = []
    closes = hist.closes
    if not closes:
        return {}, ["No price history returned by Screener.in's chart API."]

    last_date = hist.dates[-1]
    price = closes[-1]
    window = closes[-250:]  # ≈ 52 weeks of sessions
    high_52w, low_52w = max(window), min(window)
    if len(closes) < 240:
        warnings.append(f"Only {len(closes)} sessions of history — 52-week range covers less than a year.")

    dma20 = sma(closes, 20)
    dma50 = hist.dma50.get(last_date) or sma(closes, 50)
    dma200 = hist.dma200.get(last_date) or sma(closes, 200)
    if dma200 is None:
        warnings.append("Not enough history for a 200 DMA.")

    vols = hist.volumes
    volume = vols[-1] if vols else None
    prior = [v for v in vols[-21:-1] if v is not None]
    avg_vol_20 = sum(prior) / len(prior) if len(prior) >= 15 else None
    volume_ratio = volume / avg_vol_20 if volume is not None and avg_vol_20 else None

    try:
        age = ((today or date.today()) - datetime.strptime(last_date, "%Y-%m-%d").date()).days
        if age > _STALE_AFTER_DAYS:
            warnings.append(f"Latest price is from {last_date} ({age} days old) — the stock may be suspended or illiquid.")
    except ValueError:
        pass

    def vs(level):
        return _r((price / level - 1) * 100) if level else None

    metrics = {
        "as_of": last_date,
        "price": _r(price),
        "high_52w": _r(high_52w),
        "low_52w": _r(low_52w),
        "pct_above_52w_low": _r((price / low_52w - 1) * 100) if low_52w else None,
        "pct_below_52w_high": _r((1 - price / high_52w) * 100) if high_52w else None,
        "dma20": _r(dma20),
        "dma50": _r(dma50),
        "dma200": _r(dma200),
        "pct_vs_dma20": vs(dma20),
        "pct_vs_dma50": vs(dma50),
        "pct_vs_dma200": vs(dma200),
        "rsi14": _r(rsi(closes, 14), 1),
        "volume": volume,
        "avg_volume_20d": _r(avg_vol_20, 0),
        "volume_vs_20d_avg": _r(volume_ratio),
        "return_1w_pct": _r(pct_return(closes, 5)),
        "return_1m_pct": _r(pct_return(closes, 21)),
        "return_3m_pct": _r(pct_return(closes, 63)),
        "basis": "daily closes (52W range is close-based)",
    }
    return metrics, warnings

"""
Commodity price analysis — analytical context for major commodities
traded on MCX/NCDEX and their impact on Indian listed companies.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..core.envelope import ToolError, ToolResult

logger = logging.getLogger(__name__)

_COMMODITY_INFO: dict[str, dict] = {
    "gold": {
        "name": "Gold",
        "unit": "₹/10g",
        "mcx_symbol": "GOLD",
        "impact": {
            "beneficiaries": ["Titan Company", "Kalyan Jewellers", "PC Jewellers", "Senco Gold", "Rajesh Exports"],
            "hurt_by_rise": ["Manufacturers using gold as input (high cost pressure)"],
            "watch_symbols": ["TITAN", "KALYANKJIL", "SENCO", "RAJESHEXPO"],
            "sector_note": "Gold price drives jewellery retail margins. Rising gold = revenue boost but margin pressure for retailers."
        }
    },
    "silver": {
        "name": "Silver",
        "unit": "₹/kg",
        "mcx_symbol": "SILVER",
        "impact": {
            "beneficiaries": ["Hindustan Copper (indirect)", "EV battery manufacturers"],
            "hurt_by_rise": ["Electronics manufacturers, solar panel makers"],
            "watch_symbols": ["HINDZINC", "VEDL"],
            "sector_note": "Silver has both industrial (EVs, solar, electronics) and investment demand."
        }
    },
    "crude_oil": {
        "name": "Crude Oil",
        "unit": "₹/barrel",
        "mcx_symbol": "CRUDEOIL",
        "impact": {
            "beneficiaries": ["IOC", "BPCL", "HPCL", "Reliance Industries (refining margin)"],
            "hurt_by_rise": ["Asian Paints", "Pidilite", "IndiGo", "SpiceJet", "Tyre cos (CEAT, MRF, Apollo)"],
            "watch_symbols": ["RELIANCE", "IOC", "BPCL", "HINDPETRO", "ASIANPAINT", "PIDILITIND", "INDIGO", "APOLLOTYRE", "MRF", "CEATLTD"],
            "sector_note": "Crude is the most impactful commodity for Indian markets — affects paints, tyres, aviation, fertilizers, and OMCs."
        }
    },
    "copper": {
        "name": "Copper",
        "unit": "₹/kg",
        "mcx_symbol": "COPPER",
        "impact": {
            "beneficiaries": ["Hindustan Copper", "Vedanta (copper smelting)"],
            "hurt_by_rise": ["Havells India", "Polycab", "KEI Industries", "Voltas", "Thermax (motor windings)"],
            "watch_symbols": ["HINDCOPPER", "VEDL", "HAVELLS", "POLYCAB", "KEI", "VOLTAS"],
            "sector_note": "Copper is critical for EV charging infra, wiring, and industrial motors. EV boom = long-term demand driver."
        }
    },
    "aluminium": {
        "name": "Aluminium",
        "unit": "₹/kg",
        "mcx_symbol": "ALUMINIUM",
        "impact": {
            "beneficiaries": ["Hindalco Industries", "NALCO", "Vedanta"],
            "hurt_by_rise": ["Auto OEMs (Maruti, Tata Motors)", "Packaging companies", "Aerospace suppliers"],
            "watch_symbols": ["HINDALCO", "NATIONALUM", "VEDL", "MARUTI"],
            "sector_note": "Aluminium demand growing with EVs (lighter body parts) and renewable energy (solar frames)."
        }
    },
    "zinc": {
        "name": "Zinc",
        "unit": "₹/kg",
        "mcx_symbol": "ZINC",
        "impact": {
            "beneficiaries": ["Hindustan Zinc (HZL) — India's dominant producer, ~75% market share"],
            "hurt_by_rise": ["Steel companies needing galvanizing (Tata Steel, JSW Steel)"],
            "watch_symbols": ["HINDZINC", "VEDL", "TATASTEEL", "JSWSTEEL"],
            "sector_note": "HZL is virtually a pure-play on zinc prices. Parent: Vedanta."
        }
    },
    "nickel": {
        "name": "Nickel",
        "unit": "₹/kg",
        "mcx_symbol": "NICKEL",
        "impact": {
            "beneficiaries": ["Vedanta (limited)", "Import traders"],
            "hurt_by_rise": ["EV battery manufacturers (NMC batteries)", "Stainless steel producers"],
            "watch_symbols": ["VEDL", "JSL"],
            "sector_note": "Nickel is a critical EV battery input — high nickel chemistry (NMC) dominates EV packs. India imports most nickel."
        }
    },
    "cotton": {
        "name": "Cotton",
        "unit": "₹/bale (170 kg)",
        "mcx_symbol": "COTTON",
        "impact": {
            "beneficiaries": ["Cotton traders", "Gin/Mill operators during high-price cycles"],
            "hurt_by_rise": ["Page Industries", "Vardhman Textiles", "Welspun India", "Trident Group"],
            "watch_symbols": ["PAGEIND", "VTL", "WELSPUNLIV", "TRIDENT", "ARVIND", "KPRMILL"],
            "sector_note": "Cotton is the primary input for India's textile industry. MSP (minimum support price) and monsoon drive price."
        }
    },
    "natural_gas": {
        "name": "Natural Gas",
        "unit": "₹/mmBtu",
        "mcx_symbol": "NATURALGAS",
        "impact": {
            "beneficiaries": ["City gas distribution: IGL, MGL, Gujarat Gas, Adani Gas"],
            "hurt_by_rise": ["Fertilizer makers: RCF, GNFC, Chambal Fertilisers (gas = 70-80% input cost)", "Chemicals: ONGC Petro, GSFC"],
            "watch_symbols": ["IGL", "MGL", "GUJGASLTD", "RCF", "GNFC", "CHAMBLFERT"],
            "sector_note": "India's gas price linked to APM (Admin Price Mechanism) — revised every 6 months. CGD companies pass through to consumers."
        }
    },
    "steel": {
        "name": "Steel (HRC)",
        "unit": "₹/tonne",
        "mcx_symbol": "STEEL",
        "impact": {
            "beneficiaries": ["Tata Steel", "JSW Steel", "SAIL", "JSPL"],
            "hurt_by_rise": ["Auto OEMs", "Capital goods", "Real estate developers", "White goods (Voltas, Whirlpool)"],
            "watch_symbols": ["TATASTEEL", "JSWSTEEL", "SAIL", "JINDALSTEL", "MARUTI", "VOLTAS"],
            "sector_note": "Steel is linked to China demand/supply, iron ore prices, and domestic infra spending (railways, construction)."
        }
    },
    "wheat": {
        "name": "Wheat",
        "unit": "₹/quintal",
        "mcx_symbol": "WHEAT",
        "indian_contract": "NCDEX WHEAT",
        "impact": {
            "beneficiaries": ["Agri-commodity exporters when export windows open"],
            "hurt_by_rise": ["ITC (atta, biscuits — Aashirvaad, Sunfeast)", "Britannia", "Nestle India",
                             "Flour millers"],
            "watch_symbols": ["ITC", "BRITANNIA", "NESTLEIND"],
            "sector_note": ("India's wheat price is set more by the MSP, FCI procurement and export curbs than by "
                            "CBOT — treat the CBOT benchmark as direction only."),
        }
    },
    "tobacco": {
        "name": "Leaf Tobacco",
        "unit": "₹/kg (Tobacco Board auctions)",
        "mcx_symbol": "TOBACCO",
        "indian_contract": None,  # Tobacco Board auctions, no exchange contract
        "impact": {
            "beneficiaries": ["Leaf exporters (ITC's agri business, Godfrey Phillips' leaf trade)"],
            "hurt_by_rise": ["ITC (cigarettes)", "Godfrey Phillips", "VST Industries"],
            "watch_symbols": ["ITC", "GODFRYPHLP", "VSTIND"],
            "sector_note": ("Leaf tobacco is auctioned by the Tobacco Board (FCV, Andhra Pradesh/Karnataka); there "
                            "is no exchange-traded contract. For cigarette makers, excise/NCCD/GST changes "
                            "matter far more than leaf cost."),
        }
    },
    "wood_pulp": {
        "name": "Wood Pulp",
        "unit": "USD/tonne (NBSK/BHKP)",
        "mcx_symbol": "PULP",
        "indian_contract": None,  # imported; no Indian exchange contract
        "impact": {
            "beneficiaries": ["Integrated paper makers with captive plantations"],
            "hurt_by_rise": ["ITC (paperboards — partly captive pulp)", "JK Paper", "West Coast Paper",
                             "Seshasayee Paper", "Andhra Paper"],
            "watch_symbols": ["ITC", "JKPAPER", "WSTCSTPAPR", "SESHAPAPER", "ANDHRAPAP"],
            "sector_note": ("Pulp has no free public futures feed (SHFE pulp is not on Yahoo). Imported pulp prices "
                            "move paper margins; integrated mills are hedged."),
        }
    },
}

_ALIASES = {"crude": "crude_oil", "oil": "crude_oil", "brent": "crude_oil", "gas": "natural_gas",
            "pulp": "wood_pulp", "paper_pulp": "wood_pulp", "leaf_tobacco": "tobacco"}


# International benchmark futures (Yahoo Finance chart API — public, no key).
# MCX's own site blocks scripted access, so the old MCX scrape almost never
# returned a price. These are the global contracts MCX prices track; the INR
# figure is a straight FX conversion and excludes import duty / GST, so it
# will sit below the MCX quote.
# mcx_symbol → (yahoo symbol, contract, unit, INR conversion: (factor, inr unit) or None)
_OZ_PER_10G = 10 / 31.1035
_BENCHMARKS: dict[str, tuple[str, str, str, tuple[float, str] | None]] = {
    "GOLD": ("GC=F", "COMEX gold futures", "USD/troy oz", (_OZ_PER_10G, "₹/10g")),
    "SILVER": ("SI=F", "COMEX silver futures", "USD/troy oz", (1000 / 31.1035, "₹/kg")),
    "CRUDEOIL": ("BZ=F", "ICE Brent crude futures", "USD/barrel", (1.0, "₹/barrel")),
    "COPPER": ("HG=F", "COMEX copper futures", "USD/lb", (2.20462, "₹/kg")),
    "ALUMINIUM": ("ALI=F", "COMEX aluminium futures", "USD/tonne", (0.001, "₹/kg")),
    "ZINC": ("ZNC=F", "COMEX zinc futures", "USD/tonne", (0.001, "₹/kg")),
    "COTTON": ("CT=F", "ICE cotton No.2 futures", "US cents/lb", None),
    "NATURALGAS": ("NG=F", "NYMEX Henry Hub natural gas futures", "USD/mmBtu", (1.0, "₹/mmBtu")),
    "STEEL": ("HRC=F", "CME US Midwest hot-rolled coil futures", "USD/short ton", None),
    "WHEAT": ("ZW=F", "CBOT wheat futures", "US cents/bushel", None),
}


async def _yahoo_chart(symbol: str, years: int) -> dict:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": f"{max(1, min(years, 10))}y", "interval": "1wk"}
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        resp = await client.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        result = (resp.json().get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"no data for {symbol}")
    r = result[0]
    closes = [c for c in (r.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
    if not closes:
        raise ValueError(f"no closes for {symbol}")
    return {"meta": r.get("meta", {}), "closes": closes, "timestamps": r.get("timestamp") or []}


def _pct(a: float, b: float) -> float | None:
    return round((a / b - 1) * 100, 2) if b else None


async def _fetch_benchmark(mcx_symbol: str, years: int) -> tuple[dict | None, str | None]:
    """→ (benchmark dict, None) or (None, reason it's unavailable)."""
    spec = _BENCHMARKS.get(mcx_symbol)
    if not spec:
        return None, "No free public benchmark feed covers this commodity."
    symbol, contract, unit, inr = spec
    try:
        chart, fx = await asyncio.gather(_yahoo_chart(symbol, years), _yahoo_chart("INR=X", 1))
    except Exception as e:
        logger.warning("benchmark fetch failed for %s: %s", symbol, e)
        return None, f"Benchmark price feed unavailable ({type(e).__name__})."

    closes = chart["closes"]
    price = chart["meta"].get("regularMarketPrice") or closes[-1]
    usd_inr = fx["meta"].get("regularMarketPrice") or fx["closes"][-1]
    ts = chart["meta"].get("regularMarketTime")
    out = {
        "contract": contract,
        "source_symbol": symbol,
        "price": round(price, 4),
        "unit": unit,
        "as_of": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else None,
        "change_4w_pct": _pct(price, closes[-5]) if len(closes) > 5 else None,
        "change_52w_pct": _pct(price, closes[-53]) if len(closes) > 53 else None,
        "change_period_pct": _pct(price, closes[0]),
        "period_years": years,
        "period_high": round(max(closes), 4),
        "period_low": round(min(closes), 4),
        "usd_inr": round(usd_inr, 4),
    }
    if inr:
        factor, inr_unit = inr
        out["approx_inr"] = round(price * factor * usd_inr, 2)
        out["approx_inr_unit"] = inr_unit
        out["approx_inr_basis"] = "FX conversion of the international benchmark; excludes import duty and GST, so below the MCX price"
    return out, None


def _indian_contract(info: dict) -> str:
    contract = info.get("indian_contract", f"MCX {info['mcx_symbol']}")
    return f"{contract} ({info['unit']})" if contract else f"none — priced in {info['unit']}"


async def get_commodity_prices(commodity: str, years: int = 5) -> ToolResult:
    """
    Get commodity price context and impact analysis for Indian listed companies.

    commodity: gold | silver | crude_oil | copper | aluminium | zinc | nickel | cotton | natural_gas
               | steel | wheat | tobacco | wood_pulp
    years: history window for the benchmark moves and range (1-10)
    """
    key = commodity.lower().strip().replace(" ", "_").replace("-", "_")
    key = _ALIASES.get(key, key)

    if key not in _COMMODITY_INFO:
        available = ", ".join(sorted(_COMMODITY_INFO.keys()))
        raise ToolError(
            f"Unknown commodity: '{commodity}'. Supported: {available}. "
            "Example: get_commodity_prices('crude_oil')",
            "invalid_input",
        )

    info = _COMMODITY_INFO[key]
    impact = info["impact"]

    years = max(1, min(int(years or 5), 10))
    benchmark, unavailable = await _fetch_benchmark(info["mcx_symbol"], years)

    lines = [
        f"# {info['name']} — Commodity Analysis",
        f"Indian contract: {_indian_contract(info)} | Context period: {years} years",
        "",
    ]

    if benchmark:
        b = benchmark
        lines.append(f"**{b['contract']}:** {b['price']} {b['unit']} (as of {b['as_of']})")
        moves = [f"4w {b['change_4w_pct']:+.1f}%" if b["change_4w_pct"] is not None else None,
                 f"52w {b['change_52w_pct']:+.1f}%" if b["change_52w_pct"] is not None else None,
                 f"{years}y {b['change_period_pct']:+.1f}%" if b["change_period_pct"] is not None else None]
        lines.append(f"**Moves:** {' | '.join(m for m in moves if m)} | {years}y range {b['period_low']}–{b['period_high']}")
        if b.get("approx_inr"):
            lines.append(f"**≈ {b['approx_inr']:,} {b['approx_inr_unit']}** at USD/INR {b['usd_inr']} (before import duty/GST — MCX trades higher)")
        lines.append("")

    lines += [
        "## Market Impact on Indian Listed Companies",
        "",
        f"**Sector Note:** {impact['sector_note']}",
        "",
        "**Companies that benefit from higher prices:**",
    ]
    for co in impact["beneficiaries"]:
        lines.append(f"  + {co}")

    lines.append("")
    lines.append("**Companies hurt by higher prices (input cost pressure):**")
    for co in impact["hurt_by_rise"]:
        lines.append(f"  - {co}")

    lines += [
        "",
        "## How to Use in Your Analysis",
        "",
        f"1. **Direction check:** Is {info['name']} trending up or down over {years} years?",
        "   - Uptrend → tailwind for producers, headwind for users",
        "   - Downtrend → margin relief for users, pain for producers",
        "",
        "2. **Margin impact:** Check quarterly results of affected companies — did OPM% move with commodity?",
        "   - Use `get_quarterly_results(symbol)` to verify",
        "",
        "3. **Compare the exposed companies:**",
        f"   compare_companies({impact['watch_symbols'][:6]})",
        "",
        "## Live Price Sources",
        "  - MCX India (official): https://www.mcxindia.com",
        "  - NCDEX (agri): https://www.ncdex.com",
        "  - Investing.com (charts + history): https://www.investing.com/commodities",
        "  - Moneycontrol Commodities: https://www.moneycontrol.com/commodity",
        "",
        "**Note:** The benchmark is the international contract MCX/NCDEX track, not the domestic quote —",
        "use MCX/NCDEX for the Indian price.",
    ]

    return ToolResult(
        data={"commodity": key, "benchmark": benchmark, "watch_symbols": impact["watch_symbols"],
              "report": "\n".join(lines)},
        missing_fields=[] if benchmark else ["benchmark_price"],
        reason=None if benchmark else f"{unavailable} The analysis below is context only.",
    )

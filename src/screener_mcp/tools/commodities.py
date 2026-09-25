"""
Commodity price analysis — analytical context for major commodities
traded on MCX/NCDEX and their impact on Indian listed companies.
"""

import logging
import re

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
            "screener_query": 'screen_stocks("Sales growth 3Years > 10 AND Return on capital employed > 12")',
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
            "screener_query": 'screen_stocks("Market Capitalization < 5000 AND Return on capital employed > 15")',
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
            "screener_query": 'screen_stocks("Return on capital employed > 15 AND Debt to equity < 0.5")',
            "sector_note": "Crude is the most impactful commodity for Indian markets — affects paints, tyres, aviation, fertilizers, and OMCs."
        }
    },
    "crude": {
        "name": "Crude Oil",
        "unit": "₹/barrel",
        "mcx_symbol": "CRUDEOIL",
        "impact": {
            "beneficiaries": ["IOC", "BPCL", "HPCL", "Reliance Industries"],
            "hurt_by_rise": ["Asian Paints", "Pidilite", "IndiGo", "Tyre companies"],
            "screener_query": 'screen_stocks("Return on capital employed > 15 AND Debt to equity < 0.5")',
            "sector_note": "See: crude_oil"
        }
    },
    "copper": {
        "name": "Copper",
        "unit": "₹/kg",
        "mcx_symbol": "COPPER",
        "impact": {
            "beneficiaries": ["Hindustan Copper", "Vedanta (copper smelting)"],
            "hurt_by_rise": ["Havells India", "Polycab", "KEI Industries", "Voltas", "Thermax (motor windings)"],
            "screener_query": 'screen_stocks("Sales growth 3Years > 15 AND Return on capital employed > 20")',
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
            "screener_query": 'screen_stocks("Return on capital employed > 12 AND Debt to equity < 1")',
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
            "screener_query": 'search_company("Hindustan Zinc")',
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
            "screener_query": 'screen_stocks("Sales growth 3Years > 20 AND Market Capitalization < 10000")',
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
            "screener_query": 'screen_by_theme("chemicals")',
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
            "screener_query": 'screen_stocks("Sales growth 3Years > 10 AND Return on equity > 15")',
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
            "screener_query": 'screen_stocks("Return on capital employed > 12 AND Debt to equity < 1.5")',
            "sector_note": "Steel is linked to China demand/supply, iron ore prices, and domestic infra spending (railways, construction)."
        }
    },
}


async def _fetch_mcx_price(mcx_symbol: str) -> str | None:
    """Attempt to fetch current price from MCX India (best-effort)."""
    try:
        url = f"https://www.mcxindia.com/market-data/commodity-futures/{mcx_symbol.lower()}"
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                return None
            match = re.search(r'"lastPrice"\s*:\s*"?([\d,]+\.?\d*)"?', resp.text)
            if match:
                return match.group(1).replace(",", "")
    except Exception:
        pass
    return None


async def get_commodity_prices(commodity: str, years: int = 5) -> str:
    """
    Get commodity price context and impact analysis for Indian listed companies.

    commodity: gold | silver | crude_oil | copper | aluminium | zinc | nickel | cotton | natural_gas | steel
    years: historical context period to reference in analysis (1-10)
    """
    key = commodity.lower().replace(" ", "_").replace("-", "_")

    if key not in _COMMODITY_INFO:
        available = ", ".join(sorted(_COMMODITY_INFO.keys()))
        raise ToolError(
            f"Unknown commodity: '{commodity}'. Supported: {available}. "
            "Example: get_commodity_prices('crude_oil')",
            "invalid_input",
        )

    info = _COMMODITY_INFO[key]
    impact = info["impact"]

    current_price = await _fetch_mcx_price(info["mcx_symbol"])

    lines = [
        f"# {info['name']} — Commodity Analysis",
        f"Exchange: MCX India | Unit: {info['unit']} | Context period: {years} years",
        "",
    ]

    if current_price:
        lines.append(f"**MCX Spot Price (approx):** {info['unit'].split('/')[0]} {current_price}")
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
        "3. **Screener query to find affected companies:**",
        f"   {impact['screener_query']}",
        "",
        "## Live Price Sources",
        "  - MCX India (official): https://www.mcxindia.com",
        "  - NCDEX (agri): https://www.ncdex.com",
        "  - Investing.com (charts + history): https://www.investing.com/commodities",
        "  - Moneycontrol Commodities: https://www.moneycontrol.com/commodity",
        "",
        "**Note:** This tool provides analytical context. For live trading prices and historical charts,",
        "use MCX/NCDEX directly. Screener.in's commodity data covers 10,000+ commodities (premium feature).",
    ]

    return ToolResult(
        data={"report": "\n".join(lines), "mcx_price": current_price or None},
        missing_fields=[] if current_price else ["mcx_price"],
        reason=None if current_price else (
            "Live MCX price couldn't be fetched (best-effort source) — the analysis below is "
            "context only; check MCX directly for the current price."
        ),
    )

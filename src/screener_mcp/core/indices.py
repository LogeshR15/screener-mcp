"""
NSE index pages on Screener.in — used as public stock universes for technical
screens and as sector benchmarks for relative-performance comparisons.

Each index has a Screener page (/company/<slug>/) with a paginated, public
constituents table and the same chart API as a stock. Slugs are Screener's own
(verified against its search API); ``company_id`` is the chart-API id.
"""

import re
from typing import Optional

# key → (Screener slug, company_id, display name)
INDICES: dict[str, tuple[str, str, str]] = {
    "nifty50": ("NIFTY", "1272594", "Nifty 50"),
    "nifty500": ("CNX500", "1272615", "Nifty 500"),
    "midcap100": ("CNXMIDCAP", "1272674", "Nifty Midcap 100"),
    "smallcap100": ("CNXSMALLCA", "1272791", "Nifty Smallcap 100"),
    "smallcap250": ("SMALLCA250", "1275142", "Nifty Smallcap 250"),
    "bank": ("BANKNIFTY", "1272670", "Nifty Bank"),
    "private_bank": ("NIFPVTBANK", "1274024", "Nifty Private Bank"),
    "psu_bank": ("CNXPSUBANK", "1272693", "Nifty PSU Bank"),
    "financial_services": ("CNXFINANCE", "1272803", "Nifty Financial Services"),
    "it": ("CNXIT", "1272649", "Nifty IT"),
    "auto": ("CNXAUTO", "1272796", "Nifty Auto"),
    "pharma": ("CNXPHARMA", "1272672", "Nifty Pharma"),
    "healthcare": ("NFTHEALTHC", "1275139", "Nifty Healthcare"),
    "fmcg": ("CNXFMCG", "1272711", "Nifty FMCG"),
    "metal": ("CNXMETAL", "1272797", "Nifty Metal"),
    "realty": ("CNXREALTY", "1272692", "Nifty Realty"),
    "energy": ("CNXENERY", "1272671", "Nifty Energy"),
    "oil_gas": ("NIFTOILGAS", "1275137", "Nifty Oil & Gas"),
    "infrastructure": ("CNXINFRAST", "1272689", "Nifty Infrastructure"),
    "pse": ("CNXPSE", "1272714", "Nifty PSE"),
    "media": ("CNXMEDIA", "1272799", "Nifty Media"),
    "defence": ("NIFINDDEFE", "1285154", "Nifty India Defence"),
    "chemicals": ("NFTCHEMIC", "1285958", "Nifty Chemicals"),
    "ev": ("NIFTYEVNAA", "1285263", "Nifty EV & New Age Automotive"),
    "consumer_durables": ("NFTCONSDUR", "1275138", "Nifty Consumer Durables"),
    "capital_markets": ("NIFCAPMARK", "1285161", "Nifty Capital Markets"),
    "commodities": ("CNXCOMMODI", "1272802", "Nifty Commodities"),
    "services": ("CNXSERVICE", "1272770", "Nifty Services Sector"),
}

_ALIASES = {
    "nifty": "nifty50", "nifty 50": "nifty50", "banknifty": "bank", "nifty bank": "bank",
    "midcap": "midcap100", "smallcap": "smallcap100", "defense": "defence",
    "finance": "financial_services", "financials": "financial_services",
    "infra": "infrastructure", "psu": "pse", "oil and gas": "oil_gas",
    "technology": "it", "information technology": "it",
}

# Screener sector / industry labels (lower-cased substrings) → index key.
# Checked most-specific first: industry labels before broad sector labels.
_SECTOR_RULES: list[tuple[str, str]] = [
    ("private sector bank", "private_bank"),
    ("public sector bank", "psu_bank"),
    ("bank", "bank"),
    ("aerospace & defense", "defence"),
    ("defence", "defence"),
    ("pharmaceutical", "pharma"),
    ("healthcare", "healthcare"),
    ("information technology", "it"),
    ("it - software", "it"),
    ("automobile", "auto"),
    ("auto components", "auto"),
    ("fast moving consumer goods", "fmcg"),
    ("metals & mining", "metal"),
    ("realty", "realty"),
    ("oil, gas", "oil_gas"),
    ("power", "energy"),
    ("chemicals", "chemicals"),
    ("consumer durables", "consumer_durables"),
    ("media", "media"),
    ("capital markets", "capital_markets"),
    ("financial services", "financial_services"),
    ("construction", "infrastructure"),
    ("capital goods", "infrastructure"),
    ("telecommunication", "services"),
    ("services", "services"),
]


def resolve_index(name: str) -> Optional[tuple[str, str, str, str]]:
    """'nifty500' / 'Nifty 500' / 'CNX500' → (key, slug, company_id, display)."""
    if not name:
        return None
    raw = name.strip().lower()
    key = _ALIASES.get(raw, re.sub(r"[\s\-]+", "_", raw).replace("nifty_", ""))
    if key in INDICES:
        return (key, *INDICES[key])
    compact = re.sub(r"[^a-z0-9]", "", raw)
    for k, (slug, cid, display) in INDICES.items():
        if compact in (slug.lower(), re.sub(r"[^a-z0-9]", "", display.lower()), k.replace("_", "")):
            return (k, slug, cid, display)
    return None


def sector_index_for(sectors: list[str]) -> Optional[tuple[str, str, str, str]]:
    """Best sector benchmark for a company's Screener sector/industry labels."""
    labels = [s.lower() for s in sectors or []]
    for needle, key in _SECTOR_RULES:
        # industry (last label) is more specific — try it first
        for label in reversed(labels):
            if needle in label:
                return (key, *INDICES[key])
    return None

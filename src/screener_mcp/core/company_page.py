"""
Resolve a user-supplied symbol to a Screener.in company page and fetch it.

Every symbol-taking tool goes through ``fetch_company_page`` so they all share:

  * Symbol resolution — a non-canonical input ("MOTHERSONWIR", "Garden Reach",
    "HDFCBANKK") is resolved via Screener's search API, shortening the input
    prefix until something matches. A confident match is used and reported as
    "interpreted as <SYMBOL>"; an ambiguous one raises a ``ToolError`` carrying
    the top 3 candidates, so the caller can retry in one step.

  * Consolidated → standalone fallback — companies without subsidiaries publish
    no consolidated statements, and Screener still serves their /consolidated/
    page, just with every ratio and table blank. That looked identical to real
    data before; now we detect it, fetch the standalone page, and say so.

  * A short-lived page cache, so tools that fetch the same company repeatedly
    (screens, comparisons) don't re-download it.
"""

import difflib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from ..client import get_client
from .envelope import ToolError

FINANCIAL_TYPES = ("consolidated", "standalone")

_PAGE_CACHE_TTL = 300  # seconds
_PAGE_CACHE_MAX = 128
_page_cache: dict[str, tuple[float, str]] = {}

_MAX_SEARCHES = 8  # bound on prefix searches per resolution
_SLUG_RE = re.compile(r"^[A-Z0-9&\-_.]+$|^ID/\d+$")
_COMPANY_ID_RE = re.compile(r'data-company-id="(\d+)"')


@dataclass
class Candidate:
    symbol: str  # the slug used in /company/<slug>/
    name: str
    company_id: Optional[str] = None
    has_consolidated: bool = False
    score: float = 0.0

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "name": self.name, "match_score": round(self.score, 2)}


@dataclass
class CompanyPage:
    requested_symbol: str
    symbol: str
    financial_type: str  # the statement type actually served
    html: str
    company_id: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    interpreted_as: Optional[str] = None

    @property
    def meta(self) -> dict:
        meta = {"symbol": self.symbol, "financial_type": self.financial_type}
        if self.interpreted_as:
            meta["requested_symbol"] = self.requested_symbol
            meta["interpreted_as"] = self.interpreted_as
        return meta


# ─── symbol normalisation & search ─────────────────────────────────────────────

def normalize_symbol(raw: str) -> str:
    """'nse:msumi ', 'MSUMI.NS', '/company/MSUMI/' → 'MSUMI'."""
    s = (raw or "").strip().upper()
    s = re.sub(r"^(NSE|BSE)\s*:\s*", "", s)
    s = re.sub(r"\.(NS|BO)$", "", s)
    m = re.search(r"/COMPANY/((?:ID/)?[^/]+)", s)
    if m:
        s = m.group(1)
    return s.strip("/ ")


def _slug_from_url(url: str) -> str:
    m = re.search(r"/company/((?:id/)?[^/]+)/", url or "")
    return m.group(1).upper() if m else ""


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _name_match(query: str, name: str) -> tuple[float, list[str]]:
    """How well ``query`` spells out the company name → (score 0..0.99, segments).

    Coverage is the share of ``query`` consumed by prefixes of the name's words
    ("MOTHERSONWIR" vs "Motherson Sumi Wiring India" consumes MOTHERSON + WIR,
    skipping SUMI → 1.0). Words can be skipped but not reordered. Ties are
    broken towards whole-word matches ("INFOSYS" → "Infosys Ltd" over
    "HCL Infosystems"), matches starting at the first word, and fewer skipped
    words ("BAJAJFINANCE" → "Bajaj Finance" over "Bajaj Housing Finance").

    ``segments`` is how the query split across the name's words (["MOTHERSON",
    "WIR"]) — used to re-search with spaces, which Screener's search handles
    much better than a run-together token.
    """
    if not query:
        return 0.0, []
    words = [_compact(w) for w in re.split(r"[\s&\-,.()]+", name) if _compact(w)]
    pos = 0
    matched_word_chars = 0
    first_word_hit = False
    skipped_inner = 0
    pending_skips = 0
    segments: list[str] = []
    for i, w in enumerate(words):
        if pos >= len(query):
            break
        k = 0
        while k < len(w) and pos + k < len(query) and w[k] == query[pos + k]:
            k += 1
        # A 1-2 char overlap is noise unless it's the whole word ("& Co", "M&M")
        if k >= 3 or (k == len(w) and k > 0):
            segments.append(query[pos:pos + k])
            pos += k
            matched_word_chars += len(w)
            first_word_hit = first_word_hit or i == 0
            if len(segments) > 1:
                skipped_inner += pending_skips
            pending_skips = 0
        else:
            pending_skips += 1
    if not pos:
        return 0.0, []
    coverage = pos / len(query)
    precision = pos / matched_word_chars
    score = coverage * (0.85 + 0.12 * precision + 0.02 * first_word_hit) - 0.02 * skipped_inner
    return min(0.99, max(0.0, score)), segments


def score_candidate(query: str, cand: Candidate) -> float:
    q = _compact(query)
    sym = _compact(cand.symbol.split("/")[-1]) if not cand.symbol.startswith("ID/") else ""
    if q and q == sym:
        return 1.0
    symbol_sim = difflib.SequenceMatcher(None, q, sym).ratio() if sym else 0.0
    score = max(symbol_sim, _name_match(q, cand.name)[0])
    if re.search(r"\((merged|delisted|old)\)", cand.name, re.I):
        score *= 0.85
    return score


async def search_candidates(query: str) -> list[Candidate]:
    """Raw Screener search — one request, no scoring."""
    client = await get_client()
    results = await client.get_json("/api/company/search/", params={"q": query})
    out = []
    for item in results if isinstance(results, list) else []:
        url = item.get("url", "")
        slug = _slug_from_url(url)
        if not slug:
            continue
        out.append(Candidate(
            symbol=slug,
            name=item.get("name", ""),
            company_id=str(item["id"]) if item.get("id") is not None else None,
            has_consolidated=url.rstrip("/").endswith("consolidated"),
        ))
    return out


def _merge(by_symbol: dict[str, Candidate], found: list[Candidate], query: str):
    for c in found:
        if c.symbol not in by_symbol:
            c.score = score_candidate(query, c)
            by_symbol[c.symbol] = c


def _search_queries(raw: str) -> list[str]:
    """The original input, then progressively shorter prefixes of it.

    Screener's search is prefix-based on names and symbols, so "MOTHERSONWIR"
    finds nothing while "MOTHERSON" finds both Motherson companies.
    """
    raw = raw.strip()
    queries = [raw]
    token = _compact(raw)
    if " " not in raw and len(token) >= 5:
        floor = max(4, len(token) // 3)
        queries += [token[:n] for n in range(len(token) - 1, floor - 1, -1)]
    return queries


async def resolve_symbol(raw: str) -> tuple[Candidate, list[Candidate]]:
    """Resolve free-form input to one confident Candidate.

    Returns (best, alternatives). Raises ToolError(symbol_not_found) with the
    top 3 candidates when nothing matches confidently.
    """
    query = raw.strip()
    by_symbol: dict[str, Candidate] = {}
    distinct_sets = 0
    last_set: frozenset = frozenset()
    tried = set()
    for q in _search_queries(query)[:_MAX_SEARCHES]:
        tried.add(q.upper())
        found = await search_candidates(q)
        found_set = frozenset(c.symbol for c in found)
        if not found or found_set == last_set:
            continue
        distinct_sets += 1
        last_set = found_set
        _merge(by_symbol, found, query)
        # A shorter prefix widens the net ("BAJAJFIN" misses Bajaj Finance,
        # "BAJAJ" finds it), so keep going until something scores highly.
        if max(c.score for c in by_symbol.values()) >= 0.9 or distinct_sets >= 3:
            break

    # Re-search with the word split learned from the best name match
    # ("BAJAJFINANCE" → "BAJAJ FINANCE"), which surfaces the exact company
    # when a prefix search only found its siblings.
    if by_symbol:
        best_so_far = max(by_symbol.values(), key=lambda c: c.score)
        segments = _name_match(_compact(query), best_so_far.name)[1]
        spaced = " ".join(segments)
        if best_so_far.score < 1.0 and len(segments) > 1 and spaced.upper() not in tried:
            _merge(by_symbol, await search_candidates(spaced), query)

    if not by_symbol:
        raise ToolError(
            f"No Screener.in company matches '{raw}'. Try the company name "
            "(e.g. 'Motherson Wiring') or the exact NSE/BSE code.",
            "symbol_not_found",
            requested_symbol=raw,
            candidates=[],
        )

    ranked = sorted(by_symbol.values(), key=lambda c: c.score, reverse=True)
    if query.isdigit():
        # A numeric BSE code is an exact lookup on Screener's side
        ranked[0].score = 1.0
    best = ranked[0]
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    margin = best.score - runner_up

    confident = (
        best.score == 1.0
        or (best.score >= 0.97 and margin >= 0.05)
        or (best.score >= 0.9 and margin >= 0.08)
        or (best.score >= 0.85 and margin >= 0.15)
    )
    if not confident:
        top = ranked[:3]
        raise ToolError(
            f"'{raw}' is not a Screener.in symbol and no single company matches it "
            f"confidently. Did you mean: "
            + ", ".join(f"{c.symbol} ({c.name})" for c in top)
            + "? Retry with one of these symbols.",
            "symbol_ambiguous",
            requested_symbol=raw,
            candidates=[c.as_dict() for c in top],
        )
    return best, ranked[1:3]


# ─── page fetching ─────────────────────────────────────────────────────────────

def _company_path(symbol: str, financial_type: str) -> str:
    path = f"/company/{symbol}/"
    if financial_type == "consolidated":
        path += "consolidated/"
    return path


async def _get_page(symbol: str, financial_type: str) -> Optional[str]:
    """Fetch a company page (cached). None on 404."""
    path = _company_path(symbol, financial_type)
    now = time.monotonic()
    hit = _page_cache.get(path)
    if hit and now - hit[0] < _PAGE_CACHE_TTL:
        return hit[1]
    client = await get_client()
    try:
        html = await client.get_html(path)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return None
        raise
    if len(_page_cache) >= _PAGE_CACHE_MAX:
        _page_cache.pop(min(_page_cache, key=lambda k: _page_cache[k][0]))
    _page_cache[path] = (now, html)
    return html


def clear_page_cache():
    _page_cache.clear()


def page_has_data(html: str) -> bool:
    """True if the page carries a price or any P&L values.

    A /consolidated/ page for a company with no subsidiaries renders the full
    layout with every number empty — this is how we tell it apart.
    """
    soup = BeautifulSoup(html, "lxml")
    top = soup.find(id="top-ratios")
    if top and any(s.get_text(strip=True) for s in top.select("span.number")):
        return True
    pl = soup.find(id="profit-loss")
    if pl:
        for tr in pl.select("tbody tr"):
            # first cell is the row label ("Sales", "Expenses"...) — skip it
            if any(td.get_text(strip=True) for td in tr.find_all("td")[1:]):
                return True
    return False


async def fetch_company_page(symbol: str, financial_type: str = "consolidated") -> CompanyPage:
    requested = symbol
    sym = normalize_symbol(symbol)
    if not sym:
        raise ToolError("Please provide an NSE/BSE symbol or company name.", "invalid_input")

    warnings: list[str] = []
    ft = (financial_type or "consolidated").lower()
    if ft not in FINANCIAL_TYPES:
        warnings.append(f"Unknown financial_type '{financial_type}' — using consolidated.")
        ft = "consolidated"

    html = await _get_page(sym, ft) if _SLUG_RE.match(sym) else None
    interpreted_as = None

    if html is None:
        best, alternatives = await resolve_symbol(requested)
        sym = best.symbol
        html = await _get_page(sym, ft)
        if html is None:
            raise ToolError(
                f"Resolved '{requested}' to {sym} but its Screener.in page is unavailable.",
                "symbol_not_found",
                requested_symbol=requested,
                candidates=[best.as_dict()] + [a.as_dict() for a in alternatives],
            )
        interpreted_as = sym
        note = f"'{requested}' is not a Screener.in symbol — interpreted as {sym} ({best.name})."
        if alternatives:
            note += " Other matches: " + ", ".join(f"{a.symbol} ({a.name})" for a in alternatives) + "."
        warnings.append(note)

    if ft == "consolidated" and not page_has_data(html):
        standalone = await _get_page(sym, "standalone")
        if standalone and page_has_data(standalone):
            html = standalone
            ft = "standalone"
            warnings.append(
                f"{sym} publishes no consolidated financials on Screener.in (typically a "
                "company without subsidiaries) — showing standalone figures instead."
            )

    m = _COMPANY_ID_RE.search(html)
    return CompanyPage(
        requested_symbol=requested,
        symbol=sym,
        financial_type=ft,
        html=html,
        company_id=m.group(1) if m else None,
        warnings=warnings,
        interpreted_as=interpreted_as,
    )


async def resolve_nse_symbol(symbol: str) -> tuple[str, list[str], dict]:
    """Canonical NSE trading symbol for free-form input → (nse_symbol, warnings, meta).

    NSE's APIs only accept exact trading symbols and answer anything else with
    an empty list — which used to read as "no announcements". Resolving through
    the Screener page gives fuzzy matching and the company's real NSE code.
    If Screener itself is unreachable, fall back to the input as given (with a
    warning) rather than failing an NSE lookup that might still work.
    """
    from ..parsers.company import parse_overview

    sym = normalize_symbol(symbol)
    if not sym:
        raise ToolError("Please provide an NSE symbol or company name.", "invalid_input")
    try:
        page = await fetch_company_page(symbol, "standalone")
    except ToolError:
        raise
    except Exception as e:
        return sym, [
            f"Couldn't verify '{symbol}' against Screener.in ({type(e).__name__}) — querying NSE with it as given."
        ], {"symbol": sym}

    nse_code = (parse_overview(page.html).get("nse_code") or "").upper()
    if not nse_code:
        raise ToolError(
            f"{page.symbol} has no NSE listing (BSE-only), so NSE announcements, bulk deals "
            "and insider disclosures aren't available for it.",
            "not_on_nse",
            symbol=page.symbol,
        )
    meta = {"symbol": nse_code}
    if page.interpreted_as or nse_code != sym:
        meta.update({"requested_symbol": symbol, "interpreted_as": nse_code})
    return nse_code, list(page.warnings), meta

"""
Research notebook — save, read, and summarize stock research notes locally.

Notes stored as JSON in ~/.screener-mcp/notebooks/{SYMBOL}/{note_id}.json
"""

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE_DIR = Path.home() / ".screener-mcp" / "notebooks"


def _note_dir(symbol: str) -> Path:
    return _BASE_DIR / symbol.upper()


def _find_note(note_id: str) -> Path | None:
    for p in _BASE_DIR.rglob(f"{note_id}.json"):
        return p
    return None


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def notebook_ai(
    action: str,
    symbol: str = None,
    content: str = None,
    note_id: str = None,
) -> str:
    """
    Manage research notes for stocks.

    action options:
      "create"    — start a new note (requires symbol + content); "save" is an alias
      "append"    — add to an existing note (requires note_id + content)
      "read"      — read all entries in a note (requires note_id)
      "list"      — list all notes, optionally filtered by symbol
      "summarize" — AI-ready summary of all notes for a symbol (requires symbol)
      "delete"    — delete a note (requires note_id)
    """
    action = action.lower().strip()
    if action == "save":
        action = "create"

    # ── create ────────────────────────────────────────────────────────────────
    if action == "create":
        if not symbol or not content:
            return "**Error:** `create` requires both `symbol` and `content`."
        nid = str(uuid.uuid4())[:8]
        note = {
            "note_id": nid,
            "symbol": symbol.upper(),
            "created_at": _now(),
            "updated_at": _now(),
            "entries": [{"timestamp": _now(), "text": content}],
        }
        _save(_note_dir(symbol) / f"{nid}.json", note)
        return (
            f"**Note created** — ID: `{nid}` | Symbol: {symbol.upper()}\n\n"
            f"Preview:\n{content[:400]}{'...' if len(content) > 400 else ''}\n\n"
            f"Tip: `notebook_ai('append', note_id='{nid}', content='...')` to add more entries."
        )

    # ── append ─────────────────────────────────────────────────────────────────
    if action == "append":
        if not note_id or not content:
            return "**Error:** `append` requires `note_id` and `content`."
        path = _find_note(note_id)
        if not path:
            return f"**Note `{note_id}` not found.** Use `notebook_ai('list')` to see all notes."
        note = _load(path)
        note.setdefault("entries", []).append({"timestamp": _now(), "text": content})
        note["updated_at"] = _now()
        _save(path, note)
        return (
            f"**Note `{note_id}` updated** — now {len(note['entries'])} entries.\n\n"
            f"Added:\n{content[:300]}{'...' if len(content) > 300 else ''}"
        )

    # ── read ───────────────────────────────────────────────────────────────────
    if action == "read":
        if not note_id:
            return "**Error:** `read` requires `note_id`."
        path = _find_note(note_id)
        if not path:
            return f"**Note `{note_id}` not found.**"
        note = _load(path)
        entries = note.get("entries", [])
        lines = [
            f"# Research Note — {note.get('symbol', '?')} | ID: {note_id}",
            f"Created: {note.get('created_at', '')[:10]} | Updated: {note.get('updated_at', '')[:10]}",
            f"Entries: {len(entries)}",
            "",
        ]
        for i, e in enumerate(entries, 1):
            ts = e.get("timestamp", "")[:16].replace("T", " ")
            lines.append(f"## Entry {i} — {ts}")
            lines.append(e.get("text", ""))
            lines.append("")
        return "\n".join(lines)

    # ── list ───────────────────────────────────────────────────────────────────
    if action == "list":
        search_root = _note_dir(symbol) if symbol else _BASE_DIR
        if not search_root.exists():
            label = f" for {symbol.upper()}" if symbol else ""
            return (
                f"**No notes found{label}.**\n\n"
                f"Start with: `notebook_ai('create', symbol='SYMBOL', content='Your research...')`"
            )

        notes = []
        for p in sorted(search_root.rglob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            n = _load(p)
            if n:
                notes.append(n)

        if not notes:
            return "**No notes found.**"

        lines = [f"# Research Notebook — {len(notes)} notes", ""]
        for n in notes[:60]:
            nid = n.get("note_id", "?")
            sym = n.get("symbol", "?")
            updated = n.get("updated_at", "")[:10]
            count = len(n.get("entries", []))
            preview = (n.get("entries") or [{}])[0].get("text", "")[:60].replace("\n", " ")
            lines.append(f"[{nid}] {sym:<8} | {updated} | {count} entries | {preview}...")

        return "\n".join(lines)

    # ── summarize ─────────────────────────────────────────────────────────────
    if action == "summarize":
        if not symbol:
            return "**Error:** `summarize` requires `symbol`."
        search_dir = _note_dir(symbol)
        if not search_dir.exists():
            return f"**No notes found for {symbol.upper()}.**"

        all_entries = []
        for p in sorted(search_dir.rglob("*.json"), key=lambda x: x.stat().st_mtime):
            n = _load(p)
            for e in n.get("entries", []):
                all_entries.append(f"[{e.get('timestamp', '')[:10]}] {e.get('text', '')}")

        if not all_entries:
            return f"**No note content found for {symbol.upper()}.**"

        combined = "\n\n---\n\n".join(all_entries)
        return f"""# Research Notes — {symbol.upper()} ({len(all_entries)} entries)

{combined}

---
**Analyst task:** Synthesize the research notes above for {symbol.upper()} into a structured report:

1. **Investment Thesis** — what is the core bull/bear case from these notes?
2. **Key Data Points** — important metrics, numbers, or facts captured
3. **Business Quality** — what do notes say about moat, management, competitive position?
4. **Risks & Red Flags** — concerns or warning signs noted
5. **Open Questions** — what still needs investigation?
6. **Current View** — based on all notes, what is the overall stance? (Bullish / Neutral / Bearish)
"""

    # ── delete ─────────────────────────────────────────────────────────────────
    if action == "delete":
        if not note_id:
            return "**Error:** `delete` requires `note_id`."
        path = _find_note(note_id)
        if not path:
            return f"**Note `{note_id}` not found.**"
        note = _load(path)
        path.unlink()
        return f"**Note `{note_id}` deleted.** (was: {note.get('symbol', '?')} — {note.get('updated_at', '')[:10]})"

    # ── unknown ────────────────────────────────────────────────────────────────
    return (
        f"**Unknown action: '{action}'**\n\n"
        f"Valid actions: create, append, read, list, summarize, delete\n\n"
        f"Example:\n"
        f"  notebook_ai('create', symbol='TCS', content='Strong Q3 — revenue beat by 3%...')\n"
        f"  notebook_ai('list')\n"
        f"  notebook_ai('summarize', symbol='TCS')"
    )

"""
Regression test for issue #18: on Windows the default text encoding is cp1252,
so any read_text()/write_text()/open() without an explicit encoding crashes on
the bundled UI JS (non-ASCII bytes) and mangles non-ASCII portfolio/notebook
data. Every text file access in the package must pass encoding="utf-8".
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "screener_mcp"


def _calls_missing_encoding(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("read_text", "write_text", "open"):
            continue
        kwargs = {kw.arg for kw in node.keywords}
        if "encoding" in kwargs:
            continue
        # open(..., "rb"/"wb") is binary and must not take an encoding.
        mode_args = [a for a in node.args[1:2]] + [kw.value for kw in node.keywords if kw.arg == "mode"]
        if any(isinstance(m, ast.Constant) and isinstance(m.value, str) and "b" in m.value for m in mode_args):
            continue
        # Skip non-file .open() calls (e.g. webbrowser.open(url)) — only flag
        # builtin open() and Path-style .open()/.read_text()/.write_text().
        if name == "open" and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id != "Path":
            continue
        yield node.lineno


@pytest.mark.parametrize("path", sorted(SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(SRC)))
def test_text_io_specifies_utf8(path):
    missing = list(_calls_missing_encoding(path))
    assert not missing, f"{path.name}: text I/O without encoding= at lines {missing}"


def test_ui_assets_decode_as_utf8():
    # The bundle that triggered #18 must decode cleanly as UTF-8.
    for asset in (SRC / "ui").glob("*"):
        if asset.suffix in (".js", ".html"):
            asset.read_text(encoding="utf-8")

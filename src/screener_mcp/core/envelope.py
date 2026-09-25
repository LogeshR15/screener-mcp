"""
Standard response envelope shared by every MCP tool.

Every tool returns the same top-level shape so a caller can detect degraded
results programmatically instead of inferring them from blank fields:

  {
    "status":   "ok" | "partial" | "error",
    "partial":  bool,             # True whenever status != "ok"
    "warnings": [str, ...],       # human-readable caveats, may be non-empty on "ok"
    "data":     {...} | None,     # the payload (None on error)
    # optional, only present when relevant:
    "missing_fields": [str, ...], # fields the source page had no value for
    "reason":   str,              # why the result is partial / failed
    "error":    {"type": str, "message": str, ...},
    "meta":     {...},            # e.g. resolved symbol, financial type used
  }

Tool implementations return either a plain string (legacy markdown reports,
wrapped as ``data.report``), a dict (used as ``data``), or a ``ToolResult`` when
they need to report warnings / missing fields / partial status.
"""

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolResult:
    data: Any
    warnings: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    reason: Optional[str] = None
    # None = derive from missing_fields / explicit reason
    partial: Optional[bool] = None
    meta: dict = field(default_factory=dict)


class ToolError(Exception):
    """An expected failure that should reach the caller as a structured error.

    ``details`` is merged into the envelope's ``error`` object — e.g. the
    candidate symbols for an unresolvable ticker, so the caller can retry in
    one step.
    """

    def __init__(self, message: str, error_type: str = "error", **details):
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.details = details


def ok(data: Any, **kwargs) -> ToolResult:
    return ToolResult(data=data, **kwargs)


def to_envelope(result: Any) -> dict:
    """Normalize whatever a tool implementation returned into the envelope."""
    if isinstance(result, ToolResult):
        partial = result.partial
        if partial is None:
            partial = bool(result.missing_fields) or result.reason is not None
        env: dict[str, Any] = {
            "status": "partial" if partial else "ok",
            "partial": partial,
            "warnings": list(result.warnings),
            "data": result.data,
        }
        if result.missing_fields:
            env["missing_fields"] = list(result.missing_fields)
        if result.reason:
            env["reason"] = result.reason
        if result.meta:
            env["meta"] = result.meta
        return env

    if isinstance(result, str):
        data: Any = {"report": result}
    else:
        data = result
    return {"status": "ok", "partial": False, "warnings": [], "data": data}


def error_envelope(message: str, error_type: str = "error", **details) -> dict:
    return {
        "status": "error",
        "partial": True,
        "warnings": [],
        "data": None,
        "reason": message,
        "error": {"type": error_type, "message": message, **details},
    }

"""Redaction for regulated data.

Everything that leaves the process — structured logs, evidence files, the
artifact itself, prompts sent to the model — passes through `Redactor.scrub`.
Redaction is applied at the *boundary*, not at each call site, so a new log line
cannot accidentally bypass it.

Two layers:

1. Pattern layer: shapes that are sensitive regardless of context (SSN, PAN,
   email, bearer tokens, long digit runs that look like account numbers).
2. Value layer: exact values we were handed this run (passwords, params marked
   `sensitive`). These are registered at runtime and scrubbed verbatim.

Limits are documented in REPORT.md section 6: this is defence in depth, not a
DLP product, and a screenshot is not text — see `Redactor.allow_screenshot`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Pattern, Tuple

_PATTERNS: List[Tuple[str, Pattern[str]]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("EMAIL", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("TOKEN", re.compile(r"\b(?:Bearer|sk-ant-|sk-)[A-Za-z0-9._\-]{8,}")),
    ("ACCOUNT", re.compile(r"\b\d{9,17}\b")),
]

_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|ssn|pin)", re.I
)

MASK = "[REDACTED:{}]"


class Redactor:
    def __init__(self, extra_values: Iterable[str] = ()) -> None:
        self._values: List[str] = []
        for value in extra_values:
            self.register(value)

    def register(self, value: Any) -> None:
        """Register an exact value (a password, a sensitive param) to scrub."""
        text = str(value or "").strip()
        if len(text) >= 3:
            self._values.append(text)

    # -- text -------------------------------------------------------------
    def scrub(self, text: Any) -> Any:
        if not isinstance(text, str):
            return self.scrub_obj(text)
        out = text
        for value in self._values:
            if value in out:
                out = out.replace(value, MASK.format("VALUE"))
        for label, pattern in _PATTERNS:
            out = pattern.sub(MASK.format(label), out)
        return out

    # -- structures -------------------------------------------------------
    def scrub_obj(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            result: Dict[str, Any] = {}
            for key, value in obj.items():
                if _SECRET_KEY.search(str(key)):
                    result[key] = MASK.format("KEY")
                else:
                    result[key] = self.scrub_obj(value)
            return result
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        if isinstance(obj, str):
            return self.scrub(obj)
        return obj

    # -- policy hook ------------------------------------------------------
    @staticmethod
    def allow_screenshot(kind: str, policy_allows: bool) -> bool:
        """Screenshots can contain PII we cannot regex away.

        We only keep them for failure/escalation evidence, and only when the
        policy explicitly permits it for this environment.
        """
        return policy_allows and kind in {"failure", "escalation", "discovery"}


def redact_text(text: str) -> str:
    """Convenience for call sites with no session-specific secrets."""
    return Redactor().scrub(text)

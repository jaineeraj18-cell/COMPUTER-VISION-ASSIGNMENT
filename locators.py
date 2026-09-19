"""Resolving a `ControlRef` against an `Observation`.

This is where "deterministic replay" is actually won or lost. The rules:

1. Try strategies in the order the artifact declares, most meaningful first.
   The strategy that succeeded is recorded in the step log, so a reviewer can
   see when a capability has started limping along on its CSS fallback.
2. A strategy that matches more than one control is *ambiguous*, not a match:
   we disambiguate with the recorded ordinal, and if that does not resolve it
   uniquely we refuse rather than click something plausible. Clicking the wrong
   row in a bank's servicing tool is worse than stopping.
3. Disabled / invisible controls never match for click and type.

`Resolution.confidence` is what the engine uses to decide between "proceed" and
"escalate to a human".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

from ..models import ControlRef
from ..surface.base import Observation, UIElement


@dataclass
class Resolution:
    element: Optional[UIElement]
    strategy: str
    confidence: float
    candidates: int = 0
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.element is not None


_ACTIONABLE = {"click", "type", "select", "press_key"}


def resolve(
    ref: ControlRef, observation: Observation, action: str = "click"
) -> Resolution:
    pool = [e for e in observation.elements if _eligible(e, action)]
    if ref.frame_hint:
        framed = [e for e in pool if ref.frame_hint in (e.frame or "")]
        pool = framed or pool

    for strategy in ref.strategies:
        matches = _by_strategy(strategy, ref, pool)
        if not matches:
            continue
        if len(matches) == 1:
            return Resolution(matches[0], strategy, 1.0, 1)
        exact = [e for e in matches if e.ordinal == ref.ordinal]
        if len(exact) == 1:
            return Resolution(
                exact[0], strategy, 0.8, len(matches), "disambiguated by ordinal"
            )
        return Resolution(
            None,
            strategy,
            0.0,
            len(matches),
            f"ambiguous: {len(matches)} controls match {ref.describe()}",
        )

    return Resolution(None, "none", 0.0, 0, f"no control matches {ref.describe()}")


def _eligible(element: UIElement, action: str) -> bool:
    if action in _ACTIONABLE:
        return element.visible and element.enabled
    return True


def _by_strategy(strategy: str, ref: ControlRef, pool: List[UIElement]) -> List[UIElement]:
    if strategy == "role_name":
        return [
            e for e in pool if e.role == ref.role and _name_matches(ref, e.name)
        ]
    if strategy == "anchor":
        if not ref.anchor_text:
            return []
        needle = _norm(ref.anchor_text)
        return [
            e
            for e in pool
            if e.role == ref.role and needle and needle in _norm(e.anchor_text or "")
        ]
    if strategy == "css":
        if not ref.css_hint:
            return []
        return [e for e in pool if e.css_hint == ref.css_hint]
    if strategy == "text":
        needle = _norm(ref.name)
        return [e for e in pool if needle and needle in _norm(e.name)]
    if strategy == "ordinal":
        same_role = [e for e in pool if e.role == ref.role]
        return [e for e in same_role if e.ordinal == ref.ordinal]
    return []


def _name_matches(ref: ControlRef, candidate: str) -> bool:
    if ref.name_match == "regex":
        try:
            return bool(re.search(ref.name, candidate or "", re.I))
        except re.error:
            return False
    if ref.name_match == "contains":
        return _norm(ref.name) in _norm(candidate)
    return _norm(ref.name) == _norm(candidate)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def describe_near_misses(ref: ControlRef, observation: Observation, limit: int = 5) -> str:
    """Debug aid: what *was* on screen when we failed to find the control."""
    same_role = [e for e in observation.elements if e.role == ref.role]
    names = [e.name for e in same_role[:limit] if e.name]
    if not names:
        return f"no {ref.role} controls visible"
    return f"{ref.role} controls present: " + ", ".join(repr(n) for n in names)

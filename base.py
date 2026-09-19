"""The surface seam.

A *surface* is anything an operator can look at and act on: a modern web app, a
1998 frameset, a Win32 back-office client. The recorded flow must not know which
one it is talking to, so the seam is deliberately narrow:

    observe() -> Observation      # a flat list of controls, role + name + hints
    act(SurfaceAction) -> None    # click / type / select / navigate / key

Everything above this line (artifact schema, locators, replay engine, policy,
escalation) is written against `Observation` and `UIElement` only. Adding a
desktop surface means implementing two methods, not touching replay — see
`desktop_stub.py` and REPORT.md section 4.

The control vocabulary is accessibility-shaped (role, name, value, enabled)
because that is the one vocabulary that exists on *all three* surfaces: ARIA in
browsers, UIA/AX on the desktop. It is also what survives a legacy app with no
test IDs, because it is derived from what the human sees.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class UIElement:
    ref: str  # handle, valid only for the observation it came from
    role: str
    name: str = ""
    value: Optional[str] = None
    enabled: bool = True
    visible: bool = True
    frame: str = "main"
    ordinal: int = 0  # index among elements sharing role+name
    anchor_text: Optional[str] = None  # nearest label / row / cell text
    css_hint: Optional[str] = None
    bbox: Optional[List[float]] = None

    def describe(self) -> str:
        bits = [f"{self.role}"]
        if self.name:
            bits.append(f"'{self.name}'")
        if self.ordinal:
            bits.append(f"#{self.ordinal}")
        if self.value:
            bits.append(f"value={self.value!r}")
        if not self.enabled:
            bits.append("(disabled)")
        if self.anchor_text and self.anchor_text != self.name:
            bits.append(f"near={self.anchor_text!r}")
        return " ".join(bits)


@dataclass
class Observation:
    url: str
    title: str = ""
    elements: List[UIElement] = field(default_factory=list)
    text: str = ""  # visible text digest
    dialogs: List[str] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    captured_at: float = field(default_factory=time.time)

    def find_ref(self, ref: str) -> Optional[UIElement]:
        return next((e for e in self.elements if e.ref == ref), None)

    def render(self, limit: int = 60) -> str:
        """Compact text rendering handed to the model (cheap, no screenshots)."""
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.dialogs:
            lines.append("DIALOGS: " + " | ".join(self.dialogs))
        lines.append("VISIBLE TEXT:")
        lines.append(self.text[:1500])
        lines.append("CONTROLS:")
        for element in self.elements[:limit]:
            lines.append(f"  [{element.ref}] {element.describe()}")
        if len(self.elements) > limit:
            lines.append(f"  ... {len(self.elements) - limit} more controls")
        return "\n".join(lines)


@dataclass
class SurfaceAction:
    kind: str  # navigate | click | type | select | press_key | wait_for | read
    ref: Optional[str] = None
    text: Optional[str] = None
    url: Optional[str] = None
    key: Optional[str] = None
    timeout_ms: int = 10_000
    meta: Dict[str, Any] = field(default_factory=dict)


class Surface:
    """Abstract surface. Implementations must be safe to pause and resume."""

    name = "abstract"

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def observe(self, screenshot_path: Optional[str] = None) -> Observation:
        raise NotImplementedError

    def act(self, action: SurfaceAction, observation: Observation) -> None:
        raise NotImplementedError

    # -- handoff ----------------------------------------------------------
    def cede_control(self) -> None:
        """Make the live session usable by a human (see escalation/broker.py)."""
        raise NotImplementedError

    def resume_control(self) -> List[Dict[str, Any]]:
        """Take the session back; return what the human did, for the record."""
        raise NotImplementedError


class SurfaceError(RuntimeError):
    """Raised when the surface itself failed (crash, disconnect, timeout)."""

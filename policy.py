"""Policy: allowlist enforcement and risk classification.

There is exactly one enforcement point (`PolicyEngine.check`). Both the LLM
discovery loop and the deterministic replay engine call it before every action,
so a capability cannot be recorded doing something replay would refuse, and
replay cannot drift outside the allowlist because "the artifact said so".
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

DENY = "deny"
ALLOW = "allow"
REQUIRE_APPROVAL = "require_approval"


@dataclass
class Decision:
    verdict: str
    reason: str = ""
    rule: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.verdict == ALLOW


@dataclass
class ProposedAction:
    """Surface-independent description of what is about to happen."""

    kind: str
    url: str  # url we are on (or navigating to)
    control_name: str = ""
    control_role: str = ""
    value: Optional[str] = None


@dataclass
class PolicyEngine:
    allowed_domains: List[str] = field(default_factory=list)
    allowed_path_globs: List[str] = field(default_factory=lambda: ["*"])
    denied_path_globs: List[str] = field(default_factory=list)
    allowed_actions: List[str] = field(default_factory=list)
    risky_control_patterns: List[str] = field(default_factory=list)
    risky_mode: str = "require_approval"  # require_approval | block | flag
    allow_screenshots: bool = True
    max_steps: int = 25

    @classmethod
    def load(cls, path: str) -> "PolicyEngine":
        with open(path, "r", encoding="utf-8") as fh:
            raw: Dict[str, Any] = yaml.safe_load(fh) or {}
        return cls(
            allowed_domains=raw.get("allowed_domains") or [],
            allowed_path_globs=raw.get("allowed_path_globs") or ["*"],
            denied_path_globs=raw.get("denied_path_globs") or [],
            allowed_actions=raw.get("allowed_actions") or [],
            risky_control_patterns=raw.get("risky_control_patterns") or [],
            risky_mode=raw.get("risky_mode", "require_approval"),
            allow_screenshots=bool(raw.get("allow_screenshots", True)),
            max_steps=int(raw.get("max_steps", 25)),
        )

    # -- enforcement ------------------------------------------------------
    def check(self, action: ProposedAction) -> Decision:
        if action.kind not in self.allowed_actions:
            return Decision(DENY, f"action type {action.kind!r} not permitted", "allowed_actions")

        url_decision = self.check_url(action.url)
        if not url_decision.allowed:
            return url_decision

        if self.is_risky(action):
            if self.risky_mode == "block":
                return Decision(DENY, "risky action blocked by policy", "risky_mode")
            if self.risky_mode == "require_approval":
                return Decision(
                    REQUIRE_APPROVAL,
                    f"'{action.control_name}' looks irreversible; needs human approval",
                    "risky_control_patterns",
                )
            return Decision(ALLOW, "risky action flagged", "risky_control_patterns")
        return Decision(ALLOW)

    def check_url(self, url: str) -> Decision:
        if not url:
            return Decision(DENY, "empty url", "allowed_domains")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return Decision(DENY, f"scheme {parsed.scheme!r} not permitted", "allowed_domains")
        host = parsed.hostname or ""
        if not any(fnmatch.fnmatch(host, pattern) for pattern in self.allowed_domains):
            return Decision(DENY, f"host {host!r} is not on the allowlist", "allowed_domains")
        path = parsed.path or "/"
        if any(fnmatch.fnmatch(path, pattern) for pattern in self.denied_path_globs):
            return Decision(DENY, f"path {path!r} is explicitly denied", "denied_path_globs")
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed_path_globs):
            return Decision(DENY, f"path {path!r} is not on the allowlist", "allowed_path_globs")
        return Decision(ALLOW)

    def is_risky(self, action: ProposedAction) -> bool:
        if action.kind not in {"click", "press_key"}:
            return False
        haystack = f"{action.control_name} {action.value or ''}".strip()
        return any(
            re.search(pattern, haystack, re.I)
            for pattern in self.risky_control_patterns
        )

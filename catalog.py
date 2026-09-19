"""The capability catalog — how an AI agent discovers and invokes these flows.

A saved artifact is only useful if the agent-facing product can find it and call
it like a function. `Catalog.tool_definitions()` emits Anthropic/OpenAI-shaped
tool schemas straight from the artifacts, so the calling agent sees:

    saucedemo.checkout_review(item_name, first_name, last_name, postal_code)
      -> {item_price, subtotal, tax, total}

and never sees a selector, a step list, or a browser.

Artifacts are stored as one JSON file per (capability, version) so that a
version is immutable once written: a capability an agent invoked yesterday
cannot silently become something else today.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .models import Capability


class Catalog:
    def __init__(self, root: str = "artifacts") -> None:
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    # -- storage ----------------------------------------------------------
    def path_for(self, capability_id: str, version: int) -> str:
        return os.path.join(self.root, f"{capability_id}.v{version}.json")

    def save(self, capability: Capability, *, bump_if_exists: bool = True) -> str:
        path = self.path_for(capability.id, capability.version)
        while bump_if_exists and os.path.exists(path):
            capability.version += 1
            path = self.path_for(capability.id, capability.version)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(capability.to_dict(), fh, indent=2, ensure_ascii=False)
        return path

    def load(self, capability_id: str, version: Optional[int] = None) -> Capability:
        if version is not None:
            return self.load_path(self.path_for(capability_id, version))
        versions = self.versions(capability_id)
        if not versions:
            raise FileNotFoundError(f"no capability {capability_id!r} in {self.root}")
        return self.load_path(self.path_for(capability_id, max(versions)))

    def load_path(self, path: str) -> Capability:
        with open(path, "r", encoding="utf-8") as fh:
            return Capability.from_dict(json.load(fh))

    def versions(self, capability_id: str) -> List[int]:
        out = []
        prefix = f"{capability_id}.v"
        for name in os.listdir(self.root):
            if name.startswith(prefix) and name.endswith(".json"):
                try:
                    out.append(int(name[len(prefix) : -len(".json")]))
                except ValueError:
                    continue
        return sorted(out)

    def list(self) -> List[Capability]:
        seen: Dict[str, Capability] = {}
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".json"):
                continue
            try:
                capability = self.load_path(os.path.join(self.root, name))
            except Exception:
                continue
            current = seen.get(capability.id)
            if current is None or capability.version > current.version:
                seen[capability.id] = capability
        return list(seen.values())

    # -- agent-facing -----------------------------------------------------
    def tool_definitions(self, approved_only: bool = False) -> List[Dict[str, Any]]:
        return [
            capability.signature()
            for capability in self.list()
            if not approved_only or capability.approval == "approved"
        ]

    def record_replay(self, capability: Capability, success: bool) -> None:
        """Stability feedback — cheap input to the draft -> approved gate."""
        capability.stability.replays += 1
        if success:
            capability.stability.successes += 1
        path = self.path_for(capability.id, capability.version)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            raw["stability"] = {
                "replays": capability.stability.replays,
                "successes": capability.stability.successes,
                "score": capability.stability.score,
            }
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2, ensure_ascii=False)

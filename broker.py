"""Human-in-the-loop: raising an intervention and transferring control.

Control-transfer model
----------------------
Exactly one party holds the token for a session at a time:

    AUTOMATION -> (pause + cede) -> HUMAN -> (resolve) -> AUTOMATION | ABORTED

The token lives in a file next to the request, so "who is in control" is
observable from outside the process (and, in production, from another host).
The automation blocks on that file rather than polling the browser, which is
what keeps the session *the same session*: the browser context, its cookies and
its half-filled form are never torn down or recreated.

Transport is deliberately boring: a directory of JSON files. It is a queue with
one consumer, it survives the operator console restarting, and swapping it for
Redis/SQS later means reimplementing two methods. What matters for the
evaluation is the state machine and the fact that the human drives *the live
session*, not the transport.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

OPEN = "open"
TAKEN = "taken"
RESOLVED = "resolved"

HOLDER_AUTOMATION = "automation"
HOLDER_HUMAN = "human"


@dataclass
class InterventionRequest:
    id: str
    run_id: str
    capability_id: str
    goal: str
    step_id: str
    reason: str
    url: str
    state_summary: str
    screenshot: Optional[str] = None
    session_hint: Optional[str] = None  # how to attach to the live session
    created_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    status: str = OPEN
    holder: str = HOLDER_AUTOMATION
    operator: Optional[str] = None


@dataclass
class Resolution:
    action: str  # resume | abort | skip_step
    operator: str = "unknown"
    notes: str = ""
    human_actions: List[Dict[str, Any]] = field(default_factory=list)
    resolved_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )


class FileBroker:
    """Filesystem-backed intervention queue shared with the operator console."""

    def __init__(self, root: str = "interventions") -> None:
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    # -- paths ------------------------------------------------------------
    def _req_path(self, request_id: str) -> str:
        return os.path.join(self.root, f"{request_id}.request.json")

    def _res_path(self, request_id: str) -> str:
        return os.path.join(self.root, f"{request_id}.resolution.json")

    # -- automation side --------------------------------------------------
    def raise_request(self, request: InterventionRequest) -> str:
        request.status = OPEN
        request.holder = HOLDER_HUMAN  # control is ceded the moment we ask
        path = self._req_path(request.id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(request), fh, indent=2)
        return path

    def wait_for_resolution(
        self, request_id: str, timeout_s: int = 600, poll_s: float = 1.5
    ) -> Optional[Resolution]:
        deadline = time.time() + timeout_s
        path = self._res_path(request_id)
        while time.time() < deadline:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                self._mark(request_id, RESOLVED, raw.get("operator"))
                return Resolution(
                    action=raw.get("action", "abort"),
                    operator=raw.get("operator", "unknown"),
                    notes=raw.get("notes", ""),
                )
            time.sleep(poll_s)
        return None  # timed out: control stays with the human, run is escalated

    # -- operator side ----------------------------------------------------
    def list_open(self) -> List[Dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(self.root)):
            if not name.endswith(".request.json"):
                continue
            with open(os.path.join(self.root, name), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("status") in {OPEN, TAKEN}:
                out.append(raw)
        return out

    def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        path = self._req_path(request_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def take(self, request_id: str, operator: str) -> None:
        self._mark(request_id, TAKEN, operator)

    def resolve(
        self, request_id: str, action: str, operator: str = "operator", notes: str = ""
    ) -> None:
        if action not in {"resume", "abort", "skip_step"}:
            raise ValueError(f"unknown resolution action {action!r}")
        with open(self._res_path(request_id), "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "action": action,
                    "operator": operator,
                    "notes": notes,
                    "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
                fh,
                indent=2,
            )

    def _mark(self, request_id: str, status: str, operator: Optional[str]) -> None:
        path = self._req_path(request_id)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["status"] = status
        raw["holder"] = HOLDER_AUTOMATION if status == RESOLVED else HOLDER_HUMAN
        if operator:
            raw["operator"] = operator
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)


def new_request_id() -> str:
    return f"iv-{uuid.uuid4().hex[:8]}"

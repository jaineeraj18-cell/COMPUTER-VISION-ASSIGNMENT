"""Turning "stuck" into a human taking over the live session, and back.

Used identically by the discovery loop and the replay engine, so the
control-transfer semantics cannot drift between the two paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..evidence import RunEvidence
from ..surface.base import Observation, Surface, SurfaceError
from .broker import (
    FileBroker,
    InterventionRequest,
    Resolution,
    new_request_id,
)


@dataclass
class EscalationOutcome:
    action: str  # resume | abort | skip_step | unattended
    request_id: str
    operator: str = ""
    notes: str = ""
    human_actions: List[Dict[str, Any]] = field(default_factory=list)


class EscalationCoordinator:
    def __init__(
        self,
        broker: Optional[FileBroker],
        evidence: RunEvidence,
        *,
        attended: bool = True,
        timeout_s: int = 600,
    ) -> None:
        self.broker = broker
        self.evidence = evidence
        self.attended = attended
        self.timeout_s = timeout_s
        self.history: List[Dict[str, Any]] = []

    def escalate(
        self,
        *,
        surface: Surface,
        observation: Observation,
        capability_id: str,
        goal: str,
        step_id: str,
        reason: str,
        run_id: str,
    ) -> EscalationOutcome:
        request = InterventionRequest(
            id=new_request_id(),
            run_id=run_id,
            capability_id=capability_id,
            goal=goal,
            step_id=step_id,
            reason=reason,
            url=observation.url,
            state_summary=self.evidence.redactor.scrub(observation.render(limit=25)),
            screenshot=observation.screenshot_path,
            session_hint=getattr(surface, "cdp_endpoint", lambda: None)(),
        )
        self.evidence.write_json("intervention.json", request.__dict__)
        self.evidence.log(
            "escalation.raised",
            request_id=request.id,
            step_id=step_id,
            reason=reason,
            url=observation.url,
        )

        # Unattended mode (CI, nightly batch): we still *produce* the request so
        # an operator can pick it up later, but we do not block a robot on a
        # human who is not there.
        if not self.attended or self.broker is None:
            self.evidence.log("escalation.unattended", request_id=request.id)
            return EscalationOutcome(action="unattended", request_id=request.id)

        self.broker.raise_request(request)

        try:
            surface.cede_control()
            self.evidence.log("control.transferred", holder="human", request_id=request.id)
        except SurfaceError as exc:
            self.evidence.log("control.transfer_failed", error=str(exc))
            return EscalationOutcome(action="abort", request_id=request.id, notes=str(exc))

        resolution: Optional[Resolution] = self.broker.wait_for_resolution(
            request.id, timeout_s=self.timeout_s
        )
        human_actions = surface.resume_control()

        if resolution is None:
            self.evidence.log("escalation.timeout", request_id=request.id)
            return EscalationOutcome(
                action="abort",
                request_id=request.id,
                notes="no operator response before timeout",
                human_actions=human_actions,
            )

        record = {
            "request_id": request.id,
            "action": resolution.action,
            "operator": resolution.operator,
            "notes": resolution.notes,
            "human_actions": human_actions,
        }
        self.history.append(record)
        self.evidence.write_json("human_actions.json", self.history)
        self.evidence.log(
            "control.returned",
            holder="automation",
            request_id=request.id,
            action=resolution.action,
            operator=resolution.operator,
            human_action_count=len(human_actions),
        )
        return EscalationOutcome(
            action=resolution.action,
            request_id=request.id,
            operator=resolution.operator,
            notes=resolution.notes,
            human_actions=human_actions,
        )

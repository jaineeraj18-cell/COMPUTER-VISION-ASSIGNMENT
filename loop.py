"""The discovery loop: observe -> decide (LLM) -> act, until the goal is met.

This is the only place in the system that talks to a model. It produces a
`DiscoveryRun`, which `compiler.py` turns into a capability artifact. Once that
artifact exists the model is never needed for this flow again — which is the
entire economic argument for the system.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..escalation.coordinator import EscalationCoordinator
from ..evidence import RunEvidence
from ..policy import ProposedAction, PolicyEngine
from ..surface.base import Observation, Surface, SurfaceAction, SurfaceError, UIElement
from . import prompts
from .tools import TOOLS

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-5")


@dataclass
class ExecutedAction:
    """One successful model-driven action, with everything replay will need."""

    action: str
    why: str
    url_before: str
    url_after: str
    element: Optional[UIElement] = None
    value: Optional[str] = None
    url: Optional[str] = None
    key: Optional[str] = None
    extractions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class DiscoveryRun:
    goal: str
    target_url: str
    params: Dict[str, Any]
    model: str
    run_id: str
    actions: List[ExecutedAction] = field(default_factory=list)
    finish: Dict[str, Any] = field(default_factory=dict)
    status: str = "incomplete"  # complete | incomplete | escalated | denied
    llm_steps: int = 0
    human_interventions: int = 0
    stop_reason: str = ""


class DiscoveryAgent:
    def __init__(
        self,
        surface: Surface,
        policy: PolicyEngine,
        evidence: RunEvidence,
        escalation: Optional[EscalationCoordinator] = None,
        model: str = DEFAULT_MODEL,
        max_steps: int = 20,
        timeout_s: int = 300,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.escalation = escalation
        self.model = model
        self.max_steps = min(max_steps, policy.max_steps)
        self.timeout_s = timeout_s

    # -- main loop --------------------------------------------------------
    def run(self, goal: str, target_url: str, params: Dict[str, Any]) -> DiscoveryRun:
        from anthropic import Anthropic

        client = Anthropic()
        run = DiscoveryRun(
            goal=goal,
            target_url=target_url,
            params=params,
            model=self.model,
            run_id=self.evidence.run_id,
        )

        gate = self.policy.check_url(target_url)
        if not gate.allowed:
            run.status = "denied"
            run.stop_reason = gate.reason
            self.evidence.log("policy.denied", url=target_url, reason=gate.reason)
            return run

        self.evidence.log("discovery.start", goal=goal, target=target_url, model=self.model)
        self.surface.act(SurfaceAction(kind="navigate", url=target_url), Observation(url=""))

        observation = self._observe("start")
        messages: List[Dict[str, Any]] = [
            {
                "role": "user",
                "content": prompts.build_goal_message(goal, params, target_url)
                + "\n\n"
                + observation.render(),
            }
        ]

        deadline = time.time() + self.timeout_s
        while run.llm_steps < self.max_steps:
            if time.time() > deadline:
                run.status = "incomplete"
                run.stop_reason = "timeout"
                self.evidence.log("discovery.stop", reason="timeout")
                break

            response = client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=prompts.SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
            run.llm_steps += 1

            blocks, tool_call = _parse(response)
            messages.append({"role": "assistant", "content": blocks})
            if tool_call is None:
                run.status = "incomplete"
                run.stop_reason = "model stopped without calling a tool"
                self.evidence.log("discovery.stop", reason=run.stop_reason)
                break

            name, tool_input, tool_id = tool_call
            self.evidence.log(
                "model.tool_call",
                step=run.llm_steps,
                tool=name,
                why=tool_input.get("why", ""),
                input={k: v for k, v in tool_input.items() if k != "why"},
            )

            if name == "finish":
                run.finish = tool_input
                run.status = "complete"
                run.stop_reason = "goal reached"
                self.evidence.log("discovery.finish", summary=tool_input.get("summary", ""))
                break

            note, observation, stop = self._execute(
                name, tool_input, observation, run, goal
            )
            _collapse_history(messages)
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": prompts.format_tool_result(
                                observation.render(), note
                            ),
                        }
                    ],
                }
            )
            if stop:
                break
        else:
            run.status = "incomplete"
            run.stop_reason = f"hit max_steps ({self.max_steps})"
            self.evidence.log("discovery.stop", reason=run.stop_reason)

        self.evidence.write_json(
            "discovery.json",
            {
                "goal": goal,
                "status": run.status,
                "stop_reason": run.stop_reason,
                "llm_steps": run.llm_steps,
                "actions": [
                    {
                        "action": a.action,
                        "why": a.why,
                        "control": a.element.describe() if a.element else None,
                        "value": a.value,
                        "url_after": a.url_after,
                    }
                    for a in run.actions
                ],
                "finish": run.finish,
            },
        )
        return run

    # -- one tool call ----------------------------------------------------
    def _execute(self, name, tool_input, observation, run, goal):
        """Returns (note_for_model, new_observation, stop_run)."""
        if name == "escalate":
            reason = tool_input.get("reason", "agent requested help")
            return self._escalate(observation, run, goal, reason)

        if name == "extract":
            return self._extract(tool_input, observation, run)

        element: Optional[UIElement] = None
        action_kind = {
            "click": "click",
            "type_text": "type",
            "select_option": "select",
            "navigate": "navigate",
            "press_key": "press_key",
        }.get(name)
        if action_kind is None:
            return (f"ERROR: unknown tool {name!r}", observation, False)

        if action_kind != "navigate":
            element = observation.find_ref(tool_input.get("ref", ""))
            if element is None:
                return (
                    f"ERROR: no control with ref {tool_input.get('ref')!r} on this "
                    "screen. Re-read CONTROLS below.",
                    observation,
                    False,
                )

        value = tool_input.get("text") or tool_input.get("label")
        url = tool_input.get("url")

        decision = self.policy.check(
            ProposedAction(
                kind=action_kind,
                url=url or observation.url,
                control_name=element.name if element else "",
                control_role=element.role if element else "",
                value=value,
            )
        )
        if decision.verdict == "deny":
            self.evidence.log("policy.denied", tool=name, reason=decision.reason)
            return (f"REFUSED BY POLICY: {decision.reason}", observation, False)
        if decision.verdict == "require_approval":
            note, new_observation, stop = self._escalate(
                observation, run, goal, f"risky action needs approval: {decision.reason}"
            )
            if stop:
                return note, new_observation, stop
            observation = new_observation

        try:
            self.surface.act(
                SurfaceAction(
                    kind=action_kind,
                    ref=element.ref if element else None,
                    text=value,
                    url=url,
                    key=tool_input.get("key"),
                ),
                observation,
            )
        except SurfaceError as exc:
            self.evidence.log("action.error", tool=name, error=str(exc))
            return (f"ACTION FAILED: {exc}", self._observe("after-error"), False)

        url_before = observation.url
        new_observation = self._observe(f"step-{run.llm_steps}")
        run.actions.append(
            ExecutedAction(
                action=action_kind,
                why=tool_input.get("why", ""),
                url_before=url_before,
                url_after=new_observation.url,
                element=element,
                value=value,
                url=url,
                key=tool_input.get("key"),
            )
        )
        return ("", new_observation, False)

    def _extract(self, tool_input, observation, run):
        name = tool_input.get("name")
        spec = {
            "name": name,
            "description": tool_input.get("description", ""),
            "pattern": tool_input.get("pattern"),
        }
        element = observation.find_ref(tool_input.get("ref", "")) if tool_input.get("ref") else None
        if element is not None:
            spec["element"] = element
        if not run.actions:
            # Extraction before any action: attach to a synthetic read step.
            run.actions.append(
                ExecutedAction(
                    action="read",
                    why="declare output",
                    url_before=observation.url,
                    url_after=observation.url,
                )
            )
        run.actions[-1].extractions.append(spec)
        self.evidence.log("model.extract", name=name, has_element=element is not None)
        return (f"Recorded output {name!r}.", observation, False)

    def _escalate(self, observation, run, goal, reason):
        if self.escalation is None:
            run.status = "escalated"
            run.stop_reason = reason
            return (f"ESCALATION UNAVAILABLE: {reason}", observation, True)

        outcome = self.escalation.escalate(
            surface=self.surface,
            observation=observation,
            capability_id="<discovery>",
            goal=goal,
            step_id=f"llm-step-{run.llm_steps}",
            reason=reason,
            run_id=run.run_id,
        )
        run.human_interventions += 1
        if outcome.action in {"resume", "skip_step"}:
            new_observation = self._observe("after-handoff")
            note = (
                "A human operator took control of this session and has handed it "
                f"back. Notes: {outcome.notes or 'none'}. The screen below is the "
                "current state — continue from here."
            )
            return (note, new_observation, False)

        run.status = "escalated"
        run.stop_reason = reason
        return ("Run handed to a human operator and not resumed.", observation, True)

    # -- helpers ----------------------------------------------------------
    def _observe(self, label: str) -> Observation:
        shot = None
        if self.policy.allow_screenshots:
            shot = self.evidence.screenshot_path(label)
        observation = self.surface.observe(screenshot_path=shot)
        self.evidence.write_text(
            os.path.join("observations", f"{label}.txt"), observation.render()
        )
        return observation


def _parse(response):
    """Convert an SDK response into plain blocks + the single tool call."""
    blocks: List[Dict[str, Any]] = []
    tool_call = None
    for block in response.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
            if tool_call is None:
                tool_call = (block.name, dict(block.input), block.id)
    return blocks, tool_call


def _collapse_history(messages: List[Dict[str, Any]], keep: int = 2) -> None:
    """Elide old screens: context cost is linear in steps otherwise."""
    tool_results = [
        message
        for message in messages
        if message["role"] == "user"
        and isinstance(message["content"], list)
        and message["content"]
        and message["content"][0].get("type") == "tool_result"
    ]
    for message in tool_results[:-keep] if len(tool_results) > keep else []:
        block = message["content"][0]
        body = block.get("content")
        if isinstance(body, list) and body and body[0].get("type") == "text":
            body[0]["text"] = prompts.summarise_old_observation(body[0]["text"])

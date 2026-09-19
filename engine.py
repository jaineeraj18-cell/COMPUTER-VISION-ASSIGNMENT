"""Deterministic replay — the path a production AI agent triggers.

No LLM is imported in this module, and that is enforced by a test. Given an
artifact and typed arguments, the engine:

    bind params -> for each step: policy check -> resolve control -> act
                -> evaluate outcome rules -> verify checkpoint -> extract
    -> verify the success checkpoint -> return a ReplayResult

Determinism comes from four things, in decreasing order of importance:

1. No model in the decision loop: the step list, the control refs and the
   checkpoints are fixed at record time.
2. Intent-level control resolution with a declared strategy order and a refusal
   to guess when a match is ambiguous (`replay/locators.py`).
3. Explicit waits tied to *state* (text/control/url present) rather than sleeps,
   with per-step timeouts.
4. Checkpoints after state-changing steps, so we never proceed on the
   assumption that a click worked.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from ..escalation.coordinator import EscalationCoordinator
from ..evidence import RunEvidence
from ..models import (
    Capability,
    Checkpoint,
    Extraction,
    Failure,
    Outcome,
    ReplayResult,
    Step,
    StepLog,
    new_run_id,
)
from ..policy import ProposedAction, PolicyEngine
from ..redaction import Redactor
from ..surface.base import Observation, Surface, SurfaceAction, SurfaceError
from . import locators, outcomes

# Enforced by tests/test_no_llm_in_replay.py: this module and everything it
# imports must not reach for a model client.

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        policy: PolicyEngine,
        evidence: RunEvidence,
        escalation: Optional[EscalationCoordinator] = None,
        *,
        allow_risky: bool = False,
        require_approval: bool = False,
    ) -> None:
        self.surface = surface
        self.policy = policy
        self.evidence = evidence
        self.escalation = escalation
        self.allow_risky = allow_risky
        self.require_approval = require_approval

    # -- entry point ------------------------------------------------------
    def run(self, capability: Capability, params: Dict[str, Any]) -> ReplayResult:
        started = time.time()
        run_id = self.evidence.run_id
        result = ReplayResult(
            capability_id=capability.id,
            capability_version=capability.version,
            run_id=run_id,
            status="running",  # terminal status is set exactly once, on exit
            evidence_dir=self.evidence.dir,
        )

        self._base_url = capability.target.base_url.rstrip("/")
        bound = capability.bind_params(params)
        for name in capability.sensitive_params():
            if name in bound:
                self.evidence.redactor.register(bound[name])

        self.evidence.log(
            "replay.start",
            capability=capability.id,
            version=capability.version,
            variant=capability.target.variant_id,
            params=bound,
            approval=capability.approval,
        )

        if self.require_approval and capability.approval != "approved":
            return self._fail(
                result,
                started,
                Failure(
                    step_id="<preflight>",
                    expected="capability approval == 'approved'",
                    observed=f"approval == {capability.approval!r}",
                ),
                "unattended replay requires an approved capability",
            )

        entry = capability.target.base_url.rstrip("/") + capability.target.entry_path
        gate = self.policy.check_url(entry)
        if not gate.allowed:
            return self._fail(
                result,
                started,
                Failure(step_id="<preflight>", expected="entry url on allowlist", observed=gate.reason),
                gate.reason,
            )

        observation: Optional[Observation] = None
        for step in capability.steps:
            step_result = self._run_step(capability, step, bound, result)
            observation = step_result
            if result.status in {"business_outcome", "escalated", "failed"}:
                return self._finish(result, started)

        # Final success checkpoint.
        if capability.success and observation is not None:
            ok, detail = self._check(capability.success, observation)
            if not ok:
                return self._fail(
                    result,
                    started,
                    Failure(
                        step_id="<success>",
                        expected=detail,
                        observed=_observed(observation),
                        url=observation.url,
                        screenshot=self._capture_failure("success-checkpoint"),
                    ),
                    "success checkpoint not met",
                )

        result.status = "success"
        self.evidence.log("replay.success", outputs=result.outputs)
        return self._finish(result, started)

    # -- steps ------------------------------------------------------------
    def _run_step(
        self,
        capability: Capability,
        step: Step,
        params: Dict[str, Any],
        result: ReplayResult,
    ) -> Optional[Observation]:
        started = time.time()
        recoveries = 0
        attempts = 0

        while True:
            attempts += 1
            observation = self.surface.observe()

            # 1. Runtime conditions that are true *before* we act (an
            #    interstitial that appeared, an expired session).
            rule = outcomes.match_rules(capability.outcome_rules, observation, step.id)
            if rule is not None:
                handled = self._handle_rule(
                    capability, rule, step, observation, result, recoveries
                )
                if handled == "recovered":
                    recoveries += 1
                    continue
                if handled == "stop":
                    return observation

            # 2. Policy gate — same engine as discovery used.
            proposed = self._proposed(step, observation, params)
            decision = self.policy.check(proposed)
            if decision.verdict == "deny":
                self._record(result, step, "failed", detail=decision.reason)
                result.failure = Failure(
                    step_id=step.id,
                    expected="action permitted by policy",
                    observed=decision.reason,
                    url=observation.url,
                )
                result.status = "failed"
                self.evidence.log("policy.denied", step=step.id, reason=decision.reason)
                return observation
            if decision.verdict == "require_approval" and not self.allow_risky:
                escalated = self._escalate(
                    capability, step, observation, result, decision.reason
                )
                if escalated == "resume":
                    continue
                return observation

            # 3. Resolve + act.
            try:
                observation = self._act(step, observation, params, result)
            except SurfaceError as exc:
                if attempts <= step.retries:
                    self.evidence.log("step.retry", step=step.id, error=str(exc))
                    continue
                self._record(result, step, "failed", detail=str(exc))
                result.status = "failed"
                result.failure = Failure(
                    step_id=step.id,
                    expected=f"{step.action} on {step.target.describe() if step.target else step.url_template}",
                    observed=str(exc),
                    url=observation.url,
                    screenshot=self._capture_failure(step.id),
                    dom_snapshot=self._capture_dom(step.id),
                )
                return observation
            except _StepStopped as stop:
                if stop.retry and attempts <= step.retries + 1:
                    self.evidence.log("step.retry_after_human", step=step.id)
                    continue
                return self.surface.observe()

            if result.status in {"business_outcome", "escalated", "failed"}:
                return observation

            # 4. Conditions that became true *because* of the action.
            after = self.surface.observe()
            rule = outcomes.match_rules(capability.outcome_rules, after, step.id)
            if rule is not None:
                handled = self._handle_rule(
                    capability, rule, step, after, result, recoveries
                )
                if handled == "recovered":
                    recoveries += 1
                    continue
                if handled == "stop":
                    return after

            # 5. Checkpoint: did we actually get where the recording expected?
            if step.checkpoint:
                ok, detail = self._check(step.checkpoint, after)
                if not ok:
                    if attempts <= step.retries:
                        self.evidence.log("checkpoint.retry", step=step.id, expected=detail)
                        continue
                    self._record(result, step, "failed", detail=f"checkpoint: {detail}")
                    result.status = "failed"
                    result.failure = Failure(
                        step_id=step.id,
                        expected=detail,
                        observed=_observed(after),
                        url=after.url,
                        screenshot=self._capture_failure(step.id),
                        dom_snapshot=self._capture_dom(step.id),
                    )
                    return after

            # 6. Declared outputs.
            for extraction in step.extract:
                value = self._extract(extraction, after, params)
                if value is not None:
                    result.outputs[extraction.name] = value
                    if extraction.redact:
                        self.evidence.redactor.register(value)

            self._record(
                result,
                step,
                "ok",
                duration_ms=int((time.time() - started) * 1000),
                attempts=attempts,
            )
            return after

    def _act(
        self,
        step: Step,
        observation: Observation,
        params: Dict[str, Any],
        result: ReplayResult,
    ) -> Observation:
        value = _render(step.value_template, params)
        url = _render(step.url_template, params)
        if url and url.startswith("/"):
            url = self._base_url + url

        if step.action in {"navigate", "wait_for", "read"}:
            action = SurfaceAction(
                kind=step.action,
                url=url,
                text=value or (step.wait.value or ""),
                timeout_ms=step.wait.timeout_ms,
            )
            self.evidence.log("step.act", step=step.id, action=step.action, url=url, value=value)
            self.surface.act(action, observation)
            return observation

        assert step.target is not None
        target = _render_ref(step.target, params)
        resolution = locators.resolve(target, observation, step.action)
        if not resolution.found:
            detail = (
                f"{resolution.detail}; "
                f"{locators.describe_near_misses(target, observation)}"
            )
            # A control we cannot find is the classic ambiguous case: it may be a
            # business outcome (the item does not exist) or real breakage. We let
            # the artifact's outcome rules speak first (already evaluated), then
            # escalate rather than guess.
            escalated = self._escalate_missing(step, observation, result, detail)
            if escalated == "resume":
                raise _StepStopped(retry=True)
            self._record(result, step, "failed", strategy=resolution.strategy, detail=detail)
            if result.status == "failed" and result.failure is None:
                result.failure = Failure(
                    step_id=step.id,
                    expected=f"control {target.describe()}",
                    observed=detail,
                    url=observation.url,
                    screenshot=self._capture_failure(step.id),
                    dom_snapshot=self._capture_dom(step.id),
                )
            raise _StepStopped(retry=False)

        self.evidence.log(
            "step.act",
            step=step.id,
            action=step.action,
            control=resolution.element.describe(),
            strategy=resolution.strategy,
            confidence=resolution.confidence,
            value=value,
        )
        self.surface.act(
            SurfaceAction(
                kind=step.action,
                ref=resolution.element.ref,
                text=value,
                key=step.key,
                timeout_ms=step.wait.timeout_ms,
            ),
            observation,
        )
        self._last_strategy = resolution.strategy
        return observation

    # -- outcome handling -------------------------------------------------
    def _handle_rule(
        self, capability, rule, step, observation, result, recoveries: int
    ) -> str:
        message = rule.message or rule.code
        if rule.classification == "business_outcome":
            result.status = "business_outcome"
            result.outcome = Outcome(rule.code, rule.classification, message, step.id)
            self._record(result, step, "outcome", detail=rule.code)
            self.evidence.log("outcome.business", step=step.id, code=rule.code, message=message)
            return "stop"

        if rule.classification == "recoverable":
            if recoveries >= rule.max_recoveries:
                result.status = "failed"
                result.failure = Failure(
                    step_id=step.id,
                    expected=f"{rule.code} cleared after recovery",
                    observed=f"still present after {recoveries} attempt(s)",
                    url=observation.url,
                    screenshot=self._capture_failure(step.id),
                )
                self.evidence.log("outcome.recovery_exhausted", step=step.id, code=rule.code)
                return "stop"
            try:
                detail = outcomes.run_recovery(rule.recovery, self.surface, observation, rule)
            except outcomes.RecoveryFailed as exc:
                result.status = "failed"
                result.failure = Failure(
                    step_id=step.id,
                    expected=f"recovery {rule.recovery!r} for {rule.code}",
                    observed=str(exc),
                    url=observation.url,
                    screenshot=self._capture_failure(step.id),
                )
                return "stop"
            self._record(result, step, "recovered", detail=f"{rule.code}: {detail}")
            self.evidence.log("outcome.recovered", step=step.id, code=rule.code, detail=detail)
            return "recovered"

        # hard_failure
        result.status = "failed"
        result.outcome = Outcome(rule.code, rule.classification, message, step.id)
        result.failure = Failure(
            step_id=step.id,
            expected="no blocking runtime condition",
            observed=message,
            url=observation.url,
            screenshot=self._capture_failure(step.id),
            dom_snapshot=self._capture_dom(step.id),
        )
        self.evidence.log("outcome.hard_failure", step=step.id, code=rule.code)
        return "stop"

    # -- escalation -------------------------------------------------------
    def _escalate(self, capability, step, observation, result, reason: str) -> str:
        return self._do_escalate(capability, step, observation, result, reason)

    def _escalate_missing(self, step, observation, result, detail: str) -> str:
        capability_id = result.capability_id
        if self.escalation is None:
            result.status = "failed"
            return "abort"
        return self._do_escalate(None, step, observation, result, detail, capability_id)

    def _do_escalate(
        self, capability, step, observation, result, reason: str, capability_id: str = ""
    ) -> str:
        if self.escalation is None:
            result.status = "escalated"
            result.outcome = Outcome(
                "NEEDS_HUMAN", "hard_failure", reason, step.id
            )
            self.evidence.log("escalation.skipped", step=step.id, reason=reason)
            return "abort"

        outcome = self.escalation.escalate(
            surface=self.surface,
            observation=observation,
            capability_id=capability.id if capability else capability_id,
            goal=capability.description if capability else "replay",
            step_id=step.id,
            reason=reason,
            run_id=result.run_id,
        )
        if outcome.action == "resume":
            self._record(result, step, "recovered", detail="human completed step")
            return "resume"
        if outcome.action == "skip_step":
            self._record(result, step, "recovered", detail="operator skipped step")
            return "resume"

        result.status = "escalated"
        result.outcome = Outcome("NEEDS_HUMAN", "hard_failure", reason, step.id)
        return "abort"

    # -- helpers ----------------------------------------------------------
    def _proposed(self, step: Step, observation: Observation, params) -> ProposedAction:
        url = _render(step.url_template, params) or observation.url
        if url.startswith("/"):
            url = getattr(self, "_base_url", "") + url
        name = step.target.name if step.target else ""
        role = step.target.role if step.target else ""
        return ProposedAction(
            kind=step.action,
            url=url,
            control_name=_render(name, params) or name,
            control_role=role,
            value=_render(step.value_template, params),
        )

    def _check(self, checkpoint: Checkpoint, observation: Observation):
        for condition in checkpoint.conditions:
            if not outcomes.evaluate(condition, observation):
                return False, (
                    condition.description
                    or f"{condition.kind}({condition.value!r})"
                )
        return True, "all conditions met"

    def _extract(
        self, extraction: Extraction, observation: Observation, params: Dict[str, Any]
    ) -> Optional[str]:
        if extraction.method == "regex" and extraction.pattern:
            match = re.search(extraction.pattern, observation.text, re.I | re.M)
            return match.group(extraction.group) if match else None
        if extraction.target is None:
            return None
        resolution = locators.resolve(
            _render_ref(extraction.target, params), observation, "read"
        )
        if not resolution.found:
            return None
        if extraction.method == "control_value":
            return resolution.element.value
        return resolution.element.name

    def _record(self, result: ReplayResult, step: Step, status: str, **kw) -> None:
        result.steps.append(
            StepLog(
                step_id=step.id,
                action=step.action,
                status=status,
                strategy=kw.get("strategy", getattr(self, "_last_strategy", None)),
                detail=kw.get("detail"),
                duration_ms=kw.get("duration_ms", 0),
                attempts=kw.get("attempts", 1),
            )
        )

    def _capture_failure(self, label: str) -> Optional[str]:
        if not Redactor.allow_screenshot("failure", self.policy.allow_screenshots):
            return None
        path = self.evidence.screenshot_path(f"fail-{label}")
        try:
            self.surface.page.screenshot(path=path)  # type: ignore[attr-defined]
            return path
        except Exception:
            return None

    def _capture_dom(self, label: str) -> Optional[str]:
        snapshot = getattr(self.surface, "dom_snapshot", None)
        if snapshot is None:
            return None
        try:
            return snapshot(self.evidence.observation_path(f"dom-{label}.html"))
        except Exception:
            return None

    def _fail(self, result, started, failure: Failure, message: str) -> ReplayResult:
        result.status = "failed"
        result.failure = failure
        self.evidence.log("replay.failed", reason=message)
        return self._finish(result, started)

    def _finish(self, result: ReplayResult, started: float) -> ReplayResult:
        result.duration_ms = int((time.time() - started) * 1000)
        self.evidence.write_json("result.json", result.to_dict())
        self.evidence.log("replay.end", status=result.status, duration_ms=result.duration_ms)
        return result


class _StepStopped(Exception):
    """Internal: the step ended early (failure, or a human took over)."""

    def __init__(self, retry: bool = False) -> None:
        super().__init__("step stopped")
        self.retry = retry


def _render(template: Optional[str], params: Dict[str, Any]) -> Optional[str]:
    if template is None:
        return None

    def sub(match):
        key = match.group(1)
        if key not in params:
            raise KeyError(f"no value supplied for parameter {key!r}")
        return str(params[key])

    return _PLACEHOLDER.sub(sub, template)


def _render_ref(ref, params: Dict[str, Any]):
    """Parameterised control names (`"{{item_name}}"`) are rendered per call."""
    from dataclasses import replace

    return replace(
        ref,
        name=_render(ref.name, params) or "",
        anchor_text=_render(ref.anchor_text, params) if ref.anchor_text else None,
    )


def make_run_id() -> str:
    return new_run_id("replay")


def _observed(observation: Observation) -> str:
    head = (observation.text or "").strip().splitlines()[:4]
    return f"url={observation.url} text={' / '.join(head)!r}"

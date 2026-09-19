"""Runtime conditions: detection, classification, and bounded recovery.

The brief's central trap is conflating "no such member" with "the automation
broke". So classification is data, not code: every condition a flow knows about
is an `OutcomeRule` in the artifact, and the engine's only job is to evaluate
rules in order and dispatch on `classification`.

* business_outcome -> stop, return it as a *successful* call with a code
* recoverable      -> run a named handler, re-observe, continue (bounded)
* hard_failure     -> stop, capture evidence, return a debuggable failure

Recovery handlers are a closed set. Recovery is never "ask the model what to
do" — that would put the LLM back in the decision loop we just removed.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional

from ..models import Condition, OutcomeRule
from ..surface.base import Observation, Surface, SurfaceAction

# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def evaluate(condition: Condition, observation: Observation) -> bool:
    haystack = f"{observation.text}\n" + "\n".join(observation.dialogs)
    if condition.kind == "text_present":
        return _contains(haystack, condition.value)
    if condition.kind == "text_absent":
        return not _contains(haystack, condition.value)
    if condition.kind == "url_matches":
        return bool(re.search(condition.value, observation.url or ""))
    if condition.kind == "control_present":
        needle = condition.value.lower()
        return any(needle in (e.name or "").lower() for e in observation.elements)
    raise ValueError(f"unknown condition kind {condition.kind!r}")


def _contains(haystack: str, needle: str) -> bool:
    return needle.lower() in (haystack or "").lower()


def match_rules(
    rules: List[OutcomeRule], observation: Observation, step_id: str
) -> Optional[OutcomeRule]:
    """First matching rule wins; order in the artifact is meaningful."""
    for rule in rules:
        if "*" not in rule.applies_to and step_id not in rule.applies_to:
            continue
        if evaluate(rule.when, observation):
            return rule
    return None


# ---------------------------------------------------------------------------
# Recovery handlers (closed set)
# ---------------------------------------------------------------------------

Handler = Callable[[Surface, Observation, OutcomeRule], str]


def _dismiss_interstitial(surface: Surface, observation: Observation, rule: OutcomeRule) -> str:
    """Click the first obvious 'continue past this notice' control."""
    wanted = ("continue", "ok", "close", "dismiss", "acknowledge", "proceed")
    for element in observation.elements:
        if element.role in {"button", "link"} and element.name.strip().lower() in wanted:
            surface.act(SurfaceAction(kind="click", ref=element.ref), observation)
            return f"dismissed interstitial via '{element.name}'"
    raise RecoveryFailed("no dismiss control found on the interstitial")


def _retry_wait(surface: Surface, observation: Observation, rule: OutcomeRule) -> str:
    surface.act(SurfaceAction(kind="wait_for", text="", timeout_ms=1500), observation)
    return "waited for transient condition to clear"


def _reload(surface: Surface, observation: Observation, rule: OutcomeRule) -> str:
    surface.act(
        SurfaceAction(kind="navigate", url=observation.url, timeout_ms=15000), observation
    )
    return "reloaded the current page"


HANDLERS: Dict[str, Handler] = {
    "dismiss_interstitial": _dismiss_interstitial,
    "retry_wait": _retry_wait,
    "reload": _reload,
}


class RecoveryFailed(RuntimeError):
    """A declared recovery handler could not do its job."""


def run_recovery(
    name: str, surface: Surface, observation: Observation, rule: OutcomeRule
) -> str:
    handler = HANDLERS.get(name)
    if handler is None:
        raise RecoveryFailed(f"unknown recovery handler {name!r}")
    return handler(surface, observation, rule)

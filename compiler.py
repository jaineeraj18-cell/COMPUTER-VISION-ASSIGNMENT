"""Compile a `DiscoveryRun` into a versioned capability artifact.

This is the step that makes the artifact a *capability* rather than a transcript:

* concrete values used during discovery are lifted back into typed parameters
  (`"Sauce Labs Backpack"` -> `{{item_name}}`, and the same inside URLs, which
  is the `/item/12345` -> `/item/:id` canonicalisation the brief mentions);
* URLs are stored relative to `target.base_url`, so the same artifact points at
  a different tenant's host without editing a step;
* every control is described at intent level (role + accessible name + the
  legacy label anchor), never as a raw selector;
* checkpoints are derived from observed state transitions, plus the success
  condition the model was required to name;
* the model's proposed outcome rules are merged with a built-in library of
  conditions that are true of every enterprise app (session expiry, server
  error), with the built-ins last so a flow-specific rule wins.

Nothing here trusts the model blindly: the result is validated against the
schema, and anything it proposed that does not typecheck is dropped and logged.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

from ..models import (
    Capability,
    Checkpoint,
    Condition,
    ControlRef,
    Extraction,
    OutcomeRule,
    OutputSpec,
    ParamSpec,
    Provenance,
    Step,
    TargetSpec,
    WaitSpec,
)
from ..policy import PolicyEngine, ProposedAction
from ..serde import SchemaError
from .loop import DiscoveryRun, ExecutedAction

_DEFAULT_RULES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config",
    "outcome_rules.yaml",
)


def compile_capability(
    run: DiscoveryRun,
    policy: PolicyEngine,
    *,
    capability_id: str,
    app_id: str,
    sensitive_params: List[str],
    rules_path: Optional[str] = None,
    param_types: Optional[Dict[str, str]] = None,
    log=lambda *a, **k: None,
) -> Capability:
    if run.status != "complete":
        raise SchemaError(
            f"refusing to compile an artifact from a {run.status} run "
            f"({run.stop_reason}) - a capability must come from a run that "
            "actually reached the goal"
        )

    base_url = _origin(run.target_url)
    params = _params(run, sensitive_params, param_types or {})
    # Secrets are lifted too - that is precisely how the raw value is kept out
    # of the artifact: the step stores "{{password}}", never the password.
    lifted = {
        name: str(value) for name, value in run.params.items() if len(str(value)) >= 3
    }

    steps: List[Step] = []
    outputs: List[OutputSpec] = []
    for index, action in enumerate(run.actions, start=1):
        step = _step(index, action, lifted, base_url, policy)
        for spec in action.extractions:
            extraction, output = _extraction(spec, lifted)
            step.extract.append(extraction)
            outputs.append(output)
        steps.append(step)

    if not steps:
        raise SchemaError("discovery run produced no actions to record")

    # The entry navigation is implicit in the run; make it an explicit step so
    # replay never depends on browser state it did not create.
    entry_path = _relative(run.target_url, base_url)
    if steps[0].action != "navigate":
        steps.insert(
            0,
            Step(
                id="s0-navigate",
                action="navigate",
                url_template=entry_path,
                wait=WaitSpec(kind="settle", timeout_ms=15000),
                notes="entry point, inserted by the compiler",
            ),
        )

    success = _success(run.finish, lifted)
    if success and steps[-1].checkpoint is None:
        steps[-1].checkpoint = success

    capability = Capability(
        id=capability_id,
        name=capability_id.replace(".", " ").replace("_", " "),
        description=run.finish.get("summary") or run.goal,
        target=TargetSpec(
            base_url=base_url,
            entry_path=entry_path,
            app_id=app_id,
            variant_id="base",
        ),
        version=1,
        surface="web",
        params=params,
        outputs=outputs,
        steps=steps,
        outcome_rules=_outcome_rules(run.finish, rules_path or _DEFAULT_RULES, log),
        success=success,
        approval="draft",
        provenance=Provenance(
            discovered_by=run.model,
            run_id=run.run_id,
            goal=run.goal,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            llm_steps=run.llm_steps,
            human_interventions=run.human_interventions,
        ),
    )
    capability.validate()
    return capability


# ---------------------------------------------------------------------------
# pieces
# ---------------------------------------------------------------------------


def _params(run: DiscoveryRun, sensitive: List[str], types: Dict[str, str]) -> List[ParamSpec]:
    specs = []
    for name, value in run.params.items():
        is_secret = name in sensitive
        specs.append(
            ParamSpec(
                name=name,
                type=types.get(name, "string"),
                required=True,
                description=f"supplied per invocation (discovered as {name})",
                sensitive=is_secret,
                example=None if is_secret else str(value),
            )
        )
    return specs


def _step(
    index: int,
    action: ExecutedAction,
    lifted: Dict[str, str],
    base_url: str,
    policy: PolicyEngine,
) -> Step:
    step_id = f"s{index}-{action.action}"
    target = None
    if action.element is not None:
        element = action.element
        target = ControlRef(
            role=element.role,
            name=_templatize(element.name, lifted),
            name_match="exact",
            ordinal=element.ordinal,
            anchor_text=_templatize(element.anchor_text or "", lifted) or None,
            css_hint=element.css_hint,
            frame_hint=_frame_hint(element.frame, action.url_before),
            notes=action.why,
        )

    risky = policy.is_risky(
        ProposedAction(
            kind=action.action,
            url=action.url_after,
            control_name=action.element.name if action.element else "",
            value=action.value,
        )
    )

    checkpoint = None
    if action.url_after != action.url_before:
        pattern = _url_pattern(_templatize(_relative(action.url_after, base_url), lifted))
        checkpoint = Checkpoint(
            conditions=[
                Condition(
                    kind="url_matches",
                    value=pattern,
                    description=f"navigation to {_relative(action.url_after, base_url)}",
                )
            ],
            description="observed state transition during discovery",
        )

    return Step(
        id=step_id,
        action=action.action,
        target=target,
        value_template=_templatize(action.value, lifted) if action.value else None,
        url_template=_relative(action.url, base_url) if action.url else None,
        key=action.key,
        wait=WaitSpec(kind="settle", timeout_ms=10000),
        checkpoint=checkpoint,
        risk="risky" if risky else "safe",
        retries=1,
        notes=action.why,
    )


def _extraction(spec: Dict[str, Any], lifted: Dict[str, str]):
    name = spec["name"]
    element = spec.get("element")
    if spec.get("pattern"):
        extraction = Extraction(name=name, method="regex", pattern=spec["pattern"])
    elif element is not None:
        extraction = Extraction(
            name=name,
            method="control_name",
            target=ControlRef(
                role=element.role,
                name=_templatize(element.name, lifted),
                ordinal=element.ordinal,
                anchor_text=element.anchor_text,
                css_hint=element.css_hint,
            ),
        )
    else:
        extraction = Extraction(name=name, method="regex", pattern=f"{name}[:\\s]+(.+)")
    return extraction, OutputSpec(
        name=name, type="string", description=spec.get("description", "")
    )


def _success(finish: Dict[str, Any], lifted: Dict[str, str]) -> Optional[Checkpoint]:
    conditions = []
    text = finish.get("success_text")
    if text:
        conditions.append(
            Condition(
                kind="text_present",
                value=_templatize(text, lifted),
                description=f"final screen shows {text!r}",
            )
        )
    pattern = finish.get("success_url_pattern")
    if pattern:
        conditions.append(
            Condition(kind="url_matches", value=pattern, description="final url")
        )
    if not conditions:
        return None
    return Checkpoint(conditions=conditions, description="capability success condition")


def _outcome_rules(finish: Dict[str, Any], rules_path: str, log) -> List[OutcomeRule]:
    rules: List[OutcomeRule] = []
    seen = set()
    for raw in finish.get("outcome_rules") or []:
        try:
            rule = OutcomeRule(
                code=str(raw["code"]).upper().replace(" ", "_"),
                classification=raw["classification"],
                when=Condition(kind="text_present", value=raw["text"]),
                message=raw.get("message", ""),
                recovery=raw.get("recovery"),
            )
            if rule.classification == "recoverable" and not rule.recovery:
                raise ValueError("recoverable rule without a recovery handler")
        except (KeyError, ValueError) as exc:
            log("compiler.rule_rejected", raw=raw, error=str(exc))
            continue
        if rule.code in seen:
            continue
        seen.add(rule.code)
        rules.append(rule)

    for raw in _load_library(rules_path):
        if raw["code"] in seen:
            continue
        seen.add(raw["code"])
        rules.append(OutcomeRule.from_dict(raw))
    return rules


def _load_library(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return raw.get("rules", [])


# ---------------------------------------------------------------------------
# url + value handling
# ---------------------------------------------------------------------------


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _relative(url: Optional[str], base_url: str) -> str:
    if not url:
        return "/"
    if url.startswith(base_url):
        rest = url[len(base_url) :]
        return rest or "/"
    parsed = urlparse(url)
    return (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")


def _frame_hint(frame_url: Optional[str], page_url: str) -> Optional[str]:
    if not frame_url or frame_url == page_url:
        return None
    return urlparse(frame_url).path or None


_REGEX_META = re.compile(r"([.?+*()\[\]{}^$|\\])")


def _url_pattern(path: str) -> str:
    r"""Escape a recorded path into a regex, keeping it human-readable.

    Only true metacharacters are escaped, so a reviewer still sees
    `/inventory-item.html\?id=4` rather than a wall of backslashes, and any
    `{{param}}` placeholder survives to be rendered at call time.
    """
    return _REGEX_META.sub(r"\\\1", path)


def _templatize(text: Optional[str], lifted: Dict[str, str]) -> str:
    """Replace concrete discovery values with their parameter placeholders."""
    if not text:
        return ""
    out = text
    for name, value in sorted(lifted.items(), key=lambda kv: -len(kv[1])):
        if value and value.lower() in out.lower():
            out = re.sub(re.escape(value), "{{" + name + "}}", out, flags=re.I)
    return out

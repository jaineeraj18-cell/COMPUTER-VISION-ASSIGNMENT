"""Capability artifact schema (`capability/v1`) and the replay result contract.

A *capability* is what the LLM discovery run compiles into: a typed, versioned,
reviewable description of a flow that a downstream AI agent can invoke by name
with typed arguments, and that our replay engine can execute with no model in
the decision loop.

Design notes live in REPORT.md section 2. The short version:

* Steps reference controls through a `ControlRef` (an intent-level description:
  role + accessible name + disambiguators), never through a raw CSS selector
  alone. CSS is kept only as a last-resort hint.
* Params and outputs are declared separately from steps so the artifact reads as
  a function signature, not a macro recording.
* Runtime conditions (validation errors, "not found", session expiry) are
  declared as `OutcomeRule`s so replay can classify them instead of crashing.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .serde import SchemaError, many, opt, to_jsonable

SCHEMA_VERSION = "capability/v1"

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

#: Action verbs a step may use. Kept small on purpose: every verb we add is a
#: verb every surface adapter (web, legacy web, desktop) must implement.
ACTION_KINDS = (
    "navigate",
    "click",
    "type",
    "select",
    "press_key",
    "wait_for",
    "read",
)

SAFE_ACTIONS = {"navigate", "read", "wait_for"}

CLASSIFICATIONS = ("business_outcome", "recoverable", "hard_failure")


# ---------------------------------------------------------------------------
# Control targeting
# ---------------------------------------------------------------------------


@dataclass
class ControlRef:
    """How a step identifies the control it acts on.

    Ordered by how much we trust it. `role` + `name` is what a human operator
    (and a screen reader, and a desktop accessibility API) uses, so it is the
    primary key. Everything else exists to disambiguate or to fall back.
    """

    role: str
    name: str = ""
    name_match: str = "exact"  # exact | contains | regex
    ordinal: int = 0  # nth match when role+name is ambiguous
    anchor_text: Optional[str] = None  # nearby label/row text (legacy tables)
    css_hint: Optional[str] = None  # last-resort fallback, never trusted first
    frame_hint: Optional[str] = None  # frame url/name for frameset apps
    # "ordinal" is deliberately NOT a default: matching "the 3rd button on the
    # page" will happily click the wrong thing. It exists for refs that have no
    # usable name at all, and then only when a recorder opts in.
    strategies: List[str] = field(
        default_factory=lambda: ["role_name", "anchor", "css"]
    )
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ControlRef":
        return cls(
            role=d["role"],
            name=d.get("name", ""),
            name_match=d.get("name_match", "exact"),
            ordinal=int(d.get("ordinal", 0)),
            anchor_text=d.get("anchor_text"),
            css_hint=d.get("css_hint"),
            frame_hint=d.get("frame_hint"),
            strategies=d.get("strategies") or ["role_name", "anchor", "css"],
            notes=d.get("notes"),
        )

    def describe(self) -> str:
        name = self.name or "<unnamed>"
        suffix = f"[{self.ordinal}]" if self.ordinal else ""
        return f"{self.role} '{name}'{suffix}"


@dataclass
class WaitSpec:
    """What must be true before a step is considered ready / finished."""

    kind: str = "settle"  # settle | text | control | url | none
    value: Optional[str] = None
    timeout_ms: int = 10_000

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WaitSpec":
        return cls(
            kind=d.get("kind", "settle"),
            value=d.get("value"),
            timeout_ms=int(d.get("timeout_ms", 10_000)),
        )


@dataclass
class Condition:
    """A predicate evaluated against an Observation."""

    kind: str  # text_present | text_absent | url_matches | control_present
    value: str
    description: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Condition":
        return cls(
            kind=d["kind"], value=d["value"], description=d.get("description")
        )


@dataclass
class Checkpoint:
    """An assertion that we actually reached the state we expected."""

    conditions: List[Condition] = field(default_factory=list)
    timeout_ms: int = 10_000
    description: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Checkpoint":
        return cls(
            conditions=many(Condition.from_dict, d.get("conditions")),
            timeout_ms=int(d.get("timeout_ms", 10_000)),
            description=d.get("description"),
        )


@dataclass
class Extraction:
    """A declared output read off the screen."""

    name: str
    method: str = "control_name"  # control_name | control_value | regex
    target: Optional[ControlRef] = None
    pattern: Optional[str] = None  # for method == regex
    group: int = 1
    redact: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Extraction":
        return cls(
            name=d["name"],
            method=d.get("method", "control_name"),
            target=opt(ControlRef.from_dict, d.get("target")),
            pattern=d.get("pattern"),
            group=int(d.get("group", 1)),
            redact=bool(d.get("redact", False)),
        )


@dataclass
class OutcomeRule:
    """A runtime condition the flow knows how to recognise.

    This is the load-bearing idea for error handling: a replay does not "fail"
    because the page said *Sorry, this user has been locked out* — it returns a
    classified outcome the caller can act on.
    """

    code: str
    classification: str  # business_outcome | recoverable | hard_failure
    when: Condition
    message: str = ""
    recovery: Optional[str] = None  # handler name, for `recoverable`
    max_recoveries: int = 2
    applies_to: List[str] = field(default_factory=lambda: ["*"])  # step ids

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OutcomeRule":
        return cls(
            code=d["code"],
            classification=d["classification"],
            when=Condition.from_dict(d["when"]),
            message=d.get("message", ""),
            recovery=d.get("recovery"),
            max_recoveries=int(d.get("max_recoveries", 2)),
            applies_to=d.get("applies_to") or ["*"],
        )


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


@dataclass
class ParamSpec:
    name: str
    type: str = "string"  # string | number | boolean
    required: bool = True
    description: str = ""
    sensitive: bool = False  # never written to artifact, logs or evidence
    example: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ParamSpec":
        return cls(
            name=d["name"],
            type=d.get("type", "string"),
            required=bool(d.get("required", True)),
            description=d.get("description", ""),
            sensitive=bool(d.get("sensitive", False)),
            example=d.get("example"),
        )


@dataclass
class OutputSpec:
    name: str
    type: str = "string"
    description: str = ""
    redact: bool = False

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OutputSpec":
        return cls(
            name=d["name"],
            type=d.get("type", "string"),
            description=d.get("description", ""),
            redact=bool(d.get("redact", False)),
        )


@dataclass
class Step:
    id: str
    action: str
    target: Optional[ControlRef] = None
    value_template: Optional[str] = None  # "{{item_name}}"
    url_template: Optional[str] = None
    key: Optional[str] = None
    wait: WaitSpec = field(default_factory=WaitSpec)
    checkpoint: Optional[Checkpoint] = None
    extract: List[Extraction] = field(default_factory=list)
    risk: str = "safe"  # safe | risky (irreversible / side-effecting)
    retries: int = 1
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Step":
        return cls(
            id=d["id"],
            action=d["action"],
            target=opt(ControlRef.from_dict, d.get("target")),
            value_template=d.get("value_template"),
            url_template=d.get("url_template"),
            key=d.get("key"),
            wait=WaitSpec.from_dict(d.get("wait") or {}),
            checkpoint=opt(Checkpoint.from_dict, d.get("checkpoint")),
            extract=many(Extraction.from_dict, d.get("extract")),
            risk=d.get("risk", "safe"),
            retries=int(d.get("retries", 1)),
            notes=d.get("notes"),
        )


@dataclass
class TargetSpec:
    """Where the capability runs.

    `app_id` + `vendor_version` are what make cross-tenant reuse possible: the
    artifact is recorded against a *product*, and a tenant is a variant of it.
    """

    base_url: str
    entry_path: str = "/"
    app_id: str = "unknown-app"
    vendor_version: Optional[str] = None
    variant_id: str = "base"

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TargetSpec":
        return cls(
            base_url=d["base_url"],
            entry_path=d.get("entry_path", "/"),
            app_id=d.get("app_id", "unknown-app"),
            vendor_version=d.get("vendor_version"),
            variant_id=d.get("variant_id", "base"),
        )


@dataclass
class Provenance:
    discovered_by: str = ""  # model id
    run_id: str = ""
    goal: str = ""
    created_at: str = ""
    llm_steps: int = 0
    human_interventions: int = 0

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Provenance":
        return cls(
            discovered_by=d.get("discovered_by", ""),
            run_id=d.get("run_id", ""),
            goal=d.get("goal", ""),
            created_at=d.get("created_at", ""),
            llm_steps=int(d.get("llm_steps", 0)),
            human_interventions=int(d.get("human_interventions", 0)),
        )


@dataclass
class Stability:
    replays: int = 0
    successes: int = 0

    @property
    def score(self) -> Optional[float]:
        return None if not self.replays else round(self.successes / self.replays, 3)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Stability":
        return cls(
            replays=int(d.get("replays", 0)), successes=int(d.get("successes", 0))
        )


@dataclass
class VariantOverride:
    """Per-tenant specialisation of a base capability.

    Tenants run the same vendor product with different branding and labels, so
    we override *fields of named steps* rather than forking the whole flow. Any
    override is visible in one place for review.
    """

    variant_id: str
    base_url: Optional[str] = None
    step_patches: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "VariantOverride":
        return cls(
            variant_id=d["variant_id"],
            base_url=d.get("base_url"),
            step_patches=d.get("step_patches") or {},
            notes=d.get("notes"),
        )


@dataclass
class Capability:
    id: str
    name: str
    description: str
    target: TargetSpec
    version: int = 1
    schema_version: str = SCHEMA_VERSION
    surface: str = "web"
    params: List[ParamSpec] = field(default_factory=list)
    outputs: List[OutputSpec] = field(default_factory=list)
    steps: List[Step] = field(default_factory=list)
    outcome_rules: List[OutcomeRule] = field(default_factory=list)
    success: Optional[Checkpoint] = None
    approval: str = "draft"  # draft | approved (gates unattended replay)
    provenance: Provenance = field(default_factory=Provenance)
    stability: Stability = field(default_factory=Stability)
    variants: List[VariantOverride] = field(default_factory=list)

    # -- serialisation ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        d = to_jsonable(self)
        d["stability"] = dict(d.get("stability", {}))
        d["stability"]["score"] = self.stability.score
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Capability":
        if d.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
            raise SchemaError(
                f"unsupported schema_version {d.get('schema_version')!r}; "
                f"this engine speaks {SCHEMA_VERSION}"
            )
        cap = cls(
            id=d["id"],
            name=d["name"],
            description=d.get("description", ""),
            target=TargetSpec.from_dict(d["target"]),
            version=int(d.get("version", 1)),
            surface=d.get("surface", "web"),
            params=many(ParamSpec.from_dict, d.get("params")),
            outputs=many(OutputSpec.from_dict, d.get("outputs")),
            steps=many(Step.from_dict, d.get("steps")),
            outcome_rules=many(OutcomeRule.from_dict, d.get("outcome_rules")),
            success=opt(Checkpoint.from_dict, d.get("success")),
            approval=d.get("approval", "draft"),
            provenance=Provenance.from_dict(d.get("provenance") or {}),
            stability=Stability.from_dict(d.get("stability") or {}),
            variants=many(VariantOverride.from_dict, d.get("variants")),
        )
        cap.validate()
        return cap

    # -- contract ---------------------------------------------------------
    def validate(self) -> None:
        if not self.steps:
            raise SchemaError("capability has no steps")
        seen = set()
        for step in self.steps:
            if step.id in seen:
                raise SchemaError(f"duplicate step id {step.id!r}")
            seen.add(step.id)
            if step.action not in ACTION_KINDS:
                raise SchemaError(f"step {step.id}: unknown action {step.action!r}")
            if step.action in {"click", "type", "select"} and step.target is None:
                raise SchemaError(f"step {step.id}: {step.action} needs a target")
            if step.action == "navigate" and not step.url_template:
                raise SchemaError(f"step {step.id}: navigate needs url_template")
            if step.risk not in {"safe", "risky"}:
                raise SchemaError(f"step {step.id}: bad risk {step.risk!r}")
        for rule in self.outcome_rules:
            if rule.classification not in CLASSIFICATIONS:
                raise SchemaError(
                    f"outcome rule {rule.code}: bad classification "
                    f"{rule.classification!r}"
                )
            if rule.classification == "recoverable" and not rule.recovery:
                raise SchemaError(
                    f"outcome rule {rule.code}: recoverable rules need a recovery"
                )
        declared = {p.name for p in self.params}
        for placeholder in self.placeholders():
            if placeholder not in declared:
                raise SchemaError(f"step references undeclared param {placeholder!r}")

    def placeholders(self) -> List[str]:
        import re

        found: List[str] = []
        for step in self.steps:
            texts = [step.value_template, step.url_template]
            if step.target is not None:
                texts += [step.target.name, step.target.anchor_text]
            for extraction in step.extract:
                if extraction.target is not None:
                    texts.append(extraction.target.name)
            for text in texts:
                if text:
                    found.extend(re.findall(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", text))
        return sorted(set(found))

    def bind_params(self, supplied: Dict[str, Any]) -> Dict[str, Any]:
        """Validate + coerce invocation arguments against the declared params."""
        bound: Dict[str, Any] = {}
        for spec in self.params:
            if spec.name in supplied and supplied[spec.name] is not None:
                bound[spec.name] = _coerce(spec, supplied[spec.name])
            elif spec.required:
                raise SchemaError(f"missing required parameter {spec.name!r}")
        unknown = set(supplied) - {p.name for p in self.params}
        if unknown:
            raise SchemaError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
        return bound

    def sensitive_params(self) -> List[str]:
        return [p.name for p in self.params if p.sensitive]

    def for_variant(self, variant_id: str) -> "Capability":
        """Return a copy specialised for a tenant variant of the same product."""
        if variant_id == self.target.variant_id:
            return self
        override = next(
            (v for v in self.variants if v.variant_id == variant_id), None
        )
        if override is None:
            raise SchemaError(f"no override registered for variant {variant_id!r}")
        clone = Capability.from_dict(self.to_dict())
        clone.target.variant_id = variant_id
        if override.base_url:
            clone.target.base_url = override.base_url
        for step in clone.steps:
            patch = override.step_patches.get(step.id)
            if not patch:
                continue
            for key, value in patch.items():
                if key == "target" and step.target is not None:
                    merged = {**to_jsonable(step.target), **value}
                    step.target = ControlRef.from_dict(merged)
                elif key == "checkpoint":
                    step.checkpoint = Checkpoint.from_dict(value)
                else:
                    setattr(step, key, value)
        return clone

    def signature(self) -> Dict[str, Any]:
        """JSON-Schema-ish tool definition for an agent-facing catalog."""
        return {
            "name": self.id,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": {
                    p.name: {"type": _json_type(p.type), "description": p.description}
                    for p in self.params
                },
                "required": [p.name for p in self.params if p.required],
            },
            "returns": {
                o.name: {"type": _json_type(o.type), "description": o.description}
                for o in self.outputs
            },
            "approval": self.approval,
            "stability": self.stability.score,
        }


def _json_type(t: str) -> str:
    return {"string": "string", "number": "number", "boolean": "boolean"}.get(t, "string")


def _coerce(spec: ParamSpec, value: Any) -> Any:
    try:
        if spec.type == "number":
            return float(value)
        if spec.type == "boolean":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes"}
        return str(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise SchemaError(f"parameter {spec.name!r} is not a {spec.type}") from exc


# ---------------------------------------------------------------------------
# Replay result contract
# ---------------------------------------------------------------------------

#: The four things a replay can tell its caller. Anything that is not one of
#: these is a bug in the engine, not a new case.
REPLAY_STATUSES = ("success", "business_outcome", "escalated", "failed")


@dataclass
class StepLog:
    step_id: str
    action: str
    status: str  # ok | recovered | outcome | failed
    strategy: Optional[str] = None  # which locator strategy resolved the control
    detail: Optional[str] = None
    duration_ms: int = 0
    attempts: int = 1


@dataclass
class Failure:
    step_id: str
    expected: str
    observed: str
    url: str = ""
    screenshot: Optional[str] = None
    dom_snapshot: Optional[str] = None


@dataclass
class Outcome:
    code: str
    classification: str
    message: str
    step_id: Optional[str] = None


@dataclass
class ReplayResult:
    capability_id: str
    capability_version: int
    run_id: str
    status: str
    outputs: Dict[str, Any] = field(default_factory=dict)
    outcome: Optional[Outcome] = None
    failure: Optional[Failure] = None
    steps: List[StepLog] = field(default_factory=list)
    evidence_dir: str = ""
    duration_ms: int = 0
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def to_dict(self) -> Dict[str, Any]:
        return to_jsonable(self)

    @property
    def ok(self) -> bool:
        return self.status == "success"


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"


__all__ = [n for n in dir() if not n.startswith("_")]

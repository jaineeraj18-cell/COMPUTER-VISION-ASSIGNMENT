import pytest

from cua.models import (
    Capability,
    Checkpoint,
    Condition,
    ControlRef,
    OutcomeRule,
    ParamSpec,
    Step,
    TargetSpec,
)
from cua.serde import SchemaError


def make_capability(**kw) -> Capability:
    base = dict(
        id="demo.flow",
        name="demo flow",
        description="demo",
        target=TargetSpec(base_url="https://example.test", entry_path="/"),
        params=[ParamSpec(name="item_name", example="Widget")],
        steps=[
            Step(id="s1-navigate", action="navigate", url_template="/"),
            Step(
                id="s2-click",
                action="click",
                target=ControlRef(role="button", name="Add {{item_name}}"),
            ),
        ],
    )
    base.update(kw)
    return Capability(**base)


def test_round_trip_is_lossless():
    capability = make_capability()
    restored = Capability.from_dict(capability.to_dict())
    assert restored.id == capability.id
    assert [s.id for s in restored.steps] == [s.id for s in capability.steps]
    assert restored.steps[1].target.name == "Add {{item_name}}"


def test_undeclared_placeholder_is_rejected():
    capability = make_capability(params=[])
    with pytest.raises(SchemaError):
        capability.validate()


def test_click_without_target_is_rejected():
    capability = make_capability(steps=[Step(id="s1", action="click")])
    with pytest.raises(SchemaError):
        capability.validate()


def test_recoverable_rule_needs_a_handler():
    capability = make_capability(
        outcome_rules=[
            OutcomeRule(
                code="X",
                classification="recoverable",
                when=Condition(kind="text_present", value="oops"),
            )
        ]
    )
    with pytest.raises(SchemaError):
        capability.validate()


def test_bind_params_enforces_the_contract():
    capability = make_capability()
    assert capability.bind_params({"item_name": "Widget"}) == {"item_name": "Widget"}
    with pytest.raises(SchemaError):
        capability.bind_params({})
    with pytest.raises(SchemaError):
        capability.bind_params({"item_name": "Widget", "nope": 1})


def test_signature_is_agent_callable():
    signature = make_capability().signature()
    assert signature["name"] == "demo.flow"
    assert signature["input_schema"]["required"] == ["item_name"]


def test_variant_override_specialises_without_forking():
    capability = make_capability()
    capability.variants = [
        __import__("cua.models", fromlist=["VariantOverride"]).VariantOverride(
            variant_id="tenant-b",
            base_url="http://127.0.0.1:8799",
            step_patches={"s2-click": {"target": {"name": "Basket {{item_name}}"}}},
        )
    ]
    specialised = capability.for_variant("tenant-b")

    assert specialised.target.base_url == "http://127.0.0.1:8799"
    assert specialised.steps[1].target.name == "Basket {{item_name}}"
    # the override merges into the base ref rather than replacing it
    assert specialised.steps[1].target.role == "button"
    # and the base capability is untouched
    assert capability.steps[1].target.name == "Add {{item_name}}"


def test_unknown_schema_version_is_refused():
    raw = make_capability().to_dict()
    raw["schema_version"] = "capability/v99"
    with pytest.raises(SchemaError):
        Capability.from_dict(raw)


def test_success_checkpoint_serialises():
    capability = make_capability(
        success=Checkpoint(conditions=[Condition(kind="text_present", value="Done")])
    )
    restored = Capability.from_dict(capability.to_dict())
    assert restored.success.conditions[0].value == "Done"

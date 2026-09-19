"""Replay engine behaviour, driven by a scripted surface.

These are the tests that matter for the brief's core claim: the same artifact
produces a *classified* result for every kind of runtime condition, not just a
pass/fail.
"""

import sys

import pytest
from conftest import FakeSurface, Screen

from cua.evidence import RunEvidence
from cua.models import (
    Capability,
    Checkpoint,
    Condition,
    ControlRef,
    Extraction,
    OutcomeRule,
    OutputSpec,
    ParamSpec,
    Step,
    TargetSpec,
)
from cua.policy import PolicyEngine
from cua.replay.engine import ReplayEngine
from cua.serde import SchemaError

BASE = "https://www.saucedemo.com"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def screens():
    return {
        "login": Screen(
            f"{BASE}/",
            "Swag Labs\nAccepted usernames are:",
            [("textbox", "Username"), ("textbox", "Password"), ("button", "Login")],
        ),
        "inventory": Screen(
            f"{BASE}/inventory.html",
            "Products\nSauce Labs Backpack $29.99",
            [("link", "Sauce Labs Backpack"), ("button", "Add to cart"), ("button", "Add to cart")],
        ),
        "cart": Screen(
            f"{BASE}/cart.html",
            "Your Cart\nSauce Labs Backpack",
            [("button", "Checkout")],
        ),
        "overview": Screen(
            f"{BASE}/checkout-step-two.html",
            "Checkout: Overview\nItem total: $29.99\nTax: $2.40\nTotal: $32.39",
            [("button", "Finish")],
        ),
        "locked": Screen(
            f"{BASE}/",
            "Epic sadface: Sorry, this user has been locked out.",
            [("textbox", "Username"), ("button", "Login")],
        ),
        "maintenance": Screen(
            f"{BASE}/",
            "System notice: scheduled maintenance tonight.",
            [("button", "Continue")],
        ),
    }


TRANSITIONS = {
    ("login", "click:Login"): "inventory",
    ("inventory", "click:Add to cart"): "cart",
    ("cart", "click:Checkout"): "overview",
    ("maintenance", "click:Continue"): "login",
}


def capability(**kw) -> Capability:
    base = dict(
        id="saucedemo.checkout_review",
        name="checkout review",
        description="add an item and reach the checkout review screen",
        target=TargetSpec(base_url=BASE, entry_path="/", app_id="saucedemo"),
        params=[
            ParamSpec(name="username", sensitive=True),
            ParamSpec(name="password", sensitive=True),
            ParamSpec(name="item_name", example="Sauce Labs Backpack"),
        ],
        outputs=[OutputSpec(name="total")],
        steps=[
            Step(id="s1-navigate", action="navigate", url_template="/"),
            Step(
                id="s2-type",
                action="type",
                target=ControlRef(role="textbox", name="Username"),
                value_template="{{username}}",
            ),
            Step(
                id="s3-type",
                action="type",
                target=ControlRef(role="textbox", name="Password"),
                value_template="{{password}}",
            ),
            Step(
                id="s4-click",
                action="click",
                target=ControlRef(role="button", name="Login"),
                checkpoint=Checkpoint(
                    conditions=[Condition(kind="url_matches", value="/inventory.html")]
                ),
            ),
            Step(
                id="s5-click",
                action="click",
                target=ControlRef(role="button", name="Add to cart", ordinal=0),
            ),
            Step(
                id="s6-click",
                action="click",
                target=ControlRef(role="button", name="Checkout"),
                extract=[
                    Extraction(
                        name="total",
                        method="regex",
                        # anchored: "Item total:" would otherwise match first,
                        # because extraction is case-insensitive and multiline
                        pattern=r"^Total:\s*\$([0-9.]+)",
                    )
                ],
            ),
        ],
        outcome_rules=[
            OutcomeRule(
                code="LOCKED_OUT",
                classification="business_outcome",
                when=Condition(kind="text_present", value="has been locked out"),
                message="the service account is locked out",
            ),
            OutcomeRule(
                code="MAINTENANCE_NOTICE",
                classification="recoverable",
                when=Condition(kind="text_present", value="scheduled maintenance"),
                recovery="dismiss_interstitial",
            ),
        ],
        success=Checkpoint(
            conditions=[Condition(kind="text_present", value="Checkout: Overview")]
        ),
    )
    base.update(kw)
    return Capability(**base)


def engine_for(tmp_path, start="login", **engine_kw):
    surface = FakeSurface(screens(), TRANSITIONS, start)
    evidence = RunEvidence(str(tmp_path), "test-run")
    policy = PolicyEngine(
        allowed_domains=["www.saucedemo.com"],
        allowed_path_globs=["*"],
        allowed_actions=["navigate", "click", "type", "read", "wait_for"],
        risky_control_patterns=[r"\bfinish\b"],
        risky_mode="require_approval",
    )
    return ReplayEngine(surface, policy, evidence, **engine_kw), surface


PARAMS = {"username": "standard_user", "password": "secret_sauce", "item_name": "Sauce Labs Backpack"}


# ---------------------------------------------------------------------------
# the four statuses
# ---------------------------------------------------------------------------


def test_happy_path_returns_success_and_declared_outputs(tmp_path):
    engine, surface = engine_for(tmp_path)
    result = engine.run(capability(), PARAMS)

    assert result.status == "success"
    assert result.outputs == {"total": "32.39"}
    assert result.failure is None
    assert [log.step_id for log in result.steps if log.status == "ok"] == [
        "s1-navigate", "s2-type", "s3-type", "s4-click", "s5-click", "s6-click"
    ]
    # parameters were substituted into the typed values
    typed = [a.text for a in surface.acted if a.kind == "type"]
    assert typed == ["standard_user", "secret_sauce"]


def test_known_business_outcome_is_not_a_failure(tmp_path):
    engine, _ = engine_for(tmp_path, start="locked")
    result = engine.run(capability(), PARAMS)

    assert result.status == "business_outcome"
    assert result.outcome.code == "LOCKED_OUT"
    assert result.outcome.classification == "business_outcome"
    assert result.failure is None  # the caller gets an answer, not a crash


def test_recoverable_condition_is_handled_and_the_run_continues(tmp_path):
    engine, _ = engine_for(tmp_path, start="maintenance")
    result = engine.run(capability(), PARAMS)

    assert result.status == "success"
    assert any(log.status == "recovered" for log in result.steps)


def test_recovery_is_bounded(tmp_path):
    """An interstitial that never clears must fail, not loop forever."""
    engine, surface = engine_for(tmp_path, start="maintenance")
    surface.transitions = {}  # Continue no longer dismisses it
    result = engine.run(capability(), PARAMS)

    assert result.status == "failed"
    assert "recovery" in (result.failure.expected + result.failure.observed).lower()


def test_checkpoint_failure_reports_step_expected_and_observed(tmp_path):
    engine, surface = engine_for(tmp_path)
    surface.transitions = {}  # clicking Login goes nowhere
    result = engine.run(capability(), PARAMS)

    assert result.status == "failed"
    assert result.failure.step_id == "s4-click"
    assert "/inventory.html" in result.failure.expected
    assert result.failure.observed  # what we actually saw, for debugging


def test_unresolvable_control_fails_with_context_not_a_stack_trace(tmp_path):
    broken = capability()
    broken.steps[3].target = ControlRef(role="button", name="Sign On")
    engine, _ = engine_for(tmp_path)
    result = engine.run(broken, PARAMS)

    assert result.status == "failed"
    assert result.failure.step_id == "s4-click"
    assert "Login" in result.failure.observed  # tells you what *was* on screen


# ---------------------------------------------------------------------------
# guardrails on the replay path
# ---------------------------------------------------------------------------


def test_policy_denial_stops_the_replay(tmp_path):
    engine, _ = engine_for(tmp_path)
    engine.policy.allowed_domains = ["example.test"]
    result = engine.run(capability(), PARAMS)

    assert result.status == "failed"
    assert "allowlist" in result.failure.observed


def test_risky_step_without_approval_escalates_rather_than_acting(tmp_path):
    risky = capability()
    risky.steps.append(
        Step(id="s7-click", action="click", target=ControlRef(role="button", name="Finish"))
    )
    engine, surface = engine_for(tmp_path)
    result = engine.run(risky, PARAMS)

    assert result.status == "escalated"
    assert result.outcome.code == "NEEDS_HUMAN"
    # and the risky control was never actually clicked
    assert all(log.step_id != "s7-click" or log.status != "ok" for log in result.steps)


def test_draft_capability_is_refused_for_unattended_replay(tmp_path):
    engine, _ = engine_for(tmp_path, require_approval=True)
    result = engine.run(capability(), PARAMS)

    assert result.status == "failed"
    assert "approval" in result.failure.expected


def test_missing_parameter_is_rejected_before_the_browser_moves(tmp_path):
    engine, surface = engine_for(tmp_path)
    with pytest.raises(SchemaError):
        engine.run(capability(), {"username": "u"})
    assert surface.acted == []


# ---------------------------------------------------------------------------
# the no-LLM guarantee
# ---------------------------------------------------------------------------


def test_replay_never_imports_a_model_client(tmp_path):
    for name in list(sys.modules):
        if name.startswith("anthropic"):
            del sys.modules[name]
    engine, _ = engine_for(tmp_path)
    engine.run(capability(), PARAMS)
    assert not any(name.startswith("anthropic") for name in sys.modules)

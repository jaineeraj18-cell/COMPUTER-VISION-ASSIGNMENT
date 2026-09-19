"""Control transfer, and compiling a discovery run into a capability."""

import threading
import time

from conftest import FakeSurface, Screen

from cua.agent.loop import DiscoveryRun, ExecutedAction
from cua.agent.compiler import compile_capability
from cua.escalation.broker import FileBroker, InterventionRequest, new_request_id
from cua.escalation.coordinator import EscalationCoordinator
from cua.evidence import RunEvidence
from cua.policy import PolicyEngine
from cua.surface.base import UIElement

# ---------------------------------------------------------------------------
# broker / control transfer
# ---------------------------------------------------------------------------


def a_request(run_id="run-1"):
    return InterventionRequest(
        id=new_request_id(),
        run_id=run_id,
        capability_id="demo.flow",
        goal="reach the review screen",
        step_id="s4-click",
        reason="two controls matched 'Continue'",
        url="https://example.test/step",
        state_summary="URL: ...",
    )


def test_request_carries_the_context_an_operator_needs(tmp_path):
    broker = FileBroker(str(tmp_path))
    request = a_request()
    broker.raise_request(request)

    (queued,) = broker.list_open()
    for key in ("capability_id", "goal", "step_id", "reason", "url", "state_summary"):
        assert queued[key]
    assert queued["holder"] == "human"  # control is ceded the moment we ask


def test_resolution_returns_control_to_automation(tmp_path):
    broker = FileBroker(str(tmp_path))
    request = a_request()
    broker.raise_request(request)

    def operator():
        time.sleep(0.2)
        broker.resolve(request.id, "resume", operator="alice", notes="fixed the branch code")

    threading.Thread(target=operator).start()
    resolution = broker.wait_for_resolution(request.id, timeout_s=5, poll_s=0.1)

    assert resolution is not None
    assert resolution.action == "resume"
    assert resolution.operator == "alice"
    assert broker.get(request.id)["holder"] == "automation"
    assert broker.list_open() == []


def test_timeout_leaves_control_with_the_human(tmp_path):
    broker = FileBroker(str(tmp_path))
    request = a_request()
    broker.raise_request(request)
    assert broker.wait_for_resolution(request.id, timeout_s=1, poll_s=0.2) is None
    assert broker.get(request.id)["holder"] == "human"


def test_coordinator_cedes_and_reclaims_the_same_session(tmp_path):
    broker = FileBroker(str(tmp_path / "queue"))
    evidence = RunEvidence(str(tmp_path / "evidence"), "run-x")
    surface = FakeSurface(
        {"only": Screen("https://example.test/", "stuck", [("button", "Continue")])},
        {},
        "only",
    )
    coordinator = EscalationCoordinator(broker, evidence, attended=True, timeout_s=5)

    def operator():
        time.sleep(0.3)
        open_requests = broker.list_open()
        while not open_requests:
            time.sleep(0.1)
            open_requests = broker.list_open()
        broker.resolve(open_requests[0]["id"], "resume", operator="bob")

    threading.Thread(target=operator).start()
    outcome = coordinator.escalate(
        surface=surface,
        observation=surface.observe(),
        capability_id="demo.flow",
        goal="g",
        step_id="s1",
        reason="stuck",
        run_id="run-x",
    )

    assert outcome.action == "resume"
    assert surface.ceded == 1  # the live session was handed over, not recreated
    assert outcome.human_actions  # and what the human did was captured


def test_unattended_mode_records_the_request_without_blocking(tmp_path):
    evidence = RunEvidence(str(tmp_path), "run-y")
    surface = FakeSurface(
        {"only": Screen("https://example.test/", "stuck", [])}, {}, "only"
    )
    coordinator = EscalationCoordinator(None, evidence, attended=False)
    outcome = coordinator.escalate(
        surface=surface,
        observation=surface.observe(),
        capability_id="c",
        goal="g",
        step_id="s1",
        reason="stuck",
        run_id="run-y",
    )
    assert outcome.action == "unattended"
    assert surface.ceded == 0


# ---------------------------------------------------------------------------
# compiler
# ---------------------------------------------------------------------------


def a_run() -> DiscoveryRun:
    run = DiscoveryRun(
        goal="add an item and reach the review screen",
        target_url="https://www.saucedemo.com/",
        params={"item_name": "Sauce Labs Backpack", "password": "secret_sauce"},
        model="test-model",
        run_id="disc-1",
        status="complete",
    )
    run.actions = [
        ExecutedAction(
            action="type",
            why="enter the password",
            url_before="https://www.saucedemo.com/",
            url_after="https://www.saucedemo.com/",
            element=UIElement(ref="e1", role="textbox", name="Password"),
            value="secret_sauce",
        ),
        ExecutedAction(
            action="click",
            why="open the item",
            url_before="https://www.saucedemo.com/inventory.html",
            url_after="https://www.saucedemo.com/inventory-item.html?id=4",
            element=UIElement(
                ref="e2",
                role="link",
                name="Sauce Labs Backpack",
                anchor_text="Sauce Labs Backpack $29.99",
                css_hint="a:nth-of-type(2)",
            ),
        ),
    ]
    run.actions[-1].extractions = [{"name": "total", "pattern": r"Total: \$([0-9.]+)"}]
    run.finish = {
        "summary": "reaches the checkout review screen",
        "success_text": "Checkout: Overview",
        "outcome_rules": [
            {
                "code": "locked out",
                "classification": "business_outcome",
                "text": "has been locked out",
            },
            {"code": "BAD", "classification": "recoverable", "text": "oops"},  # no handler
        ],
    }
    return run


def compiled():
    return compile_capability(
        a_run(),
        PolicyEngine(risky_control_patterns=[r"\bfinish\b"], allowed_actions=["click"]),
        capability_id="saucedemo.checkout_review",
        app_id="saucedemo",
        sensitive_params=["password"],
    )


def test_concrete_values_are_lifted_into_parameters():
    capability = compiled()
    step = next(s for s in capability.steps if s.action == "click")
    assert step.target.name == "{{item_name}}"
    assert "item_name" in capability.placeholders()


def test_secrets_never_reach_the_artifact():
    raw = str(compiled().to_dict())
    assert "secret_sauce" not in raw
    password = next(p for p in compiled().params if p.name == "password")
    assert password.sensitive and password.example is None


def test_urls_are_stored_relative_to_the_tenant_host():
    capability = compiled()
    assert capability.target.base_url == "https://www.saucedemo.com"
    assert capability.steps[0].url_template == "/"
    assert "saucedemo.com" not in str(
        [s.checkpoint for s in capability.steps if s.checkpoint]
    )


def test_observed_navigation_becomes_a_checkpoint():
    capability = compiled()
    step = next(s for s in capability.steps if s.action == "click")
    assert step.checkpoint is not None
    assert "/inventory-item" in step.checkpoint.conditions[0].value


def test_model_proposed_rules_are_normalised_and_typechecked():
    capability = compiled()
    codes = [rule.code for rule in capability.outcome_rules]
    assert "LOCKED_OUT" in codes  # normalised
    assert "BAD" not in codes  # recoverable without a handler: dropped
    assert "SESSION_EXPIRED" in codes  # built-in library merged in


def test_success_condition_comes_from_the_model_and_is_asserted():
    capability = compiled()
    assert capability.success.conditions[0].value == "Checkout: Overview"
    assert capability.approval == "draft"  # nothing is trusted unattended yet

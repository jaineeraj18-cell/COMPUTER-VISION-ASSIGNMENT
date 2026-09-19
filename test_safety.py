import os
import tempfile

from cua.policy import PolicyEngine, ProposedAction
from cua.redaction import Redactor

POLICY_YAML = """
allowed_domains: ["www.saucedemo.com", "127.0.0.1"]
allowed_path_globs: ["/", "/inventory.html", "/cart.html"]
denied_path_globs: ["/admin*"]
allowed_actions: [navigate, click, type, read]
risky_control_patterns: ["\\\\bfinish\\\\b", "\\\\bdelete\\\\b"]
risky_mode: require_approval
max_steps: 25
"""


def policy() -> PolicyEngine:
    handle, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(handle, "w") as fh:
        fh.write(POLICY_YAML)
    return PolicyEngine.load(path)


# -- allowlist --------------------------------------------------------------


def test_offlist_domain_is_denied():
    decision = policy().check_url("https://evil.test/inventory.html")
    assert not decision.allowed
    assert "allowlist" in decision.reason


def test_offlist_path_is_denied_even_on_an_allowed_host():
    assert not policy().check_url("https://www.saucedemo.com/secret.html").allowed


def test_denied_path_beats_a_broad_allow():
    engine = policy()
    engine.allowed_path_globs = ["*"]
    assert not engine.check_url("https://www.saucedemo.com/admin/users").allowed


def test_non_http_schemes_are_denied():
    assert not policy().check_url("file:///etc/passwd").allowed


def test_action_type_allowlist():
    decision = policy().check(
        ProposedAction(kind="select", url="https://www.saucedemo.com/")
    )
    assert not decision.allowed


# -- risk -------------------------------------------------------------------


def test_risky_control_requires_approval():
    decision = policy().check(
        ProposedAction(
            kind="click", url="https://www.saucedemo.com/cart.html", control_name="Finish"
        )
    )
    assert decision.verdict == "require_approval"


def test_safe_control_is_allowed():
    decision = policy().check(
        ProposedAction(
            kind="click",
            url="https://www.saucedemo.com/inventory.html",
            control_name="Add to cart",
        )
    )
    assert decision.allowed


def test_risky_mode_block():
    engine = policy()
    engine.risky_mode = "block"
    decision = engine.check(
        ProposedAction(kind="click", url="https://www.saucedemo.com/", control_name="Delete")
    )
    assert decision.verdict == "deny"


# -- redaction --------------------------------------------------------------


def test_patterns_are_scrubbed():
    redactor = Redactor()
    text = redactor.scrub("SSN 123-45-6789 card 4111 1111 1111 1111 me@bank.test")
    assert "123-45-6789" not in text
    assert "4111" not in text
    assert "me@bank.test" not in text


def test_registered_values_are_scrubbed_verbatim():
    redactor = Redactor(["hunter2-swordfish"])
    assert "hunter2-swordfish" not in redactor.scrub("login with hunter2-swordfish")


def test_secret_keys_are_masked_by_name():
    redactor = Redactor()
    scrubbed = redactor.scrub_obj({"password": "abc", "note": "fine", "api_key": "xyz"})
    assert scrubbed["password"].startswith("[REDACTED")
    assert scrubbed["api_key"].startswith("[REDACTED")
    assert scrubbed["note"] == "fine"


def test_nested_structures_are_scrubbed():
    redactor = Redactor(["topsecret"])
    scrubbed = redactor.scrub_obj({"steps": [{"value": "topsecret", "n": 1}]})
    assert scrubbed["steps"][0]["value"].startswith("[REDACTED")
    assert scrubbed["steps"][0]["n"] == 1


def test_screenshots_are_evidence_only():
    assert Redactor.allow_screenshot("failure", True)
    assert not Redactor.allow_screenshot("every_step", True)
    assert not Redactor.allow_screenshot("failure", False)

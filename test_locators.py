from conftest import Screen

from cua.models import ControlRef
from cua.replay import locators


def screen():
    return Screen(
        url="https://example.test/inventory",
        text="Backpack 29.99\nBike Light 9.99",
        controls=[
            ("button", "Add to cart"),
            ("button", "Add to cart"),
            ("link", "Sauce Labs Backpack"),
            ("textbox", ""),
        ],
    ).observation()


def test_unique_role_name_resolves_with_full_confidence():
    resolution = locators.resolve(
        ControlRef(role="link", name="Sauce Labs Backpack"), screen()
    )
    assert resolution.found
    assert resolution.strategy == "role_name"
    assert resolution.confidence == 1.0


def test_ambiguous_match_is_disambiguated_by_ordinal():
    resolution = locators.resolve(
        ControlRef(role="button", name="Add to cart", ordinal=1), screen()
    )
    assert resolution.found
    assert resolution.element.ref == "e1"
    assert resolution.confidence < 1.0


def test_ambiguous_match_without_a_usable_ordinal_refuses():
    """We would rather stop than click a plausible-looking wrong control."""
    resolution = locators.resolve(
        ControlRef(role="button", name="Add to cart", ordinal=7), screen()
    )
    assert not resolution.found
    assert "ambiguous" in resolution.detail


def test_falls_back_to_anchor_then_css_in_declared_order():
    observation = screen()
    ref = ControlRef(
        role="link",
        name="Renamed Since Recording",
        anchor_text="row for Sauce Labs Backpack",
        css_hint="link:nth-of-type(3)",
    )
    resolution = locators.resolve(ref, observation)
    assert resolution.found
    assert resolution.strategy == "anchor"

    ref.anchor_text = None
    resolution = locators.resolve(ref, observation)
    assert resolution.found
    assert resolution.strategy == "css"


def test_disabled_controls_never_match_an_action():
    observation = screen()
    observation.elements[0].enabled = False
    resolution = locators.resolve(
        ControlRef(role="button", name="Add to cart", ordinal=0), observation, "click"
    )
    # the only remaining candidate is ordinal 1, so ordinal 0 must not resolve
    assert not resolution.found or resolution.element.ref != "e0"


def test_name_match_modes():
    observation = screen()
    contains = ControlRef(role="link", name="backpack", name_match="contains")
    assert locators.resolve(contains, observation).found

    regex = ControlRef(role="link", name=r"^Sauce.*Backpack$", name_match="regex")
    assert locators.resolve(regex, observation).found


def test_missing_control_reports_what_was_there():
    resolution = locators.resolve(ControlRef(role="button", name="Ghost"), screen())
    assert not resolution.found
    assert "Add to cart" in locators.describe_near_misses(
        ControlRef(role="button", name="Ghost"), screen()
    )

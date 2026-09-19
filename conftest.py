import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from cua.surface.base import Observation, Surface, SurfaceAction, UIElement  # noqa: E402


class Screen:
    """A scripted screen: url, text, and controls."""

    def __init__(self, url: str, text: str, controls: List[Tuple[str, str]], title: str = ""):
        self.url = url
        self.text = text
        self.controls = controls
        self.title = title

    def observation(self) -> Observation:
        elements = []
        counts: Dict[str, int] = {}
        for index, (role, name) in enumerate(self.controls):
            key = f"{role}|{name}"
            counts[key] = counts.get(key, -1) + 1
            elements.append(
                UIElement(
                    ref=f"e{index}",
                    role=role,
                    name=name,
                    ordinal=counts[key],
                    anchor_text=f"row for {name}",
                    css_hint=f"{role}:nth-of-type({index + 1})",
                )
            )
        return Observation(url=self.url, title=self.title, elements=elements, text=self.text)


class FakeSurface(Surface):
    """Deterministic stand-in for a browser, for engine tests."""

    name = "fake"

    def __init__(self, screens: Dict[str, Screen], transitions: Dict[Tuple[str, str], str], start: str):
        self.screens = screens
        self.transitions = transitions
        self.current = start
        self.acted: List[SurfaceAction] = []
        self.ceded = 0
        self.page = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def observe(self, screenshot_path: Optional[str] = None) -> Observation:
        return self.screens[self.current].observation()

    def act(self, action: SurfaceAction, observation: Observation) -> None:
        self.acted.append(action)
        key_name = ""
        if action.ref:
            element = observation.find_ref(action.ref)
            key_name = element.name if element else ""
        key = (self.current, f"{action.kind}:{key_name or action.url or action.text or ''}")
        if key in self.transitions:
            self.current = self.transitions[key]

    def cede_control(self) -> None:
        self.ceded += 1

    def resume_control(self):
        return [{"kind": "click", "name": "manual"}]

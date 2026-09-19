"""Desktop surface — deliberately stubbed, but at a real seam.

This file exists to make one claim checkable: nothing above the `Surface`
interface is web-specific. A Windows back-office client would be driven through
UI Automation (pywinauto / uiautomation), which exposes exactly the vocabulary
`UIElement` already uses:

    UIA ControlType.Button   -> role="button"
    UIA .Name                -> name
    UIA .Value               -> value
    UIA .IsEnabled           -> enabled
    UIA runtime id           -> ref (per-observation handle)
    parent window title      -> frame

`ControlRef.css_hint` is the only web-flavoured field in the schema, and it is
optional and last in every strategy chain. The desktop equivalent (automation
id) would slot into the same position.

What is *not* solved here and would need real work: no URL means the allowlist
becomes "which executables/windows may we drive", `navigate` becomes "focus
window / open menu path", and checkpoints lean on window titles and control
presence rather than text digests. See REPORT.md section 4.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .base import Observation, Surface, SurfaceAction


class DesktopSurface(Surface):
    name = "desktop"

    def __init__(self, app_path: str) -> None:
        self.app_path = app_path

    def start(self) -> None:
        raise NotImplementedError(
            "DesktopSurface is a documented stub. Implement with pywinauto: "
            "Application(backend='uia').start(self.app_path)"
        )

    def stop(self) -> None:  # pragma: no cover - stub
        raise NotImplementedError

    def observe(self, screenshot_path: Optional[str] = None) -> Observation:  # pragma: no cover
        # walk window.descendants(), map ControlType -> role, .Name -> name,
        # window title -> frame, runtime id -> ref.
        raise NotImplementedError

    def act(self, action: SurfaceAction, observation: Observation) -> None:  # pragma: no cover
        raise NotImplementedError

    def cede_control(self) -> None:  # pragma: no cover
        # Already trivial on a desktop session: stop sending input, tell the
        # operator which RDP/VNC session to attach to.
        raise NotImplementedError

    def resume_control(self) -> List[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

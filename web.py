"""Web surface, implemented on Playwright's *sync* API.

Why Playwright: it gives us a real browser, frame handling, and robust waiting.
Why not Playwright *selectors* in the artifact: a CSS/XPath selector recorded
today is the thing that breaks next quarter, and our target apps have no test
IDs to anchor on. So Playwright is used only as an actuator — we perceive
through `harvest.js` (role + accessible name + legacy label hints) and we
resolve controls ourselves in `replay/locators.py`.

Why not a pure screenshot+coordinates CUA loop: coordinates are the least stable
thing on the page, and a bank's back-office screens are dense text. The
role/name vocabulary we use here is the same one a desktop UIA/AX driver
exposes, so the artifact stays portable; screenshots are kept as *evidence*,
not as the control channel.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from .base import Observation, Surface, SurfaceAction, SurfaceError, UIElement

_HARVEST = os.path.join(os.path.dirname(__file__), "harvest.js")

_RECORDER = """
() => {
  if (window.__cuaRecorderInstalled) return;
  window.__cuaRecorderInstalled = true;
  window.__cuaHumanActions = [];
  const note = (kind, ev) => {
    const el = ev.target;
    if (!el || !el.tagName) return;
    window.__cuaHumanActions.push({
      kind,
      at: new Date().toISOString(),
      tag: el.tagName.toLowerCase(),
      name: (el.innerText || el.value || el.getAttribute('name') || '').slice(0, 80),
      url: location.href
    });
  };
  document.addEventListener('click', (e) => note('click', e), true);
  document.addEventListener('change', (e) => note('change', e), true);
}
"""

_BANNER = """
(on) => {
  const id = '__cua_banner';
  const existing = document.getElementById(id);
  if (!on) { if (existing) existing.remove(); return; }
  if (existing) return;
  const bar = document.createElement('div');
  bar.id = id;
  bar.textContent = 'HUMAN IN CONTROL - automation paused. Resume from the operator console.';
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
    'background:#b00020;color:#fff;font:600 13px system-ui;padding:8px 12px;text-align:center';
  document.documentElement.appendChild(bar);
}
"""


class WebSurface(Surface):
    name = "web"

    def __init__(self, headless: bool = True, slow_mo_ms: int = 0, viewport=(1280, 900)) -> None:
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.viewport = viewport
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None
        self._frames: Dict[str, str] = {}  # ref prefix -> frame url
        self._dialogs: List[str] = []

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, slow_mo=self.slow_mo_ms
        )
        self._context = self._browser.new_context(
            viewport={"width": self.viewport[0], "height": self.viewport[1]}
        )
        self.page = self._context.new_page()
        # We record dialogs and dismiss them (cancel), never accept: accepting a
        # confirm may be the irreversible action itself. Recovery handlers and
        # outcome rules decide what to do about it.
        self.page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog) -> None:
        self._dialogs.append(f"{dialog.type}: {dialog.message}")
        try:
            dialog.dismiss()
        except Exception:  # pragma: no cover - dialog already gone
            pass

    def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # pragma: no cover
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:  # pragma: no cover
            pass

    # -- perception -------------------------------------------------------
    def observe(self, screenshot_path: Optional[str] = None) -> Observation:
        self._settle()
        with open(_HARVEST, "r", encoding="utf-8") as fh:
            script = fh.read()

        elements: List[UIElement] = []
        texts: List[str] = []
        self._frames = {}
        for index, frame in enumerate(self.page.frames):
            prefix = f"e{index}_"
            try:
                payload: Dict[str, Any] = frame.evaluate(script, prefix)
            except Exception:
                continue  # frame detached or cross-origin; skip, don't crash
            self._frames[prefix] = frame.url
            for raw in payload.get("elements", []):
                elements.append(
                    UIElement(
                        ref=raw["ref"],
                        role=raw["role"],
                        name=raw.get("name") or "",
                        value=raw.get("value"),
                        enabled=bool(raw.get("enabled", True)),
                        visible=True,
                        frame=frame.url,
                        ordinal=int(raw.get("ordinal", 0)),
                        anchor_text=raw.get("anchor_text"),
                        css_hint=raw.get("css_hint"),
                    )
                )
            if payload.get("text"):
                texts.append(payload["text"])

        shot = None
        if screenshot_path:
            try:
                self.page.screenshot(path=screenshot_path, full_page=False)
                shot = screenshot_path
            except Exception:  # pragma: no cover
                shot = None

        dialogs, self._dialogs = self._dialogs, []
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            elements=elements,
            text="\n".join(texts),
            dialogs=dialogs,
            screenshot_path=shot,
        )

    def dom_snapshot(self, path: str) -> Optional[str]:
        try:
            html = self.page.content()
        except Exception:  # pragma: no cover
            return None
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return path

    # -- action -----------------------------------------------------------
    def act(self, action: SurfaceAction, observation: Observation) -> None:
        kind = action.kind
        if kind == "navigate":
            self.page.goto(action.url, timeout=action.timeout_ms, wait_until="domcontentloaded")
            return
        if kind == "wait_for":
            self._wait_for_text(action.text or "", action.timeout_ms)
            return
        if kind == "read":
            return

        locator = self._locator(action.ref, observation, action.timeout_ms)
        if kind == "click":
            locator.click(timeout=action.timeout_ms)
        elif kind == "type":
            locator.fill("", timeout=action.timeout_ms)
            locator.type(action.text or "", delay=15, timeout=action.timeout_ms)
        elif kind == "select":
            locator.select_option(label=action.text, timeout=action.timeout_ms)
        elif kind == "press_key":
            locator.press(action.key or "Enter", timeout=action.timeout_ms)
        else:
            raise SurfaceError(f"unsupported action kind {kind!r}")
        self._settle()

    def _locator(self, ref: Optional[str], observation: Observation, timeout_ms: int):
        if not ref:
            raise SurfaceError("action requires an element ref")
        element = observation.find_ref(ref)
        frame_url = element.frame if element else None
        frame = self._frame_for(frame_url)
        locator = frame.locator(f'[data-cua-ref="{ref}"]')
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
        except Exception as exc:
            raise SurfaceError(f"control {ref} vanished before the action ran") from exc
        return locator

    def _frame_for(self, frame_url: Optional[str]):
        if frame_url:
            for frame in self.page.frames:
                if frame.url == frame_url:
                    return frame
        return self.page.main_frame

    # -- waiting ----------------------------------------------------------
    def _settle(self, timeout_ms: int = 8000) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass  # networkidle is a nicety; long-polling apps never reach it

    def _wait_for_text(self, text: str, timeout_ms: int) -> None:
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                if text.lower() in (self.page.inner_text("body") or "").lower():
                    return
            except Exception:
                pass
            self.page.wait_for_timeout(250)
        raise SurfaceError(f"timed out waiting for text {text!r}")

    # -- handoff ----------------------------------------------------------
    def cede_control(self) -> None:
        """Hand the *same* live session to a human.

        Local dev: the human drives the headed browser window directly. In
        production this is the point where we would publish the CDP endpoint (or
        a VNC view of the container) to the operator console - the session and
        its cookies never move, which is the whole point.
        """
        if self.headless:
            raise SurfaceError(
                "cannot cede control of a headless session; run with CUA_HEADLESS=0"
            )
        try:
            self.page.evaluate(_RECORDER)
            self.page.evaluate(_BANNER, True)
            self.page.bring_to_front()
        except Exception as exc:  # pragma: no cover
            raise SurfaceError(f"failed to expose session to operator: {exc}") from exc

    def resume_control(self) -> List[Dict[str, Any]]:
        try:
            actions = self.page.evaluate("() => window.__cuaHumanActions || []")
            self.page.evaluate(_BANNER, False)
        except Exception:  # pragma: no cover
            actions = []
        return list(actions or [])

    def cdp_endpoint(self) -> Optional[str]:
        """Where a remote operator console would attach. Mocked locally."""
        try:
            return self._browser.ws_endpoint  # type: ignore[attr-defined]
        except Exception:
            return None

"""Prompting for the discovery run.

The prompt is written for a *recording* run, not a chat: the model is told that
everything it does will be compiled into a deterministic script that runs
without it, which measurably changes its behaviour — it stops taking exploratory
detours and it names things (success conditions, error states) explicitly.
"""

from __future__ import annotations

from typing import Any, Dict, List

SYSTEM = """You are operating a back-office business application through a \
computer-use interface, the way a trained human operator would.

Each turn you are given the current screen as:
  URL, TITLE, VISIBLE TEXT, and CONTROLS (a list of [ref] role 'name' entries).
You act by calling exactly one tool, referring to controls by their [ref].

Rules that matter:
1. This run is being RECORDED and compiled into a deterministic script that will
   later run without you. Take the shortest correct path. Do not explore, do not
   take detours "to check", and do not do anything the goal did not ask for.
2. Refs are only valid for the screen you were just shown. Never reuse an old
   ref.
3. Act only on what is visible in CONTROLS. If what you need is not there, the
   previous action probably did not do what you expected: read VISIBLE TEXT
   again before acting.
4. You are inside a policy allowlist. If a tool call is refused, do not try to
   route around it - either take a permitted path or call escalate.
5. Never invent data. Use exactly the parameter values you are given.
6. If you are stuck, ambiguous about which control is correct, or the screen
   asks for a decision that should belong to a person, call escalate. Escalating
   is a correct outcome, guessing is not.
7. When the goal is reached, call finish. In finish you must name:
   - success_text: text on the final screen that is NOT present on earlier
     screens of this flow (this becomes the replay checkpoint), and
   - outcome_rules: the runtime conditions a future replay of this same flow
     could plausibly hit (validation errors, "not found", permission denials,
     session expiry, interstitials), each with the exact on-screen text that
     identifies it and whether it is a business_outcome (a legitimate answer the
     caller needs), recoverable (dismiss/retry and continue), or a hard_failure.
"""


def build_goal_message(goal: str, params: Dict[str, Any], target: str) -> str:
    lines = [
        f"GOAL: {goal}",
        f"TARGET: {target}",
        "",
        "PARAMETERS for this run (these will become the capability's typed "
        "inputs, so use them verbatim):",
    ]
    for key, value in params.items():
        lines.append(f"  {key} = {value!r}")
    lines.append("")
    lines.append("Begin. The first screen follows.")
    return "\n".join(lines)


def summarise_old_observation(text: str) -> str:
    """Older screens are collapsed to one line to keep the context small."""
    first = (text or "").strip().splitlines()
    url = next((line for line in first if line.startswith("URL:")), "URL: ?")
    return f"[earlier screen elided] {url}"


def format_tool_result(observation_text: str, note: str = "") -> List[Dict[str, Any]]:
    body = observation_text if not note else f"{note}\n\n{observation_text}"
    return [{"type": "text", "text": body}]

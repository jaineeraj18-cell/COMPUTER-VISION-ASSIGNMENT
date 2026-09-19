"""The tool surface the discovery model is allowed to use.

Note what is *not* here: no "run javascript", no "set cookie", no free-form
selector. The model can only do things a human operator at a terminal could do,
which is what keeps the recorded artifact replayable and the blast radius
bounded. Every call is checked against the policy engine before it executes.
"""

from __future__ import annotations

from typing import Any, Dict, List

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click a control listed in CONTROLS by its [ref].",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "the [ref] of the control"},
                "why": {"type": "string", "description": "one sentence: why this control"},
            },
            "required": ["ref", "why"],
        },
    },
    {
        "name": "type_text",
        "description": "Type into a textbox. Clears the field first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["ref", "text", "why"],
        },
    },
    {
        "name": "select_option",
        "description": "Pick an option in a combobox by its visible label.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "label": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["ref", "label", "why"],
        },
    },
    {
        "name": "navigate",
        "description": "Go to an absolute URL. Must be inside the allowlist.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "why": {"type": "string"}},
            "required": ["url", "why"],
        },
    },
    {
        "name": "press_key",
        "description": "Press a key (e.g. Enter) while a control is focused.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "key": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["ref", "key", "why"],
        },
    },
    {
        "name": "extract",
        "description": (
            "Declare a value on the current screen as an output of this "
            "capability. Use a control ref when the value is a control's text, "
            "or a regex over the visible text otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "output name, snake_case"},
                "ref": {"type": "string"},
                "pattern": {"type": "string", "description": "regex with one group"},
                "description": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Hand the live session to a human operator. Use when you are stuck, "
            "when the screen needs a decision you should not make, or when you "
            "would otherwise guess."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "finish",
        "description": (
            "The goal is achieved. Declare how a replay should verify it, and "
            "which runtime conditions this flow should know how to recognise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "success_text": {
                    "type": "string",
                    "description": "text that is present on the final screen and nowhere earlier",
                },
                "success_url_pattern": {
                    "type": "string",
                    "description": "regex the final URL matches",
                },
                "outcome_rules": {
                    "type": "array",
                    "description": (
                        "runtime conditions a replay of this flow could hit: "
                        "validation errors, not-found results, permission "
                        "denials, session timeouts, interstitials"
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "description": "SCREAMING_SNAKE"},
                            "classification": {
                                "type": "string",
                                "enum": ["business_outcome", "recoverable", "hard_failure"],
                            },
                            "text": {
                                "type": "string",
                                "description": "exact on-screen text that identifies it",
                            },
                            "message": {"type": "string"},
                            "recovery": {
                                "type": "string",
                                "enum": ["dismiss_interstitial", "retry_wait", "reload"],
                                "description": "required when classification is recoverable",
                            },
                        },
                        "required": ["code", "classification", "text"],
                    },
                },
            },
            "required": ["summary", "success_text"],
        },
    },
]

TOOL_NAMES = [tool["name"] for tool in TOOLS]

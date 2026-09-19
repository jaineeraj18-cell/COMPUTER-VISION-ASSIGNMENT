"""Evidence: one directory per run, structured JSONL inside it.

Everything written here goes through the run's `Redactor`. A run directory is
meant to be handed to a reviewer as-is:

    evidence/<run_id>/
        run.jsonl          every decision, in order, with why
        observations/      text digests of what the agent saw
        screenshots/       failure + escalation frames only
        result.json        the replay result contract
        intervention.json  present only if a human was pulled in
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from .redaction import Redactor


class RunEvidence:
    def __init__(self, root: str, run_id: str, redactor: Optional[Redactor] = None) -> None:
        self.run_id = run_id
        self.dir = os.path.join(root, run_id)
        self.redactor = redactor or Redactor()
        os.makedirs(os.path.join(self.dir, "screenshots"), exist_ok=True)
        os.makedirs(os.path.join(self.dir, "observations"), exist_ok=True)
        self._log_path = os.path.join(self.dir, "run.jsonl")
        self._seq = 0

    # -- logging ----------------------------------------------------------
    def log(self, event: str, **fields: Any) -> None:
        self._seq += 1
        record: Dict[str, Any] = {
            "seq": self._seq,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": self.run_id,
            "event": event,
        }
        record.update(self.redactor.scrub_obj(fields))
        with open(self._log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"  [{event}] " + _one_line(record), flush=True)

    # -- artefacts --------------------------------------------------------
    def write_json(self, name: str, payload: Any) -> str:
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.redactor.scrub_obj(payload), fh, indent=2, ensure_ascii=False)
        return path

    def write_text(self, name: str, text: str) -> str:
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.redactor.scrub(text))
        return path

    def screenshot_path(self, label: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
        return os.path.join(self.dir, "screenshots", f"{self._seq:03d}-{safe}.png")

    def observation_path(self, label: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)
        return os.path.join(self.dir, "observations", f"{self._seq:03d}-{safe}.txt")


def _one_line(record: Dict[str, Any]) -> str:
    skip = {"seq", "ts", "run_id", "event"}
    bits = []
    for key, value in record.items():
        if key in skip:
            continue
        text = str(value)
        if len(text) > 110:
            text = text[:107] + "..."
        bits.append(f"{key}={text}")
    return " ".join(bits)

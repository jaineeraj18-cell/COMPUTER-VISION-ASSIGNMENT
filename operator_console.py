"""Mock operator console.

Deliberately mocked (the brief says a real co-browsing console is out of scope).
What is *real* here: the queue it reads, the context each request carries, the
control token it flips, and the resume signal the paused run is blocking on.

What is mocked: the viewport. Locally the operator drives the same headed
Chromium window the automation is using — same process, same cookies, same
half-filled form. In production the browser runs in a container and this page
would embed a VNC/CDP view of it; the state machine below does not change.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from flask import Flask, redirect, request

from .broker import FileBroker

app = Flask(__name__)
BROKER = FileBroker(os.environ.get("CUA_INTERVENTIONS", "interventions"))

_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Operator console</title>
<style>
 body{{font:14px/1.5 system-ui;margin:2rem;max-width:60rem;color:#111}}
 .card{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}}
 .why{{background:#fff4f4;border-left:3px solid #b00020;padding:.5rem .75rem}}
 pre{{background:#f6f6f6;padding:.75rem;overflow:auto;max-height:20rem;font-size:12px}}
 button{{font:600 13px system-ui;padding:.5rem .9rem;margin-right:.5rem;cursor:pointer}}
 .muted{{color:#666}}
</style>
<h1>Operator console <span class="muted">(mock)</span></h1>
{body}
"""


@app.get("/")
def index() -> str:
    requests_open = BROKER.list_open()
    if not requests_open:
        return _PAGE.format(
            body='<p class="muted">No open intervention requests. '
            "Start a run that gets stuck and refresh.</p>"
        )
    cards = "".join(_card(item) for item in requests_open)
    return _PAGE.format(body=cards)


def _card(item: Dict[str, Any]) -> str:
    return f"""
<div class="card">
  <h2>{item['capability_id']} &middot; step {item['step_id']}</h2>
  <p class="why"><strong>Why it stopped:</strong> {item['reason']}</p>
  <p><strong>Goal:</strong> {item['goal']}<br>
     <strong>URL:</strong> {item['url']}<br>
     <strong>Run:</strong> {item['run_id']} &middot;
     <strong>Control held by:</strong> {item.get('holder')}</p>
  <p class="muted">The automation is paused on the live session. Do the manual
     steps in that browser window, then hand control back.</p>
  <details><summary>State at the moment it stopped</summary>
    <pre>{item['state_summary']}</pre></details>
  <form method="post" action="/resolve/{item['id']}">
    <input type="hidden" name="operator" value="operator@example.com">
    <input name="notes" placeholder="what you did" size="50">
    <button name="action" value="resume">Hand control back &amp; resume</button>
    <button name="action" value="skip_step">Skip this step</button>
    <button name="action" value="abort">Abort run</button>
  </form>
</div>"""


@app.post("/resolve/<request_id>")
def resolve(request_id: str):
    action = request.form.get("action", "abort")
    BROKER.resolve(
        request_id,
        action=action,
        operator=request.form.get("operator", "operator"),
        notes=request.form.get("notes", ""),
    )
    return redirect("/")


def main() -> None:
    port = int(os.environ.get("CUA_OPERATOR_PORT", "8765"))
    print(f"Operator console on http://127.0.0.1:{port}")
    app.run(port=port, debug=False)


if __name__ == "__main__":
    main()

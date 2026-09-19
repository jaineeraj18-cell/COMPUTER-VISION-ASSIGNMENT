# Computer-use capability recorder + deterministic replayer

An LLM drives a real UI once to work out *how* a task is done. That run is
compiled into a typed, versioned **capability artifact**. From then on the
capability is replayed deterministically, with no model in the decision loop,
and an AI agent invokes it by name with typed arguments.

```
goal + target ──► discovery run (LLM)  ──► capability artifact (capability/v1)
                        │                          │
                   evidence/                       ▼
                                      deterministic replay ──► success
                                                              │ business outcome
                                                              │ escalated (human)
                                                              └ failure + evidence
```

The design write-up, including trade-offs and what was deliberately cut, is in
[REPORT.md](REPORT.md). The artifact contract is in
[schema/capability.schema.json](schema/capability.schema.json).

---

## Setup

Requires Python 3.10+ and an Anthropic API key (discovery runs only — replay
needs no key).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

cp .env.example .env     # add ANTHROPIC_API_KEY; the rest has working defaults
export PYTHONPATH=src
```

`.env` is gitignored and is read automatically by the CLI. No secret is ever
passed on the command line: `--secret password=SAUCE_PASSWORD` names an
environment variable, and the value is registered with the redactor before
anything is written to disk.

**Target application.** The public demo site `https://www.saucedemo.com` — a
site published specifically for automation practice, with published demo
credentials and no real PII. It gives the shape the brief asks for
(sign-in → list → detail action → multi-field form → confirmation screen) plus
built-in failure accounts (`locked_out_user`) to exercise the error paths. No
real bank system is touched, and none should be.

## Demo path

### 1. Discovery — one real LLM-driven run

```bash
python -m cua.cli discover \
  --url https://www.saucedemo.com/ \
  --id saucedemo.checkout_review --app saucedemo \
  --goal "Sign in, add the product named item_name to the cart, open the cart, start checkout, fill the checkout information form with first_name, last_name and postal_code, and stop on the checkout overview screen. Do not place the order." \
  --secret username=SAUCE_USERNAME --secret password=SAUCE_PASSWORD \
  --param item_name="Sauce Labs Backpack" \
  --param first_name=Alex --param last_name=Rivera --param postal_code=37402
```

Writes `artifacts/saucedemo.checkout_review.v1.json` and
`evidence/discovery-<timestamp>/` (structured log, per-step screen digests,
screenshots, the compiled capability).

`make discover` runs exactly this.

### 2. Replay — no model, different inputs

```bash
python -m cua.cli replay --capability saucedemo.checkout_review \
  --secret username=SAUCE_USERNAME --secret password=SAUCE_PASSWORD \
  --param item_name="Sauce Labs Bike Light" \
  --param first_name=Alex --param last_name=Rivera --param postal_code=37402
```

Note the *different item* — the recorded control name was lifted to
`{{item_name}}`, so this is a parameterised capability, not a macro. Prints the
caller-facing result:

```json
{
  "capability": "saucedemo.checkout_review",
  "version": 1,
  "status": "success",
  "outputs": { "item_total": "9.99", "tax": "0.80", "total": "10.79" },
  "duration_ms": 6142
}
```

### 3. Replay that hits an exceptional state

```bash
make replay-error      # signs in as locked_out_user
```

Returns `status: "business_outcome"` with code `LOCKED_OUT` — an answer for the
caller, not a crash, and no failure object. Two more error paths worth running:

| What | How | Expected |
|---|---|---|
| Validation error | `--param postal_code=""` | `business_outcome` if the discovery model recorded the rule, else a checkpoint failure naming the step |
| Item that does not exist | `--param item_name="Nonexistent Item"` | control cannot be resolved → escalation, *not* a guessed click (see REPORT §3) |

### 4. Human takes control of the live session

Three terminals:

```bash
make operator                       # 1: console on http://127.0.0.1:8765
CUA_HEADLESS=0 python -m cua.cli replay --capability saucedemo.checkout_review \
  --attended --secret ... --param ...   # 2: run that will get stuck
```

When the run hits something it cannot safely do, it raises an intervention
request carrying the capability, the step, why it stopped and the screen state;
the browser window gets a red "HUMAN IN CONTROL" banner; the operator does the
manual steps **in that same window** and clicks *Hand control back & resume*.
What the human did is captured to `evidence/<run>/human_actions.json`.

To force it for a demo, add a risky step or point a step at a control that no
longer exists.

### 5. Agent-facing catalog (stretch goal)

```bash
python -m cua.cli catalog list        # tool schemas an AI agent can call
python -m cua.cli catalog approve saucedemo.checkout_review
python -m cua.cli call saucedemo.checkout_review --param item_name="..." ...
```

`call` is the production entry point: name + typed args in, JSON out, approval
required, no human attendance.

### 6. Cross-tenant reuse (stretch goal)

```bash
make variant                                                   # second "tenant"
python scripts/add_variant_override.py saucedemo.checkout_review
python -m cua.cli replay --capability saucedemo.checkout_review --variant tenant-b \
  --secret username=SAUCE_USERNAME --secret password=SAUCE_PASSWORD --param ...
```

`target_app/variant_app.py` is the same flow as the public target with a
different skin, different wording ("Add to basket", "Sign On", "Next") and a
different mount path — one vendor product, two institutions. The same artifact
replays against it with a handful of declared overrides and no re-recording.

## Tests

```bash
pytest          # 51 tests, no browser and no API key required
```

They run against a scripted `FakeSurface`, so the whole replay contract —
success, business outcome, bounded recovery, checkpoint failure, policy denial,
escalation, redaction, control transfer — is verified deterministically and
offline. One test asserts that replaying never imports a model client.

## Running without live services

Everything except the discovery run works offline: `pytest`, the operator
console, and the tenant-variant app (`make variant`, then replay with
`--variant tenant-b`).

## Layout

```
src/cua/
  models.py            capability schema + replay result contract
  policy.py            allowlist + risk classification (one enforcement point)
  redaction.py         secret/PII scrubbing at the evidence boundary
  evidence.py          per-run directory + structured JSONL
  catalog.py           artifact storage + agent-facing tool schemas
  cli.py               discover / replay / catalog / call / operator-console
  surface/             base.py (the seam), web.py, harvest.js, desktop_stub.py
  agent/               prompts.py, tools.py, loop.py, compiler.py
  replay/              engine.py, locators.py, outcomes.py
  escalation/          broker.py, coordinator.py, operator_console.py
config/policy.yaml           allowlist, risky controls, limits
config/outcome_rules.yaml    built-in runtime-condition library
schema/                      JSON Schema for capability/v1
target_app/variant_app.py    second tenant on the same vendor product
```

# Design write-up

## 1. Architecture

One process, five components with narrow interfaces between them:

```
   CLI ──┬─► DiscoveryAgent ──► Compiler ──► Catalog (artifacts/*.json)
         │        │                              │
         └─► ReplayEngine ◄─────────────────────┘
                  │
     PolicyEngine │ RunEvidence │ EscalationCoordinator
                  ▼
              Surface  (web | legacy web | desktop)
```

**Key decision — perception is accessibility-shaped, not DOM-shaped.** A
`Surface` returns a flat list of controls described by *role, accessible name,
value, enabled, plus the nearest label-ish text*. Everything above that line is
written against that vocabulary only. This is the one vocabulary that exists on
all three surfaces in the brief's environment (ARIA in browsers, UIA/AX on the
desktop) and the one that survives a legacy app with no test IDs, because it is
derived from what the operator sees. Playwright is used purely as an actuator;
its selector engine never appears in an artifact.

Trade-off: a custom in-page harvester (`surface/harvest.js`) instead of
Playwright's a11y snapshot. The snapshot gives no handle to act on and drops
exactly the hint legacy apps need — the adjacent table cell that is the *de
facto* label when there is no `<label for>`. Cost: ~180 lines of JS to maintain,
and it tags elements with `data-cua-ref` (cleared every observation).

**Why not screenshot + coordinates.** Coordinates are the least stable property
on a screen and back-office screens are dense text; a coordinate-driven artifact
would be unreplayable after any font or zoom change. Screenshots are kept as
*evidence*, not as the control channel. The accessibility path degrades to OCR +
coordinates only where no a11y layer exists at all (Citrix, VDI) — and that
degradation is a new `Surface`, not a change to the artifact.

**Discovery and replay are separate programs.** `replay/engine.py` imports no
model client, and a test enforces it. That is the whole economic claim of the
system: the model is a one-time cost per flow, not a per-invocation cost.

**One policy enforcement point.** `PolicyEngine.check()` is called by the
discovery loop *and* by replay before every action. A recorded artifact cannot
outrank the allowlist, so a capability recorded when the policy was loose stops
working when it tightens.

**Single process, files on disk.** No queue, no database. Artifacts are JSON
files (one per capability version), evidence is a directory, the intervention
queue is a directory. The brief explicitly does not reward scaling
infrastructure, and each of these is a two-method swap.

## 2. Artifact schema

`capability/v1` — dataclasses in `src/cua/models.py`, JSON Schema in
`schema/capability.schema.json`. The shape is a **function signature**, not a
macro recording:

```jsonc
{
  "id": "saucedemo.checkout_review", "version": 1, "approval": "draft",
  "target": { "base_url": "...", "entry_path": "/", "app_id": "saucedemo", "variant_id": "base" },
  "params":  [{ "name": "item_name", "type": "string", "required": true },
              { "name": "password", "sensitive": true }],
  "outputs": [{ "name": "total", "type": "string" }],
  "steps":   [{ "id": "s5-click", "action": "click", "risk": "safe",
                "target": { "role": "link", "name": "{{item_name}}",
                            "anchor_text": "...", "css_hint": "...",
                            "strategies": ["role_name", "anchor", "css"] },
                "checkpoint": { "conditions": [{ "kind": "url_matches", "value": "/cart\\.html" }] },
                "extract": [ ... ] }],
  "outcome_rules": [{ "code": "LOCKED_OUT", "classification": "business_outcome",
                      "when": { "kind": "text_present", "value": "has been locked out" } }],
  "success": { "conditions": [{ "kind": "text_present", "value": "Checkout: Overview" }] },
  "variants": [{ "variant_id": "tenant-b", "base_url": "...", "step_patches": { ... } }],
  "provenance": { "discovered_by": "...", "run_id": "...", "llm_steps": 9 },
  "stability": { "replays": 6, "successes": 6, "score": 1.0 }
}
```

Why it is shaped this way:

- **`ControlRef` is an intent, not a selector.** Role + accessible name is the
  primary key because it is what the human uses and what a UIA tree exposes;
  `anchor_text` handles legacy layouts; `css_hint` exists but is last in the
  chain and the run log records *which* strategy won, so a reviewer can see a
  capability quietly degrading onto its fallback before it breaks outright.
- **Params are declared, and concrete values are lifted into them at compile
  time** — inside typed values, inside URLs (`/item/12345` → `/item/{{id}}`) and
  inside control names. Secrets are lifted too: that is precisely how the raw
  password stays out of the artifact.
- **URLs are relative to `target.base_url`,** so pointing the same artifact at a
  different institution's host is a field, not an edit.
- **`outcome_rules` are data.** Every runtime condition the flow knows about is
  declared with its classification, so replay dispatches on a table rather than
  on `try/except`. The discovery model is *required* to propose these (see §3).
- **Version is immutable and `approval` gates unattended use.** One file per
  (capability, version): a capability an agent called yesterday cannot silently
  become something else today.
- **`provenance` + `stability`** make it reviewable — which model, which run,
  how many human interventions, how reliably it has replayed since.

## 3. Determinism & error handling

**Determinism** comes from four things, in order of importance: (1) no model in
the decision loop; (2) intent-level control resolution with a declared strategy
order that **refuses when a match is ambiguous** rather than picking a plausible
candidate — clicking the wrong row in a servicing tool is worse than stopping;
(3) waits tied to observable state, not sleeps; (4) a checkpoint after every
state-changing step, so nothing proceeds on the assumption that a click worked.

Deliberate omission: "nth button on the page" is *not* a default locator
strategy. It always resolves and it is frequently wrong.

**The error taxonomy** is the load-bearing piece. Replay returns exactly four
statuses:

| Status | Meaning | Example |
|---|---|---|
| `success` | goal reached, checkpoint verified, outputs returned | review screen reached |
| `business_outcome` | a legitimate answer the caller needs | `LOCKED_OUT`, `PERMISSION_DENIED`, `RECORD_NOT_FOUND` |
| `escalated` | cannot safely proceed; a human was asked | ambiguous control, risky step without approval |
| `failed` | genuine breakage, with step / expected / observed / screenshot / DOM | checkpoint not met, recovery exhausted |

Rules are evaluated *before* each action (an interstitial that appeared, an
expired session) and *after* it (a validation error the action produced).
Recoverable conditions run a **closed set** of handlers — dismiss interstitial,
wait/retry, reload — bounded by `max_recoveries`, and recovery is never "ask the
model what to do", which would reintroduce the nondeterminism we removed.

Rules come from two places: the discovery model is required, in its `finish`
call, to name the conditions this flow could hit with the exact on-screen text
that identifies each one (it has just read the app, so it is well placed to);
those are typechecked and merged over a curated built-in library
(`config/outcome_rules.yaml`) of conditions true of every enterprise app. Flow-
specific rules win; anything the model proposes that does not typecheck is
dropped and logged.

Failures report what step, what was expected, what was observed, plus a
screenshot and DOM snapshot. UI drift is the secondary case: the strategy
recorded in each step log is the drift signal, and `--times N` gives a stability
score that gates the `draft → approved` transition.

## 4. Heterogeneity & multi-tenant

**Surface abstraction.** The seam is two methods — `observe() -> Observation`
and `act(SurfaceAction)`. A legacy web app needs no new code: frames are already
handled (each frame is harvested separately, `frame_hint` disambiguates) and the
anchor-text strategy exists specifically for table-labelled fields. A desktop
app is a new `Surface` (`surface/desktop_stub.py` documents the mapping: UIA
`ControlType` → role, `.Name` → name, runtime id → ref, window title → frame).
The artifact does not change; `css_hint` is the only web-flavoured field and it
is optional and last. What genuinely needs work for desktop: no URL means the
allowlist becomes "which executables and windows", `navigate` becomes "focus
window / walk a menu path", and checkpoints lean on window titles.

**Multi-tenant reuse.** A capability is recorded against a *product*
(`target.app_id`), not a tenant. A tenant is a `VariantOverride`: a base URL plus
patches to named steps. Patches are field-level merges, so an override that
renames one button does not fork the flow, and every deviation is visible in one
place for review. Demonstrated end to end: `target_app/variant_app.py` is the
same flow with different wording and mount path, and
`scripts/add_variant_override.py` attaches it to the artifact recorded against
the public target — replayed with `--variant tenant-b`, no re-recording.

Drift management, designed not built: replay already reports which locator
strategy resolved each control, so a fleet-level signal is "tenant X's steps are
resolving by `css` where the base resolves by `role_name`" — that is drift,
visible before it becomes breakage. The escalation path doubles as the repair
path: a base capability that fails for one tenant raises an intervention, and
the fix lands as an override rather than a fork. At hundreds of tenants I would
add a nightly canary replay per (capability, variant) and treat the stability
score as the promotion gate.

## 5. Escalation & handoff

**Detecting stuck** is not a heuristic — it is the set of states the engine
already refuses to guess through: an ambiguous or unresolvable control, a step
classified risky without approval, a recoverable condition that did not clear,
and the discovery model explicitly calling `escalate` (the prompt tells it that
escalating is a correct outcome and guessing is not).

**Control transfer** is a one-token state machine:
`AUTOMATION → (pause + cede) → HUMAN → (resolve) → AUTOMATION | ABORTED`.
The token lives in a file beside the request, so who holds control is observable
from outside the process. The intervention request carries the capability, the
goal, the step, why it stopped, the URL, a redacted state digest and a
screenshot.

**Same session, not a fresh one.** The automation blocks on the resolution file;
the browser context, its cookies and its half-filled form are never torn down.
The human drives that same window (banner injected, actions recorded via DOM
listeners), then the console posts `resume` / `skip_step` / `abort`. On resume,
automation re-observes and continues, and what the human did is written to
`evidence/<run>/human_actions.json`. Unattended mode still *files* the request
but does not block a batch job on an absent human.

Mocked: the operator console's viewport. Locally the operator uses the same
headed Chromium window. In production the browser runs in a container and the
console would embed a VNC or CDP view of it (`WebSurface.cdp_endpoint()` is the
seam) — the state machine does not change.

## 6. Safety

- **Allowlist** (`config/policy.yaml`): host globs, allowed route globs, an
  explicit deny list that beats a sloppy allow, and permitted action types.
  Enforced in one function called by both discovery and replay, before every
  action. Scheme-checked, so `file://` is refused.
- **Risky actions** are classified by *the intent the UI expresses* — the
  control's accessible name — rather than by HTTP verb, because in a legacy app
  everything is a POST and the only honest signal about what a button does is
  what it says to the operator. Default handling is `require_approval`: pause
  and ask a human, rather than block outright (blocking would make half the real
  work impossible) or merely flag (too weak for money movement). Configurable
  per environment.
- **Secrets and PII.** Secrets are passed by env-var *name*, registered with a
  redactor, and stored in artifacts as `{{param}}` placeholders. All evidence,
  logs and artifacts pass through the redactor at the boundary — pattern layer
  (SSN, PAN, email, tokens, long digit runs) plus exact-value layer. A test
  asserts no raw secret reaches a compiled artifact.
- **The model's tool surface is narrow by construction**: no arbitrary
  JavaScript, no cookie access, no free-form selector. It can only do what an
  operator at a terminal could.

**Limits, honestly.** Screenshots can contain PII that no regex catches — they
are failure/escalation evidence only and can be disabled per environment, but
this is the weakest point. Regex redaction has false negatives on unusual
account formats; production should key redaction off the app's own field
semantics. Name-based risk classification is a heuristic — a button labelled
"Continue" that posts a wire transfer defeats it, which is why irreversible
flows should also be gated by an explicit per-capability risk review before
`approved`. And the allowlist protects the *automation*; it does not constrain
what a human does after taking control.

## 7. Cuts

Deliberately cut, at seams that are real:

- **Real co-browsing console** — mocked viewport, real queue/token/resume. The
  brief puts this out of scope.
- **Desktop surface** — stubbed with the UIA mapping documented; the seam is
  exercised by the fact that replay is written against `Observation` only.
- **Multi-tenant plumbing** — the artifact *model* supports variants and this is
  demonstrated against a local second tenant, but there is no tenant registry,
  no canary scheduler and no per-tenant credential vault.
- **Assisted LLM fallback on replay failure** — an interesting stretch goal, but
  it weakens the determinism claim and I would rather ship the escalation path
  it competes with.
- **Persistence and concurrency** — files, one run per process.
- **Richer output typing** — outputs are strings; money and dates should be
  typed and normalised.

With more time, in order: (1) canary replays per (capability, variant) with the
stability score gating promotion, since that is what makes hundreds of tenants
survivable; (2) a proper drift report from the per-step strategy telemetry;
(3) code generation from an artifact (a page object / test file), which is cheap
once the schema exists; (4) a real co-browse view over CDP; (5) typed,
normalised outputs.

One thing found late and worth stating: the first version of the locator chain
included an "nth control of this role" fallback. It made a test pass and it
would have clicked the wrong button in production. It is gone, and the refusal
to resolve ambiguity is now the behaviour a test pins down.

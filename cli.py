"""Command line entry point.

    python -m cua.cli discover --goal ... --url ... --param k=v
    python -m cua.cli replay --capability <id> --param k=v
    python -m cua.cli catalog list|show|approve
    python -m cua.cli call <capability_id> --param k=v      # agent-style
    python -m cua.cli operator-console
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .agent.compiler import compile_capability
from .agent.loop import DEFAULT_MODEL, DiscoveryAgent
from .catalog import Catalog
from .escalation.broker import FileBroker
from .escalation.coordinator import EscalationCoordinator
from .evidence import RunEvidence
from .models import new_run_id
from .policy import PolicyEngine
from .redaction import Redactor
from .replay.engine import ReplayEngine
from .surface.web import WebSurface

DEFAULT_POLICY = os.environ.get("CUA_POLICY", "config/policy.yaml")
EVIDENCE_ROOT = os.environ.get("CUA_EVIDENCE", "evidence")
ARTIFACT_ROOT = os.environ.get("CUA_ARTIFACTS", "artifacts")
INTERVENTION_ROOT = os.environ.get("CUA_INTERVENTIONS", "interventions")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _kv(pairs: Optional[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"bad --param {pair!r}, expected name=value")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def _secrets(pairs: Optional[List[str]]) -> Dict[str, str]:
    """`--secret username=SAUCE_USERNAME` reads the value from the environment.

    Secrets never appear in argv (where they would land in shell history and
    process listings) and never reach the artifact.
    """
    out: Dict[str, str] = {}
    for pair in pairs or []:
        name, env_var = pair.split("=", 1)
        value = os.environ.get(env_var)
        if not value:
            raise SystemExit(f"env var {env_var} is empty; set it in .env")
        out[name.strip()] = value
    return out


def _headless() -> bool:
    return os.environ.get("CUA_HEADLESS", "1") not in {"0", "false", "no"}


def _surface() -> WebSurface:
    return WebSurface(
        headless=_headless(), slow_mo_ms=int(os.environ.get("CUA_SLOWMO", "0"))
    )


def _coordinator(evidence: RunEvidence, attended: bool) -> Optional[EscalationCoordinator]:
    broker = FileBroker(INTERVENTION_ROOT) if attended else None
    return EscalationCoordinator(
        broker,
        evidence,
        attended=attended,
        timeout_s=int(os.environ.get("CUA_ESCALATION_TIMEOUT", "600")),
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    params = _kv(args.param)
    secrets = _secrets(args.secret)
    params.update(secrets)

    redactor = Redactor(secrets.values())
    evidence = RunEvidence(EVIDENCE_ROOT, new_run_id("discovery"), redactor)
    policy = PolicyEngine.load(args.policy)

    surface = _surface()
    surface.start()
    try:
        agent = DiscoveryAgent(
            surface=surface,
            policy=policy,
            evidence=evidence,
            escalation=_coordinator(evidence, args.attended),
            model=args.model,
            max_steps=args.max_steps,
            timeout_s=args.timeout,
        )
        run = agent.run(args.goal, args.url, params)
    finally:
        surface.stop()

    print(f"\ndiscovery status: {run.status} ({run.stop_reason})")
    if run.status != "complete":
        print(f"evidence: {evidence.dir}")
        return 2

    capability = compile_capability(
        run,
        policy,
        capability_id=args.id,
        app_id=args.app,
        sensitive_params=list(secrets),
        log=evidence.log,
    )
    path = Catalog(ARTIFACT_ROOT).save(capability)
    evidence.write_json("capability.json", capability.to_dict())
    print(f"artifact:  {path}")
    print(f"evidence:  {evidence.dir}")
    print(f"replay it: python -m cua.cli replay --capability {capability.id}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    catalog = Catalog(ARTIFACT_ROOT)
    capability = catalog.load(args.capability, args.version)
    if args.variant:
        capability = capability.for_variant(args.variant)

    params = _kv(args.param)
    params.update(_secrets(args.secret))
    policy = PolicyEngine.load(args.policy)

    statuses = []
    for attempt in range(args.times):
        redactor = Redactor(_secrets(args.secret).values())
        evidence = RunEvidence(EVIDENCE_ROOT, new_run_id("replay"), redactor)
        surface = _surface()
        surface.start()
        try:
            engine = ReplayEngine(
                surface=surface,
                policy=policy,
                evidence=evidence,
                escalation=_coordinator(evidence, args.attended),
                allow_risky=args.allow_risky,
                require_approval=args.require_approval,
            )
            result = engine.run(capability, params)
        finally:
            surface.stop()

        catalog.record_replay(capability, result.ok)
        statuses.append(result.status)
        print(json.dumps(_caller_view(result), indent=2))
        print(f"evidence: {evidence.dir}")

    if args.times > 1:
        good = sum(1 for s in statuses if s == "success")
        print(f"\nstability: {good}/{args.times} successful -> {good / args.times:.2f}")
    return 0 if statuses[-1] in {"success", "business_outcome"} else 1


def _caller_view(result) -> Dict[str, Any]:
    """What the calling agent actually gets back."""
    payload: Dict[str, Any] = {
        "capability": result.capability_id,
        "version": result.capability_version,
        "status": result.status,
        "outputs": result.outputs,
        "duration_ms": result.duration_ms,
    }
    if result.outcome:
        payload["outcome"] = {
            "code": result.outcome.code,
            "classification": result.outcome.classification,
            "message": result.outcome.message,
            "step": result.outcome.step_id,
        }
    if result.failure:
        payload["failure"] = {
            "step": result.failure.step_id,
            "expected": result.failure.expected,
            "observed": result.failure.observed,
            "screenshot": result.failure.screenshot,
        }
    return payload


def cmd_catalog(args: argparse.Namespace) -> int:
    catalog = Catalog(ARTIFACT_ROOT)
    if args.catalog_command == "list":
        print(json.dumps(catalog.tool_definitions(), indent=2))
        return 0
    if args.catalog_command == "show":
        print(json.dumps(catalog.load(args.capability, args.version).to_dict(), indent=2))
        return 0
    if args.catalog_command == "approve":
        capability = catalog.load(args.capability, args.version)
        score = capability.stability.score
        if score is not None and score < 0.8:
            print(f"refusing: stability {score} is below 0.8")
            return 1
        capability.approval = "approved"
        path = catalog.save(capability, bump_if_exists=False)
        print(f"approved {capability.id} v{capability.version} -> {path}")
        return 0
    return 1


def cmd_call(args: argparse.Namespace) -> int:
    """Invoke a capability the way an agent would: name + typed args in, JSON out."""
    args.version = None
    args.variant = None
    args.times = 1
    args.attended = False
    args.allow_risky = False
    args.require_approval = True
    args.capability = args.capability_id
    return cmd_replay(args)


def cmd_operator(args: argparse.Namespace) -> int:
    from .escalation.operator_console import main

    main()
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cua", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--policy", default=DEFAULT_POLICY)
    common.add_argument("--param", action="append", help="name=value (repeatable)")
    common.add_argument(
        "--secret", action="append", help="name=ENV_VAR (value read from the environment)"
    )

    discover = sub.add_parser("discover", parents=[common], help="LLM discovery run")
    discover.add_argument("--goal", required=True)
    discover.add_argument("--url", required=True)
    discover.add_argument("--id", required=True, help="capability id, e.g. app.flow")
    discover.add_argument("--app", default="unknown-app", help="vendor product id")
    discover.add_argument("--model", default=DEFAULT_MODEL)
    discover.add_argument("--max-steps", type=int, default=20)
    discover.add_argument("--timeout", type=int, default=300)
    discover.add_argument("--attended", action="store_true", help="allow human handoff")
    discover.set_defaults(func=cmd_discover)

    replay = sub.add_parser("replay", parents=[common], help="deterministic replay")
    replay.add_argument("--capability", required=True)
    replay.add_argument("--version", type=int, default=None)
    replay.add_argument("--variant", default=None, help="tenant variant id")
    replay.add_argument("--times", type=int, default=1, help="stability check")
    replay.add_argument("--attended", action="store_true")
    replay.add_argument("--allow-risky", action="store_true")
    replay.add_argument(
        "--require-approval",
        action="store_true",
        help="refuse to run a capability that is still a draft",
    )
    replay.set_defaults(func=cmd_replay)

    catalog = sub.add_parser("catalog", help="agent-facing capability catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    for name in ("list", "show", "approve"):
        node = catalog_sub.add_parser(name)
        if name != "list":
            node.add_argument("capability")
            node.add_argument("--version", type=int, default=None)
    catalog.set_defaults(func=cmd_catalog)

    call = sub.add_parser("call", parents=[common], help="invoke a capability by name")
    call.add_argument("capability_id")
    call.set_defaults(func=cmd_call)

    operator = sub.add_parser("operator-console", help="mock operator console")
    operator.set_defaults(func=cmd_operator)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _load_dotenv()
    args = build_parser().parse_args(argv)
    return args.func(args)


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


if __name__ == "__main__":
    sys.exit(main())

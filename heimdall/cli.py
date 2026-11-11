"""Command line: `heimdall serve`, `heimdall audit verify`, `heimdall incident ...`."""

import argparse
import json
import sys
from pathlib import Path

from heimdall.config import Config
from heimdall.core import anchor as anchoring
from heimdall.core.audit import AuditLog
from heimdall.core.incidents import IncidentManager
from heimdall.core.store import connect
from heimdall.tools.dependencies import DependencyGraph


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from heimdall.app import build

    heimdall = build(Config.from_env())
    if heimdall.worker is not None:
        heimdall.worker.start()
    uvicorn.run(heimdall.api, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    config = Config.from_env()
    log = AuditLog(connect(args.db or config.db_path))
    head = log.head()

    if args.anchors is None and not args.gitea:
        problem = log.verify_chain()
        checked = "chain only (no anchors given)"
        anchors: list[anchoring.Anchor] = []
    else:
        if args.gitea:
            from heimdall.app import gitea_anchor_publisher

            publisher = gitea_anchor_publisher(config)
            if publisher is None:
                print("Gitea is not configured (HEIMDALL_GITEA_URL / HEIMDALL_GITEA_TOKEN)")
                return 2
            anchors = publisher.read()
        else:
            anchors = anchoring.read_anchor_file(args.anchors)
        if args.public_key:
            public_key = anchoring.load_public_key(args.public_key.read_bytes())
        elif config.audit_key_path:
            public_key = anchoring.load_private_key(config.audit_key_path).public_key()
        else:
            print("a public key is needed to check anchors (--public-key)")
            return 2
        problem = anchoring.verify(log, anchors, public_key)
        checked = f"chain and {len(anchors)} anchors"

    if problem is not None:
        print(f"FAILED at seq {problem.seq}: {problem.reason}")
        return 1
    print(f"OK: {head.seq if head else 0} events, {checked}")
    return 0


def _manager(args: argparse.Namespace) -> tuple[IncidentManager, AuditLog]:
    config = Config.from_env()
    conn = connect(args.db or config.db_path)
    log = AuditLog(conn)
    return IncidentManager(conn, log, DependencyGraph.load(config.dependencies_path)), log


def cmd_incident_list(args: argparse.Namespace) -> int:
    manager, log = _manager(args)
    if args.json:
        out = []
        for incident in manager.all():
            diagnoses = [e.payload for e in log.events(incident.id) if e.event_type == "diagnosis"]
            out.append(
                {**incident.model_dump(mode="json"), "result": diagnoses[-1] if diagnoses else None}
            )
        print(json.dumps(out))
        return 0
    for incident in manager.all():
        flags = " flapping" if incident.flapping else ""
        print(
            f"{incident.id}  {incident.state:<18} {incident.opened_at:%Y-%m-%d %H:%M:%S}  "
            f"{','.join(incident.services) or '-'}{flags}"
        )
    return 0


def cmd_incident_show(args: argparse.Namespace) -> int:
    manager, log = _manager(args)
    try:
        incident = manager.get(args.id)
    except KeyError:
        print(f"no incident {args.id}")
        return 1
    print(f"{incident.id}  state={incident.state}  services={','.join(incident.services) or '-'}")
    for event in log.events(incident.id):
        detail = ", ".join(
            f"{k}={v}" for k, v in event.payload.items() if not isinstance(v, dict | list)
        )
        print(f"  {event.seq:>5}  {event.timestamp[:19]}  {event.event_type:<18} {detail}")
    return 0


def cmd_incident_investigate(args: argparse.Namespace) -> int:
    """Investigate one incident now, in the foreground, and print the result."""
    from heimdall.agent.setup import build_investigator, build_llm
    from heimdall.agent.worker import investigate_incident

    config = Config.from_env()
    conn = connect(args.db or config.db_path)
    log = AuditLog(conn)
    manager = IncidentManager(conn, log, DependencyGraph.load(config.dependencies_path))
    llm = build_llm(config)
    if llm is None:
        print("no LLM configured (HEIMDALL_LLM_PROVIDER)")
        return 2
    try:
        incident = manager.get(args.id)
    except KeyError:
        print(f"no incident {args.id}")
        return 1
    run = investigate_incident(incident, manager, build_investigator(config, log, llm), conn)
    print(run.diagnosis.model_dump_json(indent=2))
    print(
        f"\n{run.stopped_because}: {run.tool_calls} tool calls, {run.llm_calls} LLM calls, "
        f"{run.prompt_tokens + run.completion_tokens} tokens, {run.seconds}s, "
        f"{run.rejections} rejected drafts"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="heimdall")
    commands = parser.add_subparsers(required=True)

    serve = commands.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - runs in a container
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    audit = commands.add_parser("audit").add_subparsers(required=True)
    verify = audit.add_parser("verify", help="check the audit chain and its anchors")
    verify.add_argument("--db", type=Path)
    verify.add_argument("--anchors", type=Path, help="anchor file (JSON Lines)")
    verify.add_argument("--gitea", action="store_true", help="read anchors from the Gitea repo")
    verify.add_argument("--public-key", type=Path, help="PEM file; default: derived from the key")
    verify.set_defaults(func=cmd_audit_verify)

    incident = commands.add_parser("incident").add_subparsers(required=True)
    listing = incident.add_parser("list")
    listing.add_argument("--db", type=Path)
    listing.add_argument("--json", action="store_true", help="include each diagnosis")
    listing.set_defaults(func=cmd_incident_list)
    show = incident.add_parser("show")
    show.add_argument("id")
    show.add_argument("--db", type=Path)
    show.set_defaults(func=cmd_incident_show)

    investigate = incident.add_parser("investigate")
    investigate.add_argument("id")
    investigate.add_argument("--db", type=Path)
    investigate.set_defaults(func=cmd_incident_investigate)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Hermes Event Consumer/Router

Scans Hermes inbox for unprocessed events, routes them to agents,
and manages their lifecycle through processing -> completed/failed states.

All filesystem operations go through hermes_bus.py (atomic claims + shared layout).

Usage:
    python hermes_event_consumer.py --agent agent-seo-auditor --limit 10
    python hermes_event_consumer.py --agent agent-seo-auditor --limit 10 --dry-run
    python hermes_event_consumer.py --route-all --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from hermes_bus import (
    MAX_HERMES_RETRIES,
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
    get_hermes_paths,
    list_inbox_json_sorted,
    plain_move,
    read_json_event,
    release_claim_for_event,
    retry_or_fail_claimed_event,
)
from hermes_bus import clear_retry_sidecar_for_event as clear_retries


MAX_RETRIES = MAX_HERMES_RETRIES


def ensure_dirs() -> None:
    ensure_hermes_dirs()


def read_event(path: Path) -> dict[str, Any] | None:
    return read_json_event(path)


def list_inbox_events() -> list[Path]:
    return list_inbox_json_sorted()


def move_event(src: Path, dst_dir: Path, dry_run: bool = False) -> Path | None:
    return plain_move(src, dst_dir, dry_run=dry_run)


def get_target_agent(event: dict[str, Any]) -> str | None:
    agent = event.get("target_agent")
    if agent:
        return agent
    routing_key = event.get("routing_key", "")
    if routing_key.startswith("agent."):
        return routing_key.split(".", 1)[1]
    return None


def route_event(path: Path, dry_run: bool = False) -> dict[str, Any] | None:
    event = read_event(path)
    if event is None:
        move_event(path, get_hermes_paths().failed, dry_run=dry_run)
        return None

    target = get_target_agent(event)
    if not target:
        print(f"[WARN] Event {path.name} has no target_agent or valid routing_key. Moving to failed.")
        move_event(path, get_hermes_paths().failed, dry_run=dry_run)
        return None

    processing_path = claim_inbox_json(path, dry_run=dry_run)
    if processing_path is None:
        return None

    event["_file_path"] = str(processing_path)
    event["_target_agent"] = target
    return event


def complete_event(event: dict[str, Any], dry_run: bool = False) -> bool:
    return complete_claimed_event(event, dry_run=dry_run)


def fail_event(event: dict[str, Any], dry_run: bool = False) -> bool:
    return fail_claimed_event(event, dry_run=dry_run)


def retry_or_fail(event: dict[str, Any], dry_run: bool = False, last_error: str = "") -> bool:
    return retry_or_fail_claimed_event(
        event,
        dry_run=dry_run,
        max_retries=MAX_RETRIES,
        last_error=last_error,
    )


def release_claim_lock(event: dict[str, Any]) -> None:
    release_claim_for_event(event)


def summarize_event(event: dict[str, Any]) -> str:
    event_id = event.get("id", "no-id")
    event_type = event.get("type", "unknown")
    priority = event.get("priority", "?")
    source = event.get("source_agent", "unknown")
    target = get_target_agent(event) or "unrouted"
    payload = event.get("payload", {})

    message = payload.get("message") or payload.get("summary") or payload.get("issue") or ""
    if not message and isinstance(payload, dict):
        first_key = next(iter(payload.keys()), None)
        if first_key:
            message = f"{first_key}: {payload[first_key]}"

    lines = [
        f"Event: {event_id}",
        f"  Type     : {event_type}",
        f"  Priority : {priority}",
        f"  Source   : {source}",
        f"  Target   : {target}",
    ]
    if message:
        lines.append(f"  Summary  : {message}")
    return "\n".join(lines)


def poll_agent_events(agent: str, limit: int = 10, dry_run: bool = False) -> list[dict[str, Any]]:
    ensure_dirs()
    files = list_inbox_events()
    matched: list[dict[str, Any]] = []

    for path in files:
        if len(matched) >= limit:
            break
        event = read_event(path)
        if event is None:
            continue
        target = get_target_agent(event)
        if target == agent:
            routed = route_event(path, dry_run=dry_run)
            if routed:
                matched.append(routed)
                print(summarize_event(routed))
                print()
    return matched


def route_all_events(dry_run: bool = False) -> dict[str, list[dict[str, Any]]]:
    ensure_dirs()
    files = list_inbox_events()
    routed: dict[str, list[dict[str, Any]]] = {}

    for path in files:
        event = route_event(path, dry_run=dry_run)
        if event:
            agent = event["_target_agent"]
            routed.setdefault(agent, []).append(event)
            print(summarize_event(event))
            print()
    return routed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hermes Event Consumer/Router",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --agent agent-seo-auditor --limit 10
  %(prog)s --agent agent-seo-auditor --limit 10 --dry-run
  %(prog)s --route-all --dry-run
""",
    )
    parser.add_argument("--agent", type=str, help="Agent name to poll events for")
    parser.add_argument("--limit", type=int, default=10, help="Max events to return (default: 10)")
    parser.add_argument("--route-all", action="store_true", help="Route all inbox events and show summaries")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without moving files")
    parser.add_argument("--complete", type=str, metavar="EVENT_ID", help="Mark a processing event as completed by ID")
    parser.add_argument("--fail", type=str, metavar="EVENT_ID", help="Mark a processing event as failed by ID")

    args = parser.parse_args()
    hp = get_hermes_paths()

    if args.complete:
        ensure_dirs()
        found = False
        for f in hp.processing.glob("*.json"):
            ev = read_event(f)
            if ev and ev.get("id") == args.complete:
                if complete_event({**ev, "_file_path": str(f)}, dry_run=args.dry_run):
                    print(f"[OK] Completed event {args.complete}")
                found = True
                break
        if not found:
            print(f"[ERROR] Event {args.complete} not found in processing/")
        return 0

    if args.fail:
        ensure_dirs()
        found = False
        for f in hp.processing.glob("*.json"):
            ev = read_event(f)
            if ev and ev.get("id") == args.fail:
                if fail_event({**ev, "_file_path": str(f)}, dry_run=args.dry_run):
                    print(f"[OK] Failed event {args.fail}")
                found = True
                break
        if not found:
            print(f"[ERROR] Event {args.fail} not found in processing/")
        return 0

    if args.route_all:
        results = route_all_events(dry_run=args.dry_run)
        total = sum(len(v) for v in results.values())
        print(f"[SUMMARY] Routed {total} event(s) to {len(results)} agent(s).")
        return 0

    if args.agent:
        events = poll_agent_events(args.agent, limit=args.limit, dry_run=args.dry_run)
        print(f"[SUMMARY] Polled {len(events)} event(s) for agent '{args.agent}'.")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

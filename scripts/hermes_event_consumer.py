#!/usr/bin/env python3
"""
Hermes Event Consumer/Router

Scans ~/hermes-events/inbox/ for unprocessed events, routes them to agents,
and manages their lifecycle through processing -> completed/failed states.

Usage:
    python hermes_event_consumer.py --agent agent-seo-auditor --limit 10
    python hermes_event_consumer.py --agent agent-seo-auditor --limit 10 --dry-run
    python hermes_event_consumer.py --route-all --dry-run
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Base directory for all event queues
EVENTS_BASE = Path.home() / "hermes-events"
INBOX_DIR = EVENTS_BASE / "inbox"
PROCESSING_DIR = EVENTS_BASE / "processing"
COMPLETED_DIR = EVENTS_BASE / "completed"
FAILED_DIR = EVENTS_BASE / "failed"
# Sidecar state lives alongside the event but in a dedicated directory so the
# original event JSON remains immutable (P1-19 fix). Each event gets a
# `<event_id>.retries.json` containing only `{ "retries": N, "last_error": ... }`.
STATE_DIR = EVENTS_BASE / "state"

# Max retries before moving to failed
MAX_RETRIES = 3


def ensure_dirs() -> None:
    """Create processing/completed/failed/state directories if they don't exist."""
    for d in (INBOX_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR, STATE_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── Retry sidecar (P1-19 fix) ─────────────────────────────────────────────
def _retry_sidecar_path(event: dict[str, Any]) -> Path:
    eid = event.get("id") or Path(event.get("_file_path", "untitled")).stem
    return STATE_DIR / f"{eid}.retries.json"


def get_retries(event: dict[str, Any]) -> int:
    """Read retry count from sidecar (preferred) with fallback to legacy
    in-event `_retries` field for backward compat with already-queued events."""
    p = _retry_sidecar_path(event)
    if p.exists():
        try:
            return int(json.loads(p.read_text(encoding="utf-8")).get("retries", 0))
        except (json.JSONDecodeError, ValueError, OSError):
            return 0
    # Legacy fallback for events stamped by the previous in-place mutator.
    return int(event.get("_retries", 0))


def write_retries(event: dict[str, Any], retries: int, last_error: str = "") -> bool:
    """Atomically write the retry sidecar via tmp + rename."""
    p = _retry_sidecar_path(event)
    tmp = p.with_suffix(".tmp")
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                {
                    "event_id": event.get("id"),
                    "retries": retries,
                    "last_error": last_error[:500],
                    "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(p))  # atomic on POSIX
        return True
    except OSError as exc:
        print(f"[ERROR] Failed to write retry sidecar for {event.get('id')}: {exc}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def clear_retries(event: dict[str, Any]) -> None:
    """Remove sidecar after successful processing or final failure."""
    p = _retry_sidecar_path(event)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def list_inbox_events() -> list[Path]:
    """Return sorted list of unprocessed JSON event files in inbox."""
    if not INBOX_DIR.exists():
        return []
    files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    return files


def read_event(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON event file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[ERROR] Failed to read event {path.name}: {exc}")
        return None


def get_target_agent(event: dict[str, Any]) -> str | None:
    """Extract target agent from event using target_agent or routing_key."""
    agent = event.get("target_agent")
    if agent:
        return agent
    routing_key = event.get("routing_key", "")
    if routing_key.startswith("agent."):
        return routing_key.split(".", 1)[1]
    return None


def move_event(src: Path, dst_dir: Path, dry_run: bool = False) -> Path | None:
    """Move event file to destination directory."""
    dst = dst_dir / src.name
    if dry_run:
        print(f"[DRY-RUN] Would move {src.name} -> {dst_dir.name}/")
        return dst
    try:
        shutil.move(str(src), str(dst))
        return dst
    except OSError as exc:
        print(f"[ERROR] Failed to move {src.name} to {dst_dir.name}: {exc}")
        return None


def atomic_claim_from_inbox(src: Path, dst_dir: Path, dry_run: bool = False) -> Path | None:
    """Atomically claim an inbox event (P1-10 fix).

    Uses `os.rename` which is atomic on POSIX same-filesystem moves: if two
    consumers race for the same file, only one rename succeeds; the loser
    sees a FileNotFoundError and silently skips.

    A separate `<event_id>.claim` lockfile in `STATE_DIR` is also created with
    `O_CREAT|O_EXCL` so cross-filesystem stragglers can never double-claim
    even if `os.rename` falls back. The lockfile is cleared by
    `complete_event` / `fail_event` via `clear_retries` (whose path matches).
    """
    dst = dst_dir / src.name
    if dry_run:
        return dst
    # 1. Acquire exclusive claim lock first (cheap, doesn't move the event yet).
    lock_path = STATE_DIR / f"{src.stem}.claim"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, datetime.now(timezone.utc).isoformat().encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        # Another consumer already claimed (or crashed mid-claim within last
        # CLAIM_TTL seconds). Skip silently.
        return None
    except OSError as exc:
        print(f"[ERROR] Could not create claim lock for {src.name}: {exc}")
        return None

    # 2. Atomic rename inbox → processing. If another consumer beat us between
    #    the lock and the rename (very narrow race), os.rename will raise
    #    FileNotFoundError; we release the lock and return None.
    try:
        os.rename(str(src), str(dst))
    except FileNotFoundError:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    except OSError as exc:
        # Could happen on cross-filesystem moves; fall back to shutil.move.
        try:
            shutil.move(str(src), str(dst))
        except OSError as exc2:
            print(f"[ERROR] Atomic claim failed for {src.name}: {exc} / {exc2}")
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
    return dst


def release_claim_lock(event: dict[str, Any]) -> None:
    """Best-effort removal of the `.claim` lock file."""
    eid = event.get("id") or Path(event.get("_file_path", "untitled")).stem
    try:
        (STATE_DIR / f"{eid}.claim").unlink(missing_ok=True)
    except OSError:
        pass


def summarize_event(event: dict[str, Any]) -> str:
    """Return a short, actionable summary of an event."""
    event_id = event.get("id", "no-id")
    event_type = event.get("type", "unknown")
    priority = event.get("priority", "?")
    source = event.get("source_agent", "unknown")
    target = get_target_agent(event) or "unrouted"
    payload = event.get("payload", {})

    # Try to extract a human-friendly message from payload
    message = payload.get("message") or payload.get("summary") or payload.get("issue") or ""
    if not message and isinstance(payload, dict):
        # Use first key-value pair as hint if no explicit message
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


def route_event(path: Path, dry_run: bool = False) -> dict[str, Any] | None:
    """Route a single event: atomically claim from inbox → processing, then
    parse and return the event dict. Returns None if another consumer already
    grabbed the file (P1-10 fix)."""
    event = read_event(path)
    if event is None:
        move_event(path, FAILED_DIR, dry_run=dry_run)
        return None

    target = get_target_agent(event)
    if not target:
        print(f"[WARN] Event {path.name} has no target_agent or valid routing_key. Moving to failed.")
        move_event(path, FAILED_DIR, dry_run=dry_run)
        return None

    processing_path = atomic_claim_from_inbox(path, PROCESSING_DIR, dry_run=dry_run)
    if processing_path is None:
        # Another consumer claimed it first — silently skip.
        return None

    event["_file_path"] = str(processing_path)
    event["_target_agent"] = target
    return event


def complete_event(event: dict[str, Any], dry_run: bool = False) -> bool:
    """Move an event from processing to completed and clear its retry sidecar."""
    path = Path(event.get("_file_path", ""))
    if not path.exists():
        print(f"[WARN] Processing file missing for event {event.get('id')}")
        return False
    moved = move_event(path, COMPLETED_DIR, dry_run=dry_run) is not None
    if moved and not dry_run:
        clear_retries(event)
        release_claim_lock(event)
    return moved


def fail_event(event: dict[str, Any], dry_run: bool = False) -> bool:
    """Move an event from processing to failed and clear its retry sidecar."""
    path = Path(event.get("_file_path", ""))
    if not path.exists():
        print(f"[WARN] Processing file missing for event {event.get('id')}")
        return False
    moved = move_event(path, FAILED_DIR, dry_run=dry_run) is not None
    if moved and not dry_run:
        clear_retries(event)
        release_claim_lock(event)
    return moved


def retry_or_fail(event: dict[str, Any], dry_run: bool = False, last_error: str = "") -> bool:
    """Increment retry counter (sidecar) and either re-queue the event or move
    it to `failed/`. The original event JSON is NEVER mutated (P1-19 fix).
    """
    retries = get_retries(event) + 1
    if retries >= MAX_RETRIES:
        print(f"[FAIL] Max retries ({MAX_RETRIES}) reached for event {event.get('id')}. Moving to failed.")
        ok = fail_event(event, dry_run=dry_run)
        if ok and not dry_run:
            clear_retries(event)
        return ok
    path = Path(event.get("_file_path", ""))
    if not path.exists():
        print(f"[ERROR] Cannot retry: source file missing for {event.get('id')}")
        return False
    if dry_run:
        print(f"[DRY-RUN] Would retry event {event.get('id')} ({retries}/{MAX_RETRIES}).")
        return True
    if not write_retries(event, retries, last_error=last_error):
        return False
    move_event(path, INBOX_DIR, dry_run=dry_run)
    print(f"[RETRY] Event {event.get('id')} retry {retries}/{MAX_RETRIES}.")
    return True


def poll_agent_events(agent: str, limit: int = 10, dry_run: bool = False) -> list[dict[str, Any]]:
    """Poll inbox for events targeting a specific agent, route them, and return list."""
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
    """Route all inbox events and group by target agent."""
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

    if args.complete:
        ensure_dirs()
        # Find in processing
        found = False
        for f in PROCESSING_DIR.glob("*.json"):
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
        for f in PROCESSING_DIR.glob("*.json"):
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

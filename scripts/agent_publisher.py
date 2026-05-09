#!/usr/bin/env python3
"""
Hermes Event Consumer — Agent Publisher

Processes content.written and price.asin_replaced events from the Hermes event
queue, creates git branches, commits changed MDX/JSON files, opens GitHub PRs,
monitors CI status, merges on green, and emits deployment.completed events.

Usage:
    python agent_publisher.py --consume --limit 10
    python agent_publisher.py --consume --limit 10 --dry-run
    python agent_publisher.py --consume --limit 1 --dry-run --no-auto-merge
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Load .env from scripts directory
def _load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    if key not in os.environ:
                        os.environ[key] = val

_load_env()

# ── Configuration ──────────────────────────────────────────────────────────
BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
EVENTS_BASE = Path.home() / "hermes-events"
INBOX_DIR = EVENTS_BASE / "inbox"
PROCESSING_DIR = EVENTS_BASE / "processing"
COMPLETED_DIR = EVENTS_BASE / "completed"
FAILED_DIR = EVENTS_BASE / "failed"

MAX_RETRIES = 3
CI_POLL_INTERVAL = 30  # seconds
CI_MAX_WAIT = 900  # 15 minutes

# Production hosts per site — used to ping IndexNow with the canonical URLs
# of changed pages right after a successful merge, so Bing/Yandex/Seznam pick
# up the new/updated content within minutes rather than days.
PROD_HOSTS = {
    "matelas": "www.matelas-expert.fr",
    "aspirateur": "www.top-aspirateur.fr",
    "cafe": "www.brewmance.fr",
    "pixinstant": "www.pixinstant.com",
    "bureau": "www.bureau-expert.fr",
    "airpurify": "www.airpurifyhq.com",
    "safehive": "www.safehivehq.com",
    "pawhive": "www.pawhivehq.com",
}

# Site directory → repo mapping (from AGENTS.md)
SITE_REPOS = {
    "matelas": {
        "dir": BASE_DIR / "matelas",
        "remote": "https://github.com/cat0ne/matelas-literie.git",
    },
    "aspirateur": {
        "dir": BASE_DIR / "aspirateur",
        "remote": "https://github.com/cat0ne/meilleur-aspirateur.git",
    },
    "cafe": {
        "dir": BASE_DIR / "cafe",
        "remote": "https://github.com/cat0ne/affiliation-cafe.git",
    },
    "pixinstant": {
        "dir": BASE_DIR / "pixinstant",
        "remote": "https://github.com/cat0ne/affiliation-pixinstant.git",
    },
    "bureau": {
        "dir": BASE_DIR / "bureau",
        "remote": "https://github.com/cat0ne/bureau-expert.git",
    },
    "affiliate-suite": {
        "dir": BASE_DIR / "affiliate-suite",
        "remote": "https://github.com/cat0ne/affiliate-suite.git",
    },
}

# Supported event types
SUPPORTED_TYPES = {
    "content.written",
    "price.asin_replaced",
    "seo.fix_applied",
    "seo.recrawl_requested",
    "deployment.rollback_requested",
}

# ── Hermes Event Helpers ─────────────────────────────────────────────────────


def ensure_dirs() -> None:
    for d in (INBOX_DIR, PROCESSING_DIR, COMPLETED_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def emit_event(
    event_type: str,
    payload: dict,
    priority: int = 3,
    source: str = "agent-publisher",
    target_agent: str | None = None,
) -> dict:
    """Write a Hermes event JSON file to the inbox."""
    ensure_dirs()
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_agent": source,
        "routing_key": f"agent.{event_type.split('.')[0]}",
    }
    if target_agent:
        event["target_agent"] = target_agent
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{event_type.replace('.', '_')}_{event['id'][:8]}.json"
    path = INBOX_DIR / filename
    path.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📤 Emitted: {event_type} → {filename}")
    return event


def read_event(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[ERROR] Failed to read event {path.name}: {exc}")
        return None


def move_event(src: Path, dst_dir: Path, dry_run: bool = False) -> Path | None:
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


def complete_event(event: dict[str, Any], dry_run: bool = False) -> bool:
    path = Path(event.get("_file_path", ""))
    if not path.exists():
        print(f"[WARN] Processing file missing for event {event.get('id')}")
        return False
    return move_event(path, COMPLETED_DIR, dry_run=dry_run) is not None


def fail_event(event: dict[str, Any], dry_run: bool = False) -> bool:
    path = Path(event.get("_file_path", ""))
    if not path.exists():
        print(f"[WARN] Processing file missing for event {event.get('id')}")
        return False
    return move_event(path, FAILED_DIR, dry_run=dry_run) is not None


# ── Git / GitHub Helpers ─────────────────────────────────────────────────────


def run_git(cmd: list[str], cwd: Path, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in cwd. In dry-run, print and return a fake result."""
    if dry_run:
        print(f"[DRY-RUN] git {' '.join(cmd)}  (cwd={cwd})")
        return subprocess.CompletedProcess(args=["git"] + cmd, returncode=0, stdout="", stderr="")
    result = subprocess.run(
        ["git"] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["git"] + cmd, output=result.stdout, stderr=result.stderr
        )
    return result


def run_gh(cmd: list[str], cwd: Path, dry_run: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    """Run a gh CLI command in cwd."""
    if dry_run:
        print(f"[DRY-RUN] gh {' '.join(cmd)}  (cwd={cwd})")
        return subprocess.CompletedProcess(args=["gh"] + cmd, returncode=0, stdout="", stderr="")
    result = subprocess.run(
        ["gh"] + cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, ["gh"] + cmd, output=result.stdout, stderr=result.stderr
        )
    return result


def git_staged_files(repo_dir: Path) -> list[str]:
    """Return list of staged file paths relative to repo root."""
    result = run_git(["diff", "--cached", "--name-only"], cwd=repo_dir, check=True)
    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return files


def git_any_changes(repo_dir: Path) -> bool:
    """Return True if there are uncommitted changes (staged or unstaged)."""
    result = run_git(["status", "--porcelain"], cwd=repo_dir, check=True)
    return bool(result.stdout.strip())


def git_discard_all(repo_dir: Path, dry_run: bool = False) -> None:
    """Hard reset and clean untracked files to get back to a clean main."""
    run_git(["reset", "--hard", "HEAD"], cwd=repo_dir, dry_run=dry_run)
    run_git(["clean", "-fd"], cwd=repo_dir, dry_run=dry_run)


def git_ensure_clean_main(repo_dir: Path, dry_run: bool = False) -> None:
    """Stash or discard any local changes and checkout main."""
    if git_any_changes(repo_dir):
        print(f"  [WARN] Uncommitted changes in {repo_dir.name}, stashing/discarding.")
        git_discard_all(repo_dir, dry_run=dry_run)
    run_git(["checkout", "main"], cwd=repo_dir, dry_run=dry_run)
    run_git(["pull", "origin", "main"], cwd=repo_dir, dry_run=dry_run)


def git_create_branch(repo_dir: Path, branch: str, dry_run: bool = False) -> None:
    run_git(["checkout", "-b", branch], cwd=repo_dir, dry_run=dry_run)


def git_add_files(repo_dir: Path, files: list[str], dry_run: bool = False) -> None:
    if not files:
        return
    run_git(["add"] + files, cwd=repo_dir, dry_run=dry_run)


def git_commit(repo_dir: Path, message: str, dry_run: bool = False) -> None:
    run_git(["commit", "-m", message], cwd=repo_dir, dry_run=dry_run)


def git_push_branch(repo_dir: Path, branch: str, dry_run: bool = False) -> None:
    run_git(["push", "-u", "origin", branch], cwd=repo_dir, dry_run=dry_run)


def gh_pr_create(
    repo_dir: Path,
    title: str,
    body: str,
    base: str = "main",
    dry_run: bool = False,
) -> str | None:
    """Create a GitHub PR and return the PR URL."""
    cmd = [
        "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base,
    ]
    result = run_gh(cmd, cwd=repo_dir, dry_run=dry_run)
    if dry_run:
        return "https://github.com/example/pr/123"
    # gh prints the PR URL on success
    url = result.stdout.strip().splitlines()[-1].strip()
    if url.startswith("https://"):
        return url
    # Fallback: try to parse from output
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("https://") and "/pull/" in line:
            return line
    return None


def gh_pr_checks_wait(
    repo_dir: Path,
    branch: str,
    max_wait: int = CI_MAX_WAIT,
    interval: int = CI_POLL_INTERVAL,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Poll GitHub checks for a branch/PR. Returns dict with status info."""
    if dry_run:
        print(f"[DRY-RUN] Would poll checks for branch {branch}")
        return {"state": "SUCCESS", "conclusion": "success", "checks": []}

    start = time.time()
    while time.time() - start < max_wait:
        # List checks for the branch via gh pr checks (requires PR to exist)
        # We use gh pr view to get PR number, then gh pr checks
        result = run_gh(["pr", "view", branch, "--json", "number,url,state"], cwd=repo_dir, check=False)
        if result.returncode != 0:
            print(f"  [INFO] PR not yet found for {branch}, waiting...")
            time.sleep(interval)
            continue

        pr_info = json.loads(result.stdout)
        pr_number = pr_info.get("number")
        if not pr_number:
            time.sleep(interval)
            continue

        checks_result = run_gh(
            ["pr", "checks", str(pr_number), "--json", "name,state,bucket"],
            cwd=repo_dir,
            check=False,
        )
        if checks_result.returncode != 0:
            print(f"  [INFO] Checks not ready yet for PR #{pr_number}, waiting...")
            time.sleep(interval)
            continue

        try:
            checks = json.loads(checks_result.stdout)
        except json.JSONDecodeError:
            checks = []

        # Determine overall status
        pending = [c for c in checks if c.get("state") in ("PENDING", "IN_PROGRESS", "QUEUED")]
        failures = [c for c in checks if c.get("state") == "FAILURE" or c.get("bucket") == "fail"]

        if not pending:
            if failures:
                return {"state": "FAILURE", "conclusion": "failure", "checks": checks}
            return {"state": "SUCCESS", "conclusion": "success", "checks": checks}

        print(f"  [INFO] {len(pending)} check(s) pending for PR #{pr_number}...")
        time.sleep(interval)

    return {"state": "TIMEOUT", "conclusion": "timeout", "checks": []}


def gh_pr_merge(repo_dir: Path, branch: str, squash: bool = True, dry_run: bool = False) -> bool:
    """Merge a PR by branch name."""
    cmd = ["pr", "merge", branch, "--auto"] if not squash else ["pr", "merge", branch, "--squash", "--auto"]
    # Try squash first; if not allowed, fallback merge
    result = run_gh(cmd, cwd=repo_dir, dry_run=dry_run, check=False)
    if result.returncode != 0 and "squash" in str(cmd):
        # Fallback to merge commit
        result = run_gh(["pr", "merge", branch, "--merge", "--auto"], cwd=repo_dir, dry_run=dry_run, check=False)
    if result.returncode != 0 and not dry_run:
        print(f"[ERROR] Failed to merge PR for {branch}: {result.stderr}")
        return False
    return True


# ── IndexNow ─────────────────────────────────────────────────────────────────


# IndexNow keys are 32-char hex strings hosted at https://<host>/<key>.txt.
# These were already provisioned and shipped in each site's
# `src/app/api/indexnow/route.ts` — we reuse the same per-host key so the
# verification file already on production stays valid.
INDEXNOW_KEYS = {
    "www.top-aspirateur.fr": "8f5e86d7c41c4c2d927722a18db3fbaa",
    "www.matelas-expert.fr": "8f5e86d7c41c4c2d927722a18db3fbaa",
    "www.brewmance.fr": "8f5e86d7c41c4c2d927722a18db3fbaa",
    "www.pixinstant.com": "8f5e86d7c41c4c2d927722a18db3fbaa",
    "www.bureau-expert.fr": "8f5e86d7c41c4c2d927722a18db3fbaa",
}


def slugs_to_canonical_urls(site_key: str, files: list[str]) -> list[str]:
    """Map changed MDX/JSON files to the canonical public URLs they affect.

    Heuristic: any `.mdx` under content/ becomes /<type>/<slug>/ on the
    site root, locale-aware. Non-content files (JSON, CSS) are skipped.
    """
    host = PROD_HOSTS.get(site_key)
    if not host:
        return []
    urls: set[str] = set()
    for f in files:
        p = Path(f)
        if p.suffix.lower() != ".mdx":
            continue
        parts = p.parts
        # Detect locale: content-<loc>/ or content/<loc>/  (default locale: fr)
        locale = "fr"
        for part in parts:
            if part.startswith("content-") and len(part) > 8:
                locale = part.split("-", 1)[1]
                break
            if part in {"en", "de", "es", "it", "uk", "ja"} and "content" in parts:
                locale = part
                break
        # Detect type: comparatif | guide | test | avis | pages
        type_map = {
            "comparatifs": "comparatif",
            "guides": "guide",
            "tests": "test",
            "avis": "avis",
            "pages": "",
        }
        article_type = ""
        for part in parts:
            if part in type_map:
                article_type = type_map[part]
                break
        slug = p.stem
        # Build URL — default locale FR has no prefix
        prefix = "" if locale == "fr" else f"/{locale}"
        if article_type:
            url = f"https://{host}{prefix}/{article_type}/{slug}/"
        else:
            url = f"https://{host}{prefix}/{slug}/"
        urls.add(url)
    return sorted(urls)


def ping_indexnow(site_key: str, urls: list[str], dry_run: bool = False) -> dict[str, Any]:
    """Push changed URLs to IndexNow (Bing, Yandex, Seznam, Naver).

    Limit: 10,000 URLs per request. Free, no auth beyond the host-key file
    that already exists on production at /<key>.txt.
    """
    host = PROD_HOSTS.get(site_key)
    key = INDEXNOW_KEYS.get(host or "")
    if not host or not key or not urls:
        return {"status": "skipped", "reason": "no host/key/urls", "site": site_key}
    payload = {
        "host": host,
        "key": key,
        "keyLocation": f"https://{host}/{key}.txt",
        "urlList": urls[:10000],
    }
    if dry_run:
        print(f"  [DRY-RUN] Would POST {len(urls)} URL(s) to IndexNow for {host}")
        return {"status": "dry-run", "host": host, "urls": len(urls)}
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.indexnow.org/indexnow",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="ignore")
        print(f"  📡 IndexNow ping: {host} — {len(urls)} URL(s) — HTTP {status}")
        return {"status": "ok", "http_status": status, "host": host, "urls": len(urls), "body": body[:200]}
    except Exception as exc:
        print(f"  [WARN] IndexNow ping failed for {host}: {exc}")
        return {"status": "error", "error": str(exc), "host": host, "urls": len(urls)}


# ── Event Processing ─────────────────────────────────────────────────────────


def resolve_site_from_event(event: dict[str, Any]) -> dict | None:
    """Determine which site/repo an event belongs to."""
    payload = event.get("payload", {})
    # Try explicit site key (multiple possible field names)
    site_key = payload.get("site") or payload.get("site_key") or payload.get("site_slug")
    if site_key and site_key in SITE_REPOS:
        return SITE_REPOS[site_key]

    # Try to infer from file paths in payload
    for key in ("file_path", "mdx_path", "json_path", "path", "files"):
        val = payload.get(key)
        if not val:
            continue
        if isinstance(val, list):
            paths = val
        else:
            paths = [val]
        for p in paths:
            pobj = Path(p)
            for site_key, info in SITE_REPOS.items():
                try:
                    pobj.relative_to(info["dir"])
                    return info
                except ValueError:
                    pass

    # Try to match by repo URL in payload
    repo_url = payload.get("repo_url") or payload.get("repository")
    if repo_url:
        for site_key, info in SITE_REPOS.items():
            if repo_url in info["remote"] or info["remote"] in repo_url:
                return info

    return None


def bump_parent_submodule_pointer(submodule_dir: Path, dry_run: bool = False) -> dict[str, Any]:
    """After a submodule's PR is merged on origin/main, fast-forward the
    submodule working tree to that SHA and commit/push the parent monorepo's
    pointer bump (P1-12 fix).

    Safe behaviours:
      * NEVER stash other parent-repo changes — only stages the submodule path.
      * Skips silently when the parent repo is not a git repo (legacy layout).
      * Skips when the submodule pointer already matches origin/main.
      * Best-effort push: failures are reported but don't crash the publisher.
    """
    parent = BASE_DIR
    submodule_name = submodule_dir.name
    if dry_run:
        return {"status": "skipped", "reason": "dry_run", "submodule": submodule_name}

    try:
        # Confirm parent is a git repo and submodule is registered.
        parent_check = subprocess.run(
            ["git", "-C", str(parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True,
        )
        if parent_check.returncode != 0 or parent_check.stdout.strip() != "true":
            return {"status": "skipped", "reason": "parent_not_git_repo", "submodule": submodule_name}

        listed = subprocess.run(
            ["git", "-C", str(parent), "submodule", "status", submodule_name],
            capture_output=True, text=True,
        )
        if listed.returncode != 0 or not listed.stdout.strip():
            return {"status": "skipped", "reason": "not_a_submodule", "submodule": submodule_name}

        # Pull main inside the submodule (already merged origin/main).
        subprocess.run(
            ["git", "-C", str(submodule_dir), "fetch", "origin", "main"],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(submodule_dir), "checkout", "main"],
            check=True, capture_output=True, text=True,
        )
        pull = subprocess.run(
            ["git", "-C", str(submodule_dir), "pull", "--ff-only", "origin", "main"],
            capture_output=True, text=True,
        )
        if pull.returncode != 0:
            return {
                "status": "failed", "reason": "submodule_pull_failed",
                "submodule": submodule_name, "stderr": pull.stderr.strip()[:300],
            }

        new_sha = subprocess.run(
            ["git", "-C", str(submodule_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

        # Stage *only* the submodule path — never touch other parent-repo changes.
        add = subprocess.run(
            ["git", "-C", str(parent), "add", submodule_name],
            capture_output=True, text=True,
        )
        if add.returncode != 0:
            return {
                "status": "failed", "reason": "git_add_failed",
                "submodule": submodule_name, "stderr": add.stderr.strip()[:300],
            }

        # Detect "nothing to commit" by checking the staged diff for this path.
        diff_check = subprocess.run(
            ["git", "-C", str(parent), "diff", "--cached", "--quiet", "--", submodule_name],
            capture_output=True, text=True,
        )
        if diff_check.returncode == 0:
            return {"status": "noop", "reason": "pointer_unchanged", "submodule": submodule_name, "sha": new_sha}

        commit_msg = (
            f"chore(submodules): bump {submodule_name} to {new_sha[:8]} "
            f"(auto by agent-publisher)"
        )
        commit = subprocess.run(
            ["git", "-C", str(parent), "commit", "-m", commit_msg, "--only", submodule_name],
            capture_output=True, text=True,
        )
        if commit.returncode != 0:
            # Reset the staged path so we don't poison subsequent commits.
            subprocess.run(
                ["git", "-C", str(parent), "reset", "HEAD", "--", submodule_name],
                capture_output=True, text=True,
            )
            return {
                "status": "failed", "reason": "commit_failed",
                "submodule": submodule_name, "stderr": commit.stderr.strip()[:300],
            }

        push = subprocess.run(
            ["git", "-C", str(parent), "push", "origin", "HEAD:main"],
            capture_output=True, text=True,
        )
        if push.returncode != 0:
            return {
                "status": "committed_not_pushed", "submodule": submodule_name,
                "sha": new_sha, "stderr": push.stderr.strip()[:300],
            }
        return {"status": "ok", "submodule": submodule_name, "sha": new_sha}
    except subprocess.CalledProcessError as exc:
        return {
            "status": "failed", "reason": "subprocess_error",
            "submodule": submodule_name,
            "stderr": (exc.stderr or "").strip()[:300] if hasattr(exc, "stderr") else str(exc)[:300],
        }
    except Exception as exc:
        return {"status": "failed", "reason": "unexpected", "submodule": submodule_name, "error": str(exc)[:300]}


def make_branch_name(event: dict[str, Any]) -> str:
    """Generate a branch name like hermes/<timestamp>-<article-slug>."""
    payload = event.get("payload", {})
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = payload.get("slug") or payload.get("article_slug") or payload.get("asin") or "change"
    # Sanitize slug
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(slug)).strip("-").lower()[:40]
    return f"hermes/{ts}-{slug}"


def make_commit_message(event: dict[str, Any], files: list[str]) -> str:
    """Generate a meaningful commit message from event metadata."""
    event_type = event.get("type", "unknown")
    payload = event.get("payload", {})
    slug = payload.get("slug") or payload.get("article_slug") or payload.get("asin") or "update"

    if event_type == "content.written":
        title = payload.get("title") or slug
        return f"content: add/update {title}"
    elif event_type == "price.asin_replaced":
        old_asin = payload.get("old_asin", "?")
        new_asin = payload.get("new_asin", "?")
        return f"price: replace ASIN {old_asin} -> {new_asin}"
    elif event_type == "seo.fix_applied":
        ft = payload.get("fix_type", "seo")
        shown = ", ".join(files[:3]) + ("…" if len(files) > 3 else "")
        return f"seo: {ft} ({shown})"
    else:
        return f"hermes: apply {event_type} for {slug}"


def make_pr_body(event: dict[str, Any], files: list[str]) -> str:
    """Generate a PR body with context."""
    event_type = event.get("type", "unknown")
    payload = event.get("payload", {})
    lines = [
        f"**Event type:** `{event_type}`",
        f"**Event ID:** {event.get('id', 'n/a')}",
        "",
        "**Files changed:**",
    ]
    for f in files:
        lines.append(f"- `{f}`")
    lines.append("")
    if payload:
        lines.append("**Payload:**")
        lines.append(f"```json\n{json.dumps(payload, indent=2, ensure_ascii=False)}\n```")
    return "\n".join(lines)


def process_rollback_request(event: dict[str, Any], dry_run: bool = False) -> bool:
    """Handle deployment.rollback_requested events from agent-canary.

    Strategy: identify the most recent commit on the affected submodule
    that touched any of the offending files, create a `git revert` PR
    on that submodule, and leave it OPEN for human review (no auto-merge
    on rollbacks — the canary signal could be noisy).
    """
    event_id = event.get("id", "no-id")
    payload = event.get("payload", {}) or {}
    site = payload.get("site")
    files: list[str] = payload.get("files") or []
    site_info = SITE_REPOS.get(site)
    if not site_info:
        print(f"[ERROR] Rollback: unknown site '{site}'.")
        return False
    repo_dir = site_info["dir"]
    if not files:
        print(f"[WARN] Rollback: no files in payload for {event_id}.")
        return False
    print(f"\n▶ Rollback request {event_id} for {site} (drop {payload.get('click_drop_pct','?')}%)")

    if dry_run:
        print(f"  [DRY-RUN] Would identify the bad commit on {repo_dir.name} touching {files} and open a revert PR.")
        return True

    try:
        git_ensure_clean_main(repo_dir, dry_run=False)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Could not prepare {repo_dir.name} for rollback: {exc.stderr}")
        return False

    file_args = [f for f in files if f]
    log = subprocess.run(
        ["git", "-C", str(repo_dir), "log", "-n", "1", "--format=%H", "--", *file_args],
        capture_output=True, text=True,
    )
    bad_sha = log.stdout.strip()
    if not bad_sha:
        print(f"[WARN] Rollback: no commit found touching {file_args}; skipping.")
        return False

    branch = f"hermes/rollback-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{bad_sha[:8]}"
    try:
        git_create_branch(repo_dir, branch, dry_run=False)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Could not create rollback branch: {exc.stderr}")
        return False

    revert = subprocess.run(
        ["git", "-C", str(repo_dir), "revert", "--no-edit", bad_sha],
        capture_output=True, text=True,
    )
    if revert.returncode != 0:
        print(f"[ERROR] git revert failed (likely conflicts): {revert.stderr.strip()[:300]}")
        subprocess.run(["git", "-C", str(repo_dir), "revert", "--abort"], capture_output=True, text=True)
        return False

    push = subprocess.run(
        ["git", "-C", str(repo_dir), "push", "-u", "origin", branch],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        print(f"[ERROR] Push failed for rollback branch: {push.stderr.strip()[:300]}")
        return False

    pr_body = (
        f"Auto-rollback proposed by `agent-canary`.\n\n"
        f"- Site: {site}\n"
        f"- Bad commit: {bad_sha}\n"
        f"- Click drop: {payload.get('click_drop_pct','?')}%\n"
        f"- Impression drop: {payload.get('impression_drop_pct','?')}%\n"
        f"- Baseline clicks (7d): {payload.get('baseline_clicks_7d','?')}\n"
        f"- Current clicks (7d): {payload.get('current_clicks_7d','?')}\n"
        f"- Affected URLs: {', '.join(payload.get('affected_urls', [])[:5])}\n\n"
        f"**Manual review required — do not auto-merge.**"
    )
    pr_create = subprocess.run(
        ["gh", "pr", "create", "--title", f"rollback: {bad_sha[:8]} (canary regression)",
         "--body", pr_body, "--base", "main", "--head", branch],
        cwd=str(repo_dir), capture_output=True, text=True,
    )
    if pr_create.returncode != 0:
        print(f"[ERROR] gh pr create failed: {pr_create.stderr.strip()[:300]}")
        return False
    pr_url = pr_create.stdout.strip().split("\n")[-1]
    print(f"  ✅ Rollback PR opened: {pr_url}")
    emit_event(
        "deployment.rollback_proposed",
        {"site": site, "bad_sha": bad_sha, "pr_url": pr_url, "canary_payload": payload},
        priority=1, source="agent-publisher",
    )
    git_ensure_clean_main(repo_dir, dry_run=False)
    return True


def collect_repo_relative_paths(repo_dir: Path, changed_files: list[str]) -> list[str]:
    """Normalize paths to be relative to repo_dir (same rules as process_event)."""
    repo_relative_files: list[str] = []
    for f in changed_files:
        if not f:
            continue
        p = Path(f)
        if p.is_absolute():
            try:
                repo_relative_files.append(str(p.relative_to(repo_dir)))
            except ValueError:
                print(f"[WARN] File {f} is not under repo {repo_dir}, skipping")
        else:
            resolved = BASE_DIR / f
            if resolved.exists():
                try:
                    repo_relative_files.append(str(resolved.relative_to(repo_dir)))
                except ValueError:
                    repo_relative_files.append(str(f))
            else:
                alt = repo_dir / f
                if alt.exists():
                    repo_relative_files.append(str(f))
                else:
                    repo_relative_files.append(str(f))
    return repo_relative_files


def process_pr_from_existing_branch(
    event: dict[str, Any],
    repo_dir: Path,
    branch: str,
    changed_files: list[str],
    dry_run: bool = False,
    auto_merge: bool = True,
) -> bool:
    """Push a branch another agent already committed to, open PR, merge, IndexNow.

    Used by agent-translator (`use_existing_branch`) and for any agent that
    prepares work locally then hands off to the publisher.
    """
    event_id = event.get("id", "no-id")
    event_type = event.get("type", "unknown")
    payload = event.get("payload", {})

    print(f"  Existing-branch workflow: `{branch}` (files for IndexNow: {changed_files})")

    if dry_run:
        print(f"  [DRY-RUN] Would checkout {branch}, push, open PR, poll CI")
        return True

    try:
        run_git(["fetch", "origin"], cwd=repo_dir, dry_run=False, check=False)
    except subprocess.CalledProcessError:
        pass

    co = run_git(["checkout", branch], cwd=repo_dir, check=False)
    if co.returncode != 0:
        co2 = run_git(["checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, check=False)
        if co2.returncode != 0:
            print(f"[ERROR] Cannot checkout branch {branch}.\n{co.stderr}\n{co2.stderr}")
            try:
                git_ensure_clean_main(repo_dir, dry_run=False)
            except Exception:
                pass
            return False

    try:
        git_push_branch(repo_dir, branch, dry_run=False)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Push failed for {branch}: {exc.stderr}")
        try:
            git_ensure_clean_main(repo_dir, dry_run=False)
        except Exception:
            pass
        return False

    pr_title = (payload.get("summary") or make_commit_message(event, changed_files))[:120]
    pr_body = make_pr_body(event, changed_files)
    try:
        pr_url = gh_pr_create(repo_dir, title=pr_title, body=pr_body, dry_run=False)
        if pr_url:
            print(f"  PR opened: {pr_url}")
        else:
            print(f"[WARN] PR created but URL not parsed.")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] PR creation failed: {exc.stderr}")
        try:
            git_ensure_clean_main(repo_dir, dry_run=False)
        except Exception:
            pass
        return False

    print(f"  Monitoring CI checks...")
    checks = gh_pr_checks_wait(repo_dir, branch, dry_run=False)
    conclusion = checks.get("conclusion", "unknown")
    print(f"  Checks conclusion: {conclusion}")

    if conclusion == "success":
        if auto_merge and payload.get("auto_merge", True):
            print(f"  Auto-merging PR...")
            merged = gh_pr_merge(repo_dir, branch, dry_run=False)
            if merged:
                print(f"  Merged and deployed (git push triggers build).")
            else:
                print(f"[WARN] Merge failed; PR remains open for manual review.")
        else:
            print(f"  Auto-merge disabled; PR remains open.")
    elif conclusion == "failure":
        print(f"[WARN] Checks failed for PR. Leaving open for manual review.")
    elif conclusion == "timeout":
        print(f"[WARN] CI check polling timed out. Leaving PR open.")
    else:
        print(f"[INFO] CI status: {conclusion}. Leaving PR open.")

    merged = conclusion == "success" and auto_merge and payload.get("auto_merge", True)
    indexnow_result: dict[str, Any] = {"status": "skipped", "reason": "not merged"}
    if merged:
        site_key = repo_dir.name
        canonical_urls = slugs_to_canonical_urls(site_key, changed_files)
        if canonical_urls:
            indexnow_result = ping_indexnow(site_key, canonical_urls, dry_run=False)

    submodule_bump: dict[str, Any] = {"status": "skipped", "reason": "not merged"}
    if merged:
        submodule_bump = bump_parent_submodule_pointer(repo_dir, dry_run=False)

    deploy_payload = {
        "original_event_id": event_id,
        "original_event_type": event_type,
        "site": repo_dir.name,
        "branch": branch,
        "pr_url": pr_url,
        "checks_conclusion": conclusion,
        "merged": merged,
        "files": changed_files,
        "indexnow": indexnow_result,
        "submodule_bump": submodule_bump,
        "workflow": "existing_branch",
    }
    emit_event("deployment.completed", deploy_payload, priority=2, source="agent-publisher")

    try:
        git_ensure_clean_main(repo_dir, dry_run=False)
    except subprocess.CalledProcessError as exc:
        print(f"[WARN] Failed to return to main after processing: {exc.stderr}")

    return True


def process_event(event: dict[str, Any], dry_run: bool = False, auto_merge: bool = True) -> bool:
    """Process a single event end-to-end. Returns True on success."""
    event_id = event.get("id", "no-id")
    event_type = event.get("type", "unknown")
    print(f"\n▶ Processing event {event_id} ({event_type})")

    if event_type == "deployment.rollback_requested":
        return process_rollback_request(event, dry_run=dry_run)

    # 1. Resolve site/repo
    site_info = resolve_site_from_event(event)
    if not site_info:
        print(f"[ERROR] Could not resolve site for event {event_id}. Failing.")
        return False

    repo_dir = site_info["dir"]
    print(f"  Repo: {repo_dir.name} ({site_info['remote']})")

    payload = event.get("payload", {})
    # Branch prepared by another agent (e.g. translator) — push, open PR, merge.
    if event_type == "seo.fix_applied" and payload.get("use_existing_branch"):
        br = (payload.get("branch") or payload.get("branch_name") or "").strip()
        raw_files: list[str] = []
        fp = payload.get("files")
        if isinstance(fp, list):
            raw_files = [str(x) for x in fp if x]
        elif isinstance(fp, str) and fp:
            raw_files = [fp]
        elif isinstance(fp, dict):
            raw_files = list(fp.keys())
        changed_norm = collect_repo_relative_paths(repo_dir, raw_files)
        if not br:
            print(f"[ERROR] use_existing_branch set but payload.branch is empty for {event_id}.")
            return False
        if not changed_norm:
            print(f"[ERROR] use_existing_branch requires resolvable files; got {raw_files!r}.")
            return False
        return process_pr_from_existing_branch(
            event, repo_dir, br, changed_norm, dry_run=dry_run, auto_merge=auto_merge
        )

    # 2. Ensure clean main
    try:
        git_ensure_clean_main(repo_dir, dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Failed to clean/checkout main in {repo_dir.name}: {exc.stderr}")
        return False

    # 3. Create branch
    branch = make_branch_name(event)
    print(f"  Branch: {branch}")
    try:
        git_create_branch(repo_dir, branch, dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Failed to create branch {branch}: {exc.stderr}")
        return False

    # 4. Apply file changes
    payload = event.get("payload", {})
    changed_files: list[str] = []

    # Support explicit file content in payload
    files_payload = payload.get("files") or payload.get("file_path")
    if isinstance(files_payload, dict):
        # { "relative/path": "content" }
        for rel_path, content in files_payload.items():
            target_path = repo_dir / rel_path
            if dry_run:
                print(f"[DRY-RUN] Would write {target_path}")
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(content, encoding="utf-8")
            changed_files.append(rel_path)
    elif isinstance(files_payload, list):
        # List of relative paths already modified on disk (by another agent)
        for rel_path in files_payload:
            changed_files.append(rel_path)
    elif isinstance(files_payload, str):
        changed_files.append(files_payload)

    # Also support direct mdx_path / json_path / path keys
    for key in ("mdx_path", "json_path", "path"):
        val = payload.get(key)
        if val and val not in changed_files:
            changed_files.append(val)
    
    # Convert paths to be relative to repo_dir if they're absolute or relative to BASE_DIR
    repo_relative_files = []
    for f in changed_files:
        p = Path(f)
        if p.is_absolute():
            # Absolute path: make relative to repo_dir
            try:
                rel = p.relative_to(repo_dir)
                repo_relative_files.append(str(rel))
            except ValueError:
                # Not under repo_dir, skip
                print(f"[WARN] File {f} is not under repo {repo_dir}, skipping")
        else:
            # Relative path - check if it starts with site slug
            resolved = BASE_DIR / f
            if resolved.exists():
                # It's relative to BASE_DIR, convert to repo-relative
                try:
                    rel = resolved.relative_to(repo_dir)
                    repo_relative_files.append(str(rel))
                except ValueError:
                    repo_relative_files.append(str(f))
            else:
                repo_relative_files.append(str(f))
    
    changed_files = repo_relative_files

    if not changed_files:
        print(f"[WARN] No files to commit for event {event_id}. Aborting branch.")
        try:
            git_ensure_clean_main(repo_dir, dry_run=dry_run)
        except Exception:
            pass
        return False

    # 5. Stage and commit
    try:
        git_add_files(repo_dir, changed_files, dry_run=dry_run)
        # Verify something is staged
        staged = git_staged_files(repo_dir)
        if not staged and not dry_run:
            # No diff detected — content may be identical to HEAD
            # This is OK for content refresh events where the writer kept similar content
            print(f"  [INFO] No diff detected — content unchanged or identical to HEAD. Skipping commit.")
            # Still emit deployment.completed since the content was reviewed and approved
            # Get site key from payload for the event
            event_site_key = payload.get("site_slug") or payload.get("site") or payload.get("site_key") or "unknown"
            emit_event(
                "deployment.completed",
                {
                    "site_slug": event_site_key,
                    "article_slug": payload.get("article_slug", ""),
                    "branch": branch,
                    "pr_url": None,
                    "status": "skipped_no_diff",
                    "reason": "Content unchanged after refresh",
                },
                priority=2,
            )
            # Clean up branch
            try:
                git_ensure_clean_main(repo_dir, dry_run=dry_run)
            except Exception:
                pass
            return True
        commit_msg = make_commit_message(event, changed_files)
        git_commit(repo_dir, commit_msg, dry_run=dry_run)
        print(f"  Committed: {commit_msg}")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Git commit failed: {exc.stderr}")
        try:
            git_ensure_clean_main(repo_dir, dry_run=dry_run)
        except Exception:
            pass
        return False

    # 6. Push branch
    try:
        git_push_branch(repo_dir, branch, dry_run=dry_run)
        print(f"  Pushed: {branch}")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] Git push failed: {exc.stderr}")
        return False

    # 7. Open PR
    pr_title = make_commit_message(event, changed_files)
    pr_body = make_pr_body(event, changed_files)
    try:
        pr_url = gh_pr_create(repo_dir, title=pr_title, body=pr_body, dry_run=dry_run)
        if pr_url:
            print(f"  PR opened: {pr_url}")
        else:
            print(f"[WARN] PR created but URL not parsed.")
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] PR creation failed: {exc.stderr}")
        return False

    # 8. Monitor CI / checks
    print(f"  Monitoring CI checks...")
    checks = gh_pr_checks_wait(repo_dir, branch, dry_run=dry_run)
    conclusion = checks.get("conclusion", "unknown")
    print(f"  Checks conclusion: {conclusion}")

    if conclusion == "success":
        if auto_merge:
            print(f"  Auto-merging PR...")
            merged = gh_pr_merge(repo_dir, branch, dry_run=dry_run)
            if merged:
                print(f"  Merged and deployed (git push triggers build).")
            else:
                print(f"[WARN] Merge failed; PR remains open for manual review.")
                # Still consider processing successful since PR is open
        else:
            print(f"  Auto-merge disabled; PR remains open.")
    elif conclusion == "failure":
        print(f"[WARN] Checks failed for PR. Leaving open for manual review.")
    elif conclusion == "timeout":
        print(f"[WARN] CI check polling timed out. Leaving PR open.")
    else:
        print(f"[INFO] CI status: {conclusion}. Leaving PR open.")

    # 9. Ping IndexNow for changed pages (only when we actually merged to main).
    merged = conclusion == "success" and auto_merge
    indexnow_result: dict[str, Any] = {"status": "skipped", "reason": "not merged"}
    if merged:
        site_key = repo_dir.name
        canonical_urls = slugs_to_canonical_urls(site_key, changed_files)
        if canonical_urls:
            indexnow_result = ping_indexnow(site_key, canonical_urls, dry_run=dry_run)

    # 9b. Bump the parent monorepo submodule pointer so the deployed SHA is
    # recorded in `affiliation-sites` itself (P1-12 fix). Without this, the
    # parent repo's submodule pointer drifts forever and `git status` from the
    # parent always shows "modified content".
    submodule_bump: dict[str, Any] = {"status": "skipped", "reason": "not merged"}
    if merged:
        submodule_bump = bump_parent_submodule_pointer(repo_dir, dry_run=dry_run)

    # 10. Emit deployment.completed event
    deploy_payload = {
        "original_event_id": event_id,
        "original_event_type": event_type,
        "site": repo_dir.name,
        "branch": branch,
        "pr_url": pr_url,
        "checks_conclusion": conclusion,
        "merged": merged,
        "files": changed_files,
        "indexnow": indexnow_result,
        "submodule_bump": submodule_bump,
    }
    emit_event("deployment.completed", deploy_payload, priority=2, source="agent-publisher")

    # 11. Cleanup: return to main so repo is ready for next event
    try:
        git_ensure_clean_main(repo_dir, dry_run=dry_run)
    except subprocess.CalledProcessError as exc:
        print(f"[WARN] Failed to return to main after processing: {exc.stderr}")

    return True


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hermes Agent Publisher — consumes content.written and price.asin_replaced events, "
                    "creates PRs, monitors CI, merges on green, and emits deployment.completed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --consume --limit 10
  %(prog)s --consume --limit 10 --dry-run
  %(prog)s --consume --limit 1 --dry-run --no-auto-merge
""",
    )
    parser.add_argument("--consume", action="store_true", help="Consume events from inbox and process them")
    parser.add_argument("--limit", type=int, default=5, help="Max events to process (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without executing")
    parser.add_argument("--no-auto-merge", action="store_true", help="Do not auto-merge PRs even if checks pass")

    args = parser.parse_args()

    if not args.consume:
        parser.print_help()
        return 0

    ensure_dirs()
    files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )

    processed = 0
    succeeded = 0
    failed = 0

    for path in files:
        if processed >= args.limit:
            break

        event = read_event(path)
        if event is None:
            move_event(path, FAILED_DIR, dry_run=args.dry_run)
            failed += 1
            continue

        event_type = event.get("type", "")
        if event_type not in SUPPORTED_TYPES:
            # Skip unsupported events silently (leave in inbox for other agents)
            continue

        # Route to processing
        processing_path = move_event(path, PROCESSING_DIR, dry_run=args.dry_run)
        if processing_path is None:
            failed += 1
            continue
        event["_file_path"] = str(processing_path)

        auto_merge = not args.no_auto_merge
        ok = process_event(event, dry_run=args.dry_run, auto_merge=auto_merge)
        if ok:
            complete_event(event, dry_run=args.dry_run)
            succeeded += 1
        else:
            fail_event(event, dry_run=args.dry_run)
            failed += 1

        processed += 1

    print(f"\n[SUMMARY] Processed {processed} event(s): {succeeded} succeeded, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

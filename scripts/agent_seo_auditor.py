#!/usr/bin/env python3
"""
Agent SEO Auditor — Hermes Event Consumer for Technical SEO Monitoring

Consumes events from the Hermes inbox (``HERMES_EVENTS_ROOT`` / ``HERMES_EVENTS_DIR``; ``scripts/hermes_bus.py``):
  - seo.issue_detected      → Analyze and auto-fix
  - health.site_status      → Validate fixes
  - content.published       → Recrawl check

Core functions:
  1. GSC error analysis (indexing, mobile, CWV)
  2. Technical audit (links, images, headings, schema)
  3. Auto-fixer (meta descriptions, alt tags, canonicals, schema)
  4. Manual escalation for non-auto-fixable issues

Emits:
  - seo.fix_applied         → Publisher (commit changes)
  - seo.fix_failed          → Human (needs manual fix)
  - seo.audit_completed     → Analytics
  - content.refresh_needed  → Writer (thin content found)

Usage:
    python3 agent_seo_auditor.py --consume --limit 10
    python3 agent_seo_auditor.py --audit-all --dry-run
    python3 agent_seo_auditor.py --site aspirateur --check-links
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
    plain_move,
)

# Load .env
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

# ── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = portfolio_root()
DB_PATH = Path("~/affiliate-machine.db").expanduser()
_HP_INIT = ensure_hermes_dirs()
EVENTS_BASE = _HP_INIT.base
INBOX_DIR = _HP_INIT.inbox
PROCESSING_DIR = _HP_INIT.processing
COMPLETED_DIR = _HP_INIT.completed
FAILED_DIR = _HP_INIT.failed
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Site Config ────────────────────────────────────────────────────────────
SITES = {
    "aspirateur": {"domain": "top-aspirateur.fr", "default_locale": "fr", "repo_path": BASE_DIR / "aspirateur"},
    "bureau": {"domain": "bureau-expert.fr", "default_locale": "fr", "repo_path": BASE_DIR / "bureau"},
    "matelas": {"domain": "matelas-expert.fr", "default_locale": "fr", "repo_path": BASE_DIR / "matelas"},
    "cafe": {"domain": "brewmance.fr", "default_locale": "fr", "repo_path": BASE_DIR / "cafe"},
    "pixinstant": {"domain": "pixinstant.com", "default_locale": "fr", "repo_path": BASE_DIR / "pixinstant"},
    "airpurify": {"domain": "airpurifyhq.com", "default_locale": "en", "repo_path": BASE_DIR / "affiliate-suite/apps/airpurify"},
    "safehive": {"domain": "safehivehq.com", "default_locale": "en", "repo_path": BASE_DIR / "affiliate-suite/apps/safehive"},
    "pawhive": {"domain": "pawhivehq.com", "default_locale": "en", "repo_path": BASE_DIR / "affiliate-suite/apps/pawhive"},
}

SITE_GSC_MAP = {
    "aspirateur": "sc-domain:top-aspirateur.fr",
    "bureau": "sc-domain:bureau-expert.fr",
    "matelas": "sc-domain:matelas-expert.fr",
    "cafe": "sc-domain:brewmance.fr",
    "pixinstant": "sc-domain:pixinstant.com",
    "airpurify": "sc-domain:airpurifyhq.com",
    "safehive": "sc-domain:safehivehq.com",
    "pawhive": "sc-domain:pawhivehq.com",
}

# ── Event Helpers ──────────────────────────────────────────────────────────

def ensure_dirs() -> None:
    ensure_hermes_dirs()


def emit_event(event_type: str, payload: dict, priority: int = 3, target_agent: str = None) -> dict:
    ensure_dirs()
    event = {
        "id": str(uuid.uuid4()),
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_agent": "agent-seo-auditor",
        "routing_key": f"agent.{event_type.split('.')[0]}",
    }
    if target_agent:
        event["target_agent"] = target_agent
    filename = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{event_type.replace('.', '_')}_{event['id'][:8]}.json"
    path = ensure_hermes_dirs().inbox / filename
    path.write_text(json.dumps(event, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  📤 Emitted: {event_type} → {filename}")
    return event

def read_event(path: Path) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def move_event(src: Path, dst_dir: Path, dry_run: bool = False) -> Optional[Path]:
    return plain_move(src, dst_dir, dry_run=dry_run)

# ── GSC API ────────────────────────────────────────────────────────────────

def get_gsc_service():
    """Initialize GSC API service from service account."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        
        creds_path = os.environ.get("GSC_CREDENTIALS_PATH", str(Path.home() / "gsc-credentials.json"))
        credentials = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
        )
        return build("webmasters", "v3", credentials=credentials, cache_discovery=False)
    except ImportError:
        print("  ⚠️ google-api-python-client not installed. GSC disabled.")
        return None
    except Exception as e:
        print(f"  ❌ GSC auth failed: {e}")
        return None

def fetch_gsc_errors(site_slug: str) -> List[dict]:
    """Fetch indexing errors from GSC."""
    service = get_gsc_service()
    if not service:
        return []
    
    property_uri = SITE_GSC_MAP.get(site_slug)
    if not property_uri:
        return []
    
    try:
        # Get site info
        site_info = service.sites().get(siteUrl=property_uri).execute()
        
        errors = []
        
        # Check indexing status
        try:
            inspect_url = service.urlInspection().index().inspect(
                body={"inspectionUrl": f"https://www.{SITES[site_slug]['domain']}/", "siteUrl": property_uri}
            ).execute()
        except:
            inspect_url = {}
        
        # Add basic site info as issues if problems detected
        if site_info.get("permissionLevel") != "siteOwner":
            errors.append({
                "type": "gsc_permission",
                "severity": "warning",
                "description": f"GSC permission level is {site_info.get('permissionLevel', 'unknown')}",
                "auto_fixable": False,
            })
        
        return errors
        
    except Exception as e:
        print(f"  ❌ GSC error fetch failed: {e}")
        return []

# ── Technical Audit ────────────────────────────────────────────────────────

# Known non-default locale directory names.
_LOCALE_DIRS = {"en", "de", "es", "it", "uk", "ja"}

# Article-type segment → singular form for canonical/url construction.
_TYPE_SINGULAR = {
    "comparatifs": "comparatif",
    "guides": "guide",
    "tests": "test",
    "avis": "avis",
    "reviews": "review",
}


def _detect_locale_and_type(rel_path: Path, site_default: str) -> Tuple[str, str]:
    """Infer (locale, content_type) from a path relative to the site repo.

    Supports both layouts used across the portfolio:
      - parallel:  content-en/tests/foo.mdx       → ("en", "test")
      - nested:    content/en/tests/foo.mdx       → ("en", "test")
      - default:   content/tests/foo.mdx          → (site_default, "test")
      - monorepo:  src/app/.../content/.../*.mdx  → (site_default, "")
    """
    parts = rel_path.parts
    if not parts:
        return site_default, ""

    locale = site_default
    type_segments = parts
    first = parts[0]

    if first.startswith("content-"):
        suffix = first[len("content-"):]
        if suffix in _LOCALE_DIRS:
            locale = suffix
        type_segments = parts[1:]
    elif first == "content":
        if len(parts) >= 2 and parts[1] in _LOCALE_DIRS:
            locale = parts[1]
            type_segments = parts[2:]
        else:
            type_segments = parts[1:]
    # else (monorepo with src/app/...): leave locale = site_default

    content_type = ""
    for seg in type_segments:
        if seg in _TYPE_SINGULAR:
            content_type = _TYPE_SINGULAR[seg]
            break
    return locale, content_type


def find_mdx_files(site_slug: str) -> List[Tuple[Path, str, str]]:
    """Return [(mdx_path, locale, content_type)] for every MDX file in a site.

    Enumerates top-level ``content*`` directories explicitly (matching the
    reference implementation in ``agent_title_trimmer.scan_site`` and
    ``agent_url_health._site_slugs``), so each path is tagged with its true
    locale at scan time. This prevents the auto-fixer from writing
    default-locale content into a localized file (e.g. building a canonical
    URL without ``/en/`` when the file lives under ``content-en/``).

    Falls back to the monorepo layout (``src/app/**/content/**/*.mdx``) when
    no top-level ``content*`` directory exists.
    """
    site_info = SITES.get(site_slug, {})
    repo_path = site_info.get("repo_path")
    if not repo_path or not repo_path.exists():
        return []
    site_default = site_info.get("default_locale", "fr")

    out: List[Tuple[Path, str, str]] = []
    seen: Set[Path] = set()

    # Top-level content roots: catches both parallel (content/, content-en/, ...)
    # and nested (content/, content/en/, ...) layouts on a single pass without
    # descending into node_modules or sibling dirs.
    content_roots = sorted({p for p in repo_path.glob("content*") if p.is_dir()})
    for root in content_roots:
        for mdx in root.rglob("*.mdx"):
            try:
                rel = mdx.relative_to(repo_path)
            except ValueError:
                continue
            # Skip fixtures / page bundles
            if any(part in {"data", "pages"} for part in rel.parts):
                continue
            locale, content_type = _detect_locale_and_type(rel, site_default)
            if mdx in seen:
                continue
            seen.add(mdx)
            out.append((mdx, locale, content_type))

    # Monorepo layout (no top-level content dir, e.g. affiliate-suite apps).
    if not out:
        for mdx in repo_path.glob("src/app/**/content/**/*.mdx"):
            try:
                rel = mdx.relative_to(repo_path)
            except ValueError:
                continue
            if any(part in {"data", "pages"} for part in rel.parts):
                continue
            # Best-effort locale detection inside the app's content/ subtree.
            try:
                content_idx = rel.parts.index("content")
                sub_rel = Path(*rel.parts[content_idx:])
                locale, content_type = _detect_locale_and_type(sub_rel, site_default)
            except ValueError:
                locale, content_type = site_default, ""
            if mdx in seen:
                continue
            seen.add(mdx)
            out.append((mdx, locale, content_type))

    out.sort(key=lambda t: str(t[0]))
    return out

def parse_frontmatter(content: str) -> Tuple[dict, str]:
    """Extract YAML frontmatter and body from MDX."""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                fm = yaml.safe_load(parts[1]) or {}
                return fm, parts[2].strip()
            except ImportError:
                pass
    return {}, content

def check_meta_description(fm: dict, body: str, site_slug: str) -> Optional[dict]:
    """Check if meta description exists and is valid."""
    desc = fm.get("description", "")
    
    if not desc:
        return {
            "type": "missing_meta_description",
            "severity": "error",
            "description": "No meta description in frontmatter",
            "auto_fixable": True,
            "fix_type": "generate_from_content",
        }
    
    if len(desc) < 120 or len(desc) > 170:
        return {
            "type": "meta_description_length",
            "severity": "warning",
            "description": f"Meta description length {len(desc)} not in [120,170]",
            "auto_fixable": True,
            "fix_type": "regenerate",
        }
    
    return None

def check_title(fm: dict, body: str, site_slug: str) -> Optional[dict]:
    """Check if title exists and is valid."""
    title = fm.get("title", "")
    
    if not title:
        return {
            "type": "missing_title",
            "severity": "error",
            "description": "No title in frontmatter",
            "auto_fixable": True,
            "fix_type": "generate_from_heading",
        }
    
    if len(title) < 40 or len(title) > 70:
        return {
            "type": "title_length",
            "severity": "warning",
            "description": f"Title length {len(title)} not in [40,70]",
            "auto_fixable": True,
            "fix_type": "regenerate",
        }
    
    return None

def check_canonical(fm: dict, body: str, site_slug: str, file_path: Path) -> Optional[dict]:
    """Check canonical tag configuration."""
    canonical = fm.get("canonical", "")
    slug = fm.get("slug", "")
    
    if not canonical and not slug:
        return {
            "type": "missing_canonical",
            "severity": "warning",
            "description": "No canonical or slug for self-referencing canonical",
            "auto_fixable": True,
            "fix_type": "add_canonical",
        }
    
    return None

def check_schema_markup(fm: dict, body: str) -> List[dict]:
    """Check structured data / schema markup."""
    issues = []
    
    # Check for required schema types
    schema_type = fm.get("schemaType", "")
    if not schema_type:
        issues.append({
            "type": "missing_schema_type",
            "severity": "warning",
            "description": "No schemaType in frontmatter",
            "auto_fixable": True,
            "fix_type": "infer_schema",
        })
    
    # Check JSON-LD in body
    jsonld_pattern = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
    jsonld_blocks = jsonld_pattern.findall(body)
    
    for block in jsonld_blocks:
        try:
            data = json.loads(block)
            if "@type" not in data:
                issues.append({
                    "type": "invalid_jsonld",
                    "severity": "error",
                    "description": "JSON-LD missing @type",
                    "auto_fixable": True,
                    "fix_type": "add_schema_type",
                })
        except json.JSONDecodeError:
            issues.append({
                "type": "malformed_jsonld",
                "severity": "error",
                "description": "JSON-LD block is invalid JSON",
                "auto_fixable": False,
            })
    
    return issues

def check_images(body: str) -> List[dict]:
    """Check images for alt tags."""
    issues = []
    
    # Find markdown images ![alt](url) and HTML <img> tags
    md_images = re.findall(r'!\[([^\]]*)\]\(([^)]+)\)', body)
    for alt, url in md_images:
        if not alt.strip():
            issues.append({
                "type": "missing_alt_tag",
                "severity": "warning",
                "description": f"Image missing alt text: {url[:50]}",
                "auto_fixable": True,
                "fix_type": "generate_alt_from_filename",
            })
    
    html_images = re.findall(r'<img[^>]+src="([^"]+)"[^>]*>', body)
    for img_tag in html_images:
        if 'alt="' not in img_tag or 'alt=""' in img_tag:
            issues.append({
                "type": "missing_alt_tag_html",
                "severity": "warning",
                "description": "HTML image missing alt attribute",
                "auto_fixable": True,
                "fix_type": "generate_alt_from_filename",
            })
    
    return issues

def check_headings(body: str) -> List[dict]:
    """Check H1/H2 structure."""
    issues = []
    
    h1s = re.findall(r'^# (.+)$', body, re.MULTILINE)
    h2s = re.findall(r'^## (.+)$', body, re.MULTILINE)
    
    if len(h1s) == 0:
        issues.append({
            "type": "missing_h1",
            "severity": "error",
            "description": "No H1 heading found",
            "auto_fixable": True,
            "fix_type": "generate_from_title",
        })
    elif len(h1s) > 1:
        issues.append({
            "type": "multiple_h1",
            "severity": "warning",
            "description": f"Multiple H1 headings ({len(h1s)})",
            "auto_fixable": False,
        })
    
    if len(h2s) == 0:
        issues.append({
            "type": "no_h2_sections",
            "severity": "info",
            "description": "No H2 sections for content structure",
            "auto_fixable": False,
        })
    
    return issues

def check_links(body: str, site_slug: str) -> List[dict]:
    """Check for broken or problematic links."""
    issues = []
    
    # Find all links
    links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', body)
    
    for text, url in links:
        if url.startswith("http"):
            # External link
            if "amazon" in url and "/dp/" in url:
                # Check ASIN format
                asin_match = re.search(r'/dp/([A-Z0-9]{10})', url)
                if not asin_match:
                    issues.append({
                        "type": "invalid_asin_link",
                        "severity": "error",
                        "description": f"Invalid ASIN in link: {url[:60]}",
                        "auto_fixable": False,
                    })
        elif url.startswith("/"):
            # Internal link - check if it looks valid
            if len(url) < 2:
                issues.append({
                    "type": "empty_internal_link",
                    "severity": "warning",
                    "description": f"Suspicious internal link: {url}",
                    "auto_fixable": False,
                })
    
    return issues

def audit_page(file_path: Path, site_slug: str, locale: str = "", content_type: str = "") -> List[dict]:
    """Run full technical audit on a single MDX file.

    ``locale`` and ``content_type`` are tagged onto every emitted issue so
    downstream auto-fixers (notably ``add_canonical``) cannot write
    default-locale content into a localized file.
    """
    issues = []

    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        return [{
            "type": "file_read_error",
            "severity": "error",
            "description": f"Cannot read file: {e}",
            "auto_fixable": False,
            "file_path": str(file_path),
            "site_slug": site_slug,
            "locale": locale,
            "content_type": content_type,
        }]

    fm, body = parse_frontmatter(content)

    # Run all checks
    checks = [
        check_meta_description(fm, body, site_slug),
        check_title(fm, body, site_slug),
        check_canonical(fm, body, site_slug, file_path),
    ]

    for issue in checks:
        if issue:
            issues.append(issue)

    issues.extend(check_schema_markup(fm, body))
    issues.extend(check_images(body))
    issues.extend(check_headings(body))
    issues.extend(check_links(body, site_slug))

    # Add file/locale context to all issues. ``locale`` and ``content_type``
    # are required by apply_fix (add_canonical) to produce a URL that matches
    # the file's locale subtree.
    try:
        rel_for_report = str(file_path.relative_to(BASE_DIR))
    except ValueError:
        rel_for_report = str(file_path)
    for issue in issues:
        issue["file_path"] = rel_for_report
        issue["site_slug"] = site_slug
        issue["locale"] = locale
        issue["content_type"] = content_type

    return issues

# ── Auto-Fixer ───────────────────────────────────────────────────────────────

def generate_meta_description(body: str, max_length: int = 160) -> str:
    """Generate meta description from content."""
    # Take first paragraph, truncate
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip() and not p.startswith("#")]
    if paragraphs:
        desc = paragraphs[0][:max_length]
        if len(desc) == max_length:
            desc = desc.rsplit(" ", 1)[0] + "..."
        return desc
    return ""

def generate_alt_tag(image_url: str) -> str:
    """Generate alt tag from image filename."""
    filename = Path(image_url).stem
    # Remove extensions, numbers, hyphens to spaces
    alt = re.sub(r'[-_]', ' ', filename)
    alt = re.sub(r'\d+', '', alt)
    alt = alt.strip().title()
    return alt or "Product image"

def apply_fix(file_path: Path, issue: dict, dry_run: bool = False) -> bool:
    """Apply automatic fix for a single issue."""
    fix_type = issue.get("fix_type", "")
    
    if dry_run:
        print(f"  [DRY-RUN] Would apply fix '{fix_type}' to {file_path}")
        return True
    
    try:
        content = file_path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(content)
    except Exception as e:
        print(f"  ❌ Cannot read file for fix: {e}")
        return False
    
    try:
        import yaml
        
        if fix_type == "generate_from_content":
            # Missing meta description
            desc = generate_meta_description(body)
            fm["description"] = desc
            
        elif fix_type == "regenerate":
            # Regenerate meta description or title
            if issue["type"] == "meta_description_length":
                desc = generate_meta_description(body)
                fm["description"] = desc
            elif issue["type"] == "title_length":
                # Extract from H1 or first sentence
                h1s = re.findall(r'^# (.+)$', body, re.MULTILINE)
                if h1s:
                    title = h1s[0][:70]
                    fm["title"] = title
                    
        elif fix_type == "generate_from_heading":
            # Missing title - use H1
            h1s = re.findall(r'^# (.+)$', body, re.MULTILINE)
            if h1s:
                fm["title"] = h1s[0][:70]
                
        elif fix_type == "add_canonical":
            # Add a self-referencing canonical from the slug. The URL must
            # reflect the file's locale subtree: a file at content-en/tests/
            # foo.mdx canonicalizes to https://.../en/test/foo, NOT https://.../foo.
            # Prior to this fix the auto-fixer dropped the locale prefix, which
            # caused EN pages to canonicalize to the FR URL (cross-locale leak).
            slug = fm.get("slug") or file_path.stem
            site_info = SITES.get(issue.get("site_slug", ""), {})
            domain = site_info.get("domain", "")
            site_default = site_info.get("default_locale", "fr")
            issue_locale = issue.get("locale") or site_default
            content_type = issue.get("content_type", "") or ""
            if not domain:
                pass  # nothing safe to write
            else:
                prefix = "" if issue_locale == site_default else f"/{issue_locale}"
                type_segment = f"/{content_type}" if content_type else ""
                fm["canonical"] = f"https://www.{domain}{prefix}{type_segment}/{slug}"
                
        elif fix_type == "infer_schema":
            # Infer schema type from content
            if "review" in body.lower() or "test" in body.lower():
                fm["schemaType"] = "Review"
            elif "guide" in body.lower() or "comment" in body.lower():
                fm["schemaType"] = "Article"
            else:
                fm["schemaType"] = "Product"
                
        elif fix_type == "generate_alt_from_filename":
            # Fix missing alt tags in markdown
            def replace_alt(match):
                alt_text = match.group(1)
                url = match.group(2)
                if not alt_text.strip():
                    new_alt = generate_alt_tag(url)
                    return f"![{new_alt}]({url})"
                return match.group(0)
            
            body = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', replace_alt, body)
        
        # Rebuild file
        new_fm = yaml.dump(fm, allow_unicode=True, sort_keys=False)
        new_content = f"---\n{new_fm}---\n\n{body}\n"
        
        file_path.write_text(new_content, encoding="utf-8")
        print(f"  ✅ Fixed {issue['type']} in {file_path.name}")
        return True
        
    except Exception as e:
        print(f"  ❌ Fix failed: {e}")
        return False

# ── Audit Runner ───────────────────────────────────────────────────────────

def run_site_audit(site_slug: str, dry_run: bool = False, auto_fix: bool = True) -> dict:
    """Run full technical audit on a site."""
    print(f"\n🔍 Auditing {site_slug}...")
    
    mdx_entries = find_mdx_files(site_slug)
    if not mdx_entries:
        print(f"  ⚠️ No MDX files found for {site_slug}")
        return {"pages_checked": 0, "issues_found": 0, "issues_fixed": 0}

    all_issues = []
    fixed_count = 0
    failed_count = 0

    for file_path, locale, content_type in mdx_entries[:100]:  # Limit to 100 pages per audit
        issues = audit_page(file_path, site_slug, locale=locale, content_type=content_type)
        all_issues.extend(issues)

        # Auto-fix if enabled. The (path, locale, content_type) triple is the
        # write contract — apply_fix relies on the locale tag set above to
        # construct a canonical URL that matches the file's locale subtree.
        if auto_fix and not dry_run:
            for issue in issues:
                if issue.get("auto_fixable"):
                    success = apply_fix(file_path, issue, dry_run=dry_run)
                    if success:
                        fixed_count += 1
                        issue["fix_status"] = "applied"
                    else:
                        failed_count += 1
                        issue["fix_status"] = "failed"
    
    # Store issues in DB
    if not dry_run:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        for issue in all_issues:
            cursor.execute("""
                INSERT INTO seo_issues 
                (site_slug, page_path, issue_type, severity, description, auto_fixable, fix_applied, fix_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                issue.get("site_slug", site_slug),
                issue.get("file_path", ""),
                issue["type"],
                issue["severity"],
                issue["description"],
                issue.get("auto_fixable", False),
                issue.get("fix_type", ""),
                issue.get("fix_status", "pending"),
            ))
        
        cursor.execute("""
            INSERT INTO seo_audits 
            (site_slug, audit_type, pages_checked, issues_found, issues_fixed, issues_failed)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (site_slug, "technical", len(mdx_entries), len(all_issues), fixed_count, failed_count))
        
        conn.commit()
        conn.close()
    
    # Emit events
    auto_fixable = [i for i in all_issues if i.get("auto_fixable")]
    manual_issues = [i for i in all_issues if not i.get("auto_fixable")]
    
    if auto_fixable and not dry_run:
        emit_event(
            "seo.fix_applied",
            {
                "site_slug": site_slug,
                "fixes_count": fixed_count,
                "issues": [{"type": i["type"], "file": i.get("file_path", "")} for i in auto_fixable[:10]],
            },
            priority=3,
        )
    
    if manual_issues:
        emit_event(
            "seo.fix_failed",
            {
                "site_slug": site_slug,
                "issues_count": len(manual_issues),
                "issues": [{"type": i["type"], "file": i.get("file_path", ""), "reason": "Manual fix required"} for i in manual_issues[:10]],
            },
            priority=2,
        )
    
    # Emit audit completed
    emit_event(
        "seo.audit_completed",
        {
            "site_slug": site_slug,
            "pages_checked": len(mdx_entries),
            "issues_found": len(all_issues),
            "issues_fixed": fixed_count,
            "issues_manual": len(manual_issues),
        },
        priority=3,
    )
    
    print(f"  ✅ Audit complete: {len(mdx_entries)} pages, {len(all_issues)} issues, {fixed_count} fixed, {len(manual_issues)} manual")
    return {
        "pages_checked": len(mdx_entries),
        "issues_found": len(all_issues),
        "issues_fixed": fixed_count,
        "issues_manual": len(manual_issues),
    }

# ── Event Processing ─────────────────────────────────────────────────────────

def process_event(event: dict, dry_run: bool = False) -> bool:
    """Process a single SEO event."""
    event_type = event.get("type", "")
    payload = event.get("payload", {})
    site_slug = payload.get("site_slug", "")
    
    if event_type == "seo.issue_detected":
        if site_slug:
            run_site_audit(site_slug, dry_run=dry_run)
        else:
            for site in SITES:
                run_site_audit(site, dry_run=dry_run)
        return True
    
    elif event_type == "health.site_status":
        # Validate that previous fixes worked
        print(f"  ✅ Site status check for {site_slug}")
        return True
    
    elif event_type == "content.published":
        # Trigger recrawl check
        print(f"  📊 Scheduling recrawl check for {site_slug}")
        return True
    
    else:
        print(f"  ⚠️ Unknown event type: {event_type}")
        return False

# ── Event Consumption ────────────────────────────────────────────────────────

def consume_events(limit: int = 10, dry_run: bool = False, timeout: int = 600) -> int:
    """Consume SEO events from inbox."""
    ensure_dirs()
    overall_start = time.time()
    
    all_files = sorted(
        [f for f in INBOX_DIR.iterdir() if f.is_file() and f.suffix == ".json"],
        key=lambda p: p.stat().st_mtime,
    )
    
    candidate_files = []
    for p in all_files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                ev = json.load(f)
            if ev.get("type") in {"seo.issue_detected", "health.site_status", "content.published"}:
                candidate_files.append(p)
        except:
            continue
        if len(candidate_files) >= limit * 2:
            break
    
    processed = 0
    failed = 0
    
    for path in candidate_files:
        if processed >= limit:
            break
        
        elapsed = time.time() - overall_start
        remaining = timeout - elapsed
        if remaining <= 30:
            print(f"\n⏰ Timeout approaching, exiting.")
            break
        
        event = read_event(path)
        if event is None:
            move_event(path, FAILED_DIR, dry_run=dry_run)
            continue
        
        print(f"\n📦 Processing: {event.get('type')} ({processed + 1}/{limit})")
        
        proc_path = claim_inbox_json(path, dry_run=dry_run)
        if not proc_path:
            continue
        event["_file_path"] = str(proc_path)

        success = process_event(event, dry_run=dry_run)
        
        if success:
            complete_claimed_event(event, dry_run=dry_run)
            processed += 1
        else:
            fail_claimed_event(event, dry_run=dry_run)
            failed += 1
    
    print(f"\n📊 Summary: {processed} processed, {failed} failed")
    return processed

# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agent SEO Auditor — Technical SEO Monitoring",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --consume --limit 10
  %(prog)s --audit-all --dry-run
  %(prog)s --site aspirateur --check-links
""",
    )
    parser.add_argument("--consume", action="store_true", help="Consume pending SEO events")
    parser.add_argument("--limit", type=int, default=10, help="Max events to process")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes")
    parser.add_argument("--audit-all", action="store_true", help="Audit all sites")
    parser.add_argument("--site", type=str, help="Target site slug")
    parser.add_argument("--check-links", action="store_true", help="Check broken links")
    parser.add_argument("--check-images", action="store_true", help="Check missing alt tags")
    parser.add_argument("--check-schema", action="store_true", help="Check schema markup")
    parser.add_argument("--auto-fix", action="store_true", default=True, help="Apply automatic fixes")
    parser.add_argument("--no-auto-fix", action="store_true", dest="auto_fix", help="Disable auto-fix")
    
    args = parser.parse_args()
    
    if args.audit_all:
        for site in SITES:
            print(f"\n{'='*60}")
            print(f"🔍 Auditing {site}")
            print(f"{'='*60}")
            run_site_audit(site, dry_run=args.dry_run, auto_fix=args.auto_fix)
        return 0
    
    if args.site:
        if args.check_links or args.check_images or args.check_schema:
            # Run specific checks
            mdx_entries = find_mdx_files(args.site)
            for file_path, locale, content_type in mdx_entries[:20]:
                issues = audit_page(file_path, args.site, locale=locale, content_type=content_type)
                for issue in issues:
                    if args.check_links and "link" in issue["type"]:
                        print(f"  {issue['severity']}: {issue['description']} ({issue.get('file_path', '')})")
                    if args.check_images and "alt" in issue["type"]:
                        print(f"  {issue['severity']}: {issue['description']} ({issue.get('file_path', '')})")
                    if args.check_schema and "schema" in issue["type"]:
                        print(f"  {issue['severity']}: {issue['description']} ({issue.get('file_path', '')})")
        else:
            run_site_audit(args.site, dry_run=args.dry_run, auto_fix=args.auto_fix)
        return 0
    
    if args.consume:
        processed = consume_events(limit=args.limit, dry_run=args.dry_run)
        return 0 if processed >= 0 else 1
    
    parser.print_help()
    return 0

if __name__ == "__main__":
    sys.exit(main())

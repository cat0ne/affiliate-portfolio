#!/usr/bin/env python3
"""
Site Health Monitor
Check git status, content counts, translations, schema coverage,
config files, and recent activity across all 5 affiliate sites.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = BASE_DIR / "reports"
SITES = ["aspirateur", "bureau", "cafe", "matelas", "pixinstant"]
LOCALES = ["content-en", "content-de", "content-es", "content-it", "content-uk"]
SCHEMA_TYPES = [
    "ArticleJsonLd",
    "ProductJsonLd",
    "ProductsJsonLd",
    "BreadcrumbJsonLd",
    "OrganizationJsonLd",
    "ReviewJsonLd",
    "HowToJsonLd",
    "FaqJsonLd",
    "ItemListJsonLd",
    "AggregateRatingJsonLd",
    "WebsiteJsonLd",
    "PersonJsonLd",
    "CollectionPageJsonLd",
    "JsonLd",
]


def run_git(site_dir: Path, *args):
    """Run a git command in the site directory."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=site_dir,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception as e:
        return "", str(e), 1


def get_git_status(site_dir: Path):
    """Check uncommitted changes and ahead/behind remote."""
    stdout, stderr, rc = run_git(site_dir, "status", "--short")
    uncommitted = stdout.splitlines() if rc == 0 else []

    branch, _, _ = run_git(site_dir, "rev-parse", "--abbrev-ref", "HEAD")
    ahead_behind = "0\t0"
    if branch:
        ab_out, _, ab_rc = run_git(
            site_dir,
            "rev-list",
            "--left-right",
            "--count",
            f"origin/{branch}...HEAD",
        )
        if ab_rc == 0:
            ahead_behind = ab_out.strip()

    try:
        behind, ahead = ahead_behind.split("\t")
    except ValueError:
        behind, ahead = "0", "0"

    return {
        "branch": branch,
        "uncommitted_count": len(uncommitted),
        "uncommitted_files": uncommitted,
        "ahead": int(ahead) if ahead.isdigit() else 0,
        "behind": int(behind) if behind.isdigit() else 0,
    }


def get_recent_commits(site_dir: Path, days: int = 7):
    """Get commits in the last N days."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    stdout, _, rc = run_git(
        site_dir,
        "log",
        f"--since={since}",
        "--oneline",
        "--no-decorate",
    )
    if rc != 0:
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def count_content_files(site_dir: Path):
    """Count content files per locale.

    Supports two directory patterns:
      - Sibling dirs: content-en/, content-de/, content-es/ ...
      - Nested dirs:  content/en/, content/de/, content/es/ ...
    FR is counted from content/ excluding nested locale subdirs.
    """
    counts = {}
    content_dir = site_dir / "content"
    locale_names = [loc.replace("content-", "") for loc in LOCALES]

    # FR = .mdx files in content/ but NOT inside content/en/, content/de/, etc.
    fr_count = 0
    if content_dir.exists():
        for f in content_dir.rglob("*.mdx"):
            # skip files inside nested locale dirs (e.g. content/en/...)
            rel_parts = f.relative_to(content_dir).parts
            if len(rel_parts) > 1 and rel_parts[0] in locale_names:
                continue
            fr_count += 1
    counts["fr"] = fr_count

    for locale in LOCALES:
        loc_key = locale.replace("content-", "")
        total = 0

        # Pattern 1: sibling dir (e.g. aspirateur/content-en/)
        sibling_dir = site_dir / locale
        if sibling_dir.exists():
            total += sum(1 for f in sibling_dir.rglob("*.mdx"))

        # Pattern 2: nested dir (e.g. pixinstant/content/en/)
        nested_dir = site_dir / "content" / loc_key
        if nested_dir.exists():
            total += sum(1 for f in nested_dir.rglob("*.mdx"))

        counts[loc_key] = total

    return counts


def get_schema_coverage(site_dir: Path):
    """Check which schema components exist in src/components/seo/."""
    seo_dir = site_dir / "src" / "components" / "seo"
    found = {}
    if not seo_dir.exists():
        return {s: False for s in SCHEMA_TYPES}
    existing = {f.stem for f in seo_dir.iterdir() if f.is_file()}
    for schema in SCHEMA_TYPES:
        found[schema] = schema in existing
    return found


def check_config_files(site_dir: Path):
    """Check existence of robots.ts, sitemap.ts, next.config.ts and key settings."""
    # Next.js App Router convention: robots.ts and sitemap.ts live in src/app/
    robots = (site_dir / "src" / "app" / "robots.ts").exists()
    sitemap = (site_dir / "src" / "app" / "sitemap.ts").exists()
    next_config = site_dir / "next.config.ts"
    config_exists = next_config.exists()

    settings = {}
    if config_exists:
        try:
            text = next_config.read_text(encoding="utf-8")
            settings["trailingSlash"] = "trailingSlash" in text
            settings["poweredByHeader"] = "poweredByHeader" in text
            settings["compress"] = "compress" in text
            settings["images"] = "images:" in text or "images =" in text
        except Exception:
            pass

    return {
        "robots_ts": robots,
        "sitemap_ts": sitemap,
        "next_config_ts": config_exists,
        "settings": settings,
    }


def analyze_site(site: str):
    """Run all checks for a single site."""
    site_dir = BASE_DIR / site
    if not site_dir.exists():
        return None

    git = get_git_status(site_dir)
    commits = get_recent_commits(site_dir)
    content = count_content_files(site_dir)
    schemas = get_schema_coverage(site_dir)
    config = check_config_files(site_dir)

    # Translation gaps: compare FR to each locale
    fr_count = content.get("fr", 0)
    gaps = {}
    for loc in ["en", "de", "es", "it", "uk"]:
        loc_count = content.get(loc, 0)
        gaps[loc] = {
            "count": loc_count,
            "gap": fr_count - loc_count,
            "pct": round((loc_count / fr_count) * 100, 1) if fr_count else 0,
        }

    return {
        "site": site,
        "git": git,
        "commits_last_7d": commits,
        "commits_last_7d_count": len(commits),
        "content": content,
        "translation_gaps": gaps,
        "schemas": schemas,
        "config": config,
    }


def print_summary(results):
    """Print a nice console summary."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"\n{'=' * 60}")
    print(f"  SITE HEALTH MONITOR  —  {now}")
    print(f"{'=' * 60}\n")

    # Git summary
    print("  GIT STATUS")
    print(f"  {'Site':<12} {'Branch':<10} {'Uncommitted':>11} {'Ahead':>6} {'Behind':>7}")
    print(f"  {'-' * 50}")
    for r in results:
        g = r["git"]
        flag = "⚠️" if g["uncommitted_count"] or g["ahead"] or g["behind"] else "✅"
        print(
            f"  {r['site']:<12} {g['branch']:<10} {g['uncommitted_count']:>10} {g['ahead']:>6} {g['behind']:>7}  {flag}"
        )
    print()

    # Content counts
    print("  CONTENT FILES (FR + Locales)")
    print(f"  {'Site':<12} {'FR':>6} {'EN':>6} {'DE':>6} {'ES':>6} {'IT':>6} {'UK':>6}")
    print(f"  {'-' * 56}")
    for r in results:
        c = r["content"]
        print(
            f"  {r['site']:<12} {c.get('fr',0):>6} {c.get('en',0):>6} {c.get('de',0):>6} {c.get('es',0):>6} {c.get('it',0):>6} {c.get('uk',0):>6}"
        )
    print()

    # Recent activity
    print("  RECENT COMMITS (last 7 days)")
    for r in results:
        count = r["commits_last_7d_count"]
        emoji = "🔥" if count >= 5 else "✅" if count > 0 else "💤"
        print(f"  {r['site']:<12} {count:>3} commits  {emoji}")
    print()

    # Translation warnings
    print("  TRANSLATION GAPS (FR - locale)")
    for r in results:
        gaps = r["translation_gaps"]
        warnings = []
        for loc, data in gaps.items():
            if data["gap"] > 20:
                warnings.append(f"{loc.upper()}: -{data['gap']}")
        if warnings:
            print(f"  {r['site']:<12} ⚠️  {', '.join(warnings)}")
        else:
            print(f"  {r['site']:<12} ✅  all caught up")
    print()

    # Schema coverage summary
    print("  SCHEMA COVERAGE")
    for r in results:
        schemas = r["schemas"]
        total = sum(1 for v in schemas.values() if v)
        print(f"  {r['site']:<12} {total:>2} / {len(SCHEMA_TYPES)} types")
    print()

    # Config files
    print("  CONFIG FILES")
    print(f"  {'Site':<12} {'robots.ts':>10} {'sitemap.ts':>11} {'next.config.ts':>15}")
    print(f"  {'-' * 46}")
    for r in results:
        cfg = r["config"]
        print(
            f"  {r['site']:<12} {'✅' if cfg['robots_ts'] else '❌':>10} {'✅' if cfg['sitemap_ts'] else '❌':>11} {'✅' if cfg['next_config_ts'] else '❌':>15}"
        )
    print()

    # Overall health score
    print("  HEALTH SCORE")
    for r in results:
        score = 100
        if r["git"]["uncommitted_count"]:
            score -= 10
        if r["git"]["behind"]:
            score -= 5
        if not r["config"]["next_config_ts"]:
            score -= 20
        if r["commits_last_7d_count"] == 0:
            score -= 10
        gaps = r["translation_gaps"]
        if any(g["gap"] > 50 for g in gaps.values()):
            score -= 10
        emoji = "🟢" if score >= 90 else "🟡" if score >= 70 else "🔴"
        print(f"  {r['site']:<12} {score:>3}/100  {emoji}")
    print(f"{'=' * 60}\n")


def generate_markdown(results):
    """Generate a markdown report from results."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Site Health Report",
        f"",
        f"Generated: {now}",
        f"",
        "## Git Status",
        f"",
        "| Site | Branch | Uncommitted | Ahead | Behind |",
        "|------|--------|-------------|-------|--------|",
    ]
    for r in results:
        g = r["git"]
        lines.append(
            f"| {r['site']} | {g['branch']} | {g['uncommitted_count']} | {g['ahead']} | {g['behind']} |"
        )

    lines.extend([
        "",
        "## Content Counts",
        "",
        "| Site | FR | EN | DE | ES | IT | UK |",
        "|------|----|----|----|----|----|----|",
    ])
    for r in results:
        c = r["content"]
        lines.append(
            f"| {r['site']} | {c.get('fr',0)} | {c.get('en',0)} | {c.get('de',0)} | {c.get('es',0)} | {c.get('it',0)} | {c.get('uk',0)} |"
        )

    lines.extend([
        "",
        "## Recent Commits (last 7 days)",
        "",
    ])
    for r in results:
        lines.append(f"### {r['site']}")
        if r["commits_last_7d"]:
            for commit in r["commits_last_7d"][:5]:
                lines.append(f"- `{commit}`")
        else:
            lines.append("_No commits in the last 7 days._")
        lines.append("")

    lines.extend([
        "## Translation Gaps",
        "",
    ])
    for r in results:
        lines.append(f"### {r['site']}")
        for loc, data in r["translation_gaps"].items():
            status = "✅" if data["gap"] <= 5 else "⚠️" if data["gap"] <= 30 else "🔴"
            lines.append(f"- {status} **{loc.upper()}**: {data['count']} / {r['content'].get('fr',0)} ({data['pct']}%)")
        lines.append("")

    lines.extend([
        "## Schema Coverage",
        "",
    ])
    for r in results:
        lines.append(f"### {r['site']}")
        for schema, present in r["schemas"].items():
            icon = "✅" if present else "❌"
            lines.append(f"- {icon} `{schema}`")
        lines.append("")

    lines.extend([
        "## Config Files",
        "",
        "| Site | robots.ts | sitemap.ts | next.config.ts |",
        "|------|-----------|------------|----------------|",
    ])
    for r in results:
        cfg = r["config"]
        lines.append(
            f"| {r['site']} | {'✅' if cfg['robots_ts'] else '❌'} | {'✅' if cfg['sitemap_ts'] else '❌'} | {'✅' if cfg['next_config_ts'] else '❌'} |"
        )

    lines.extend([
        "",
        "## Next.config Key Settings",
        "",
    ])
    for r in results:
        cfg = r["config"]
        settings = cfg.get("settings", {})
        if settings:
            parts = [f"{k}={v}" for k, v in settings.items()]
            lines.append(f"- **{r['site']}**: {', '.join(parts)}")
        else:
            lines.append(f"- **{r['site']}**: _could not parse settings_")

    lines.append("")
    return "\n".join(lines)


def main():
    results = []
    for site in SITES:
        data = analyze_site(site)
        if data:
            results.append(data)

    print_summary(results)

    # Write JSON
    json_path = REPORTS_DIR / "site-health-latest.json"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {"generated_at": datetime.now().isoformat(), "sites": results},
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"  JSON written to: {json_path}\n")

    # Write Markdown report with date stamp
    md = generate_markdown(results)
    date_str = datetime.now().strftime("%Y-%m-%d")
    md_path = REPORTS_DIR / f"site-health-{date_str}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"  Markdown written to: {md_path}\n")

    # Also update the template/latest symlink style file
    template_path = BASE_DIR / "reports" / "site-health-report.md"
    with open(template_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"  Template written to: {template_path}\n")


if __name__ == "__main__":
    main()

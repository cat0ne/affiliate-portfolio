#!/usr/bin/env python3
"""
Content Decay Watcher
Scan all MDX files across affiliate sites, flag:
- Articles not modified in > N days (stale)
- Articles with outdated year references (e.g., 2025 in 2026 content)
- Articles with thin prose content (< 500 words)
Generate a refresh roadmap.
"""

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import yaml

BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
REPORTS_DIR = BASE_DIR / "reports"
SITES = ["aspirateur", "bureau", "cafe", "matelas", "pixinstant"]
STALE_DAYS = 90
THIN_THRESHOLD = 500
CURRENT_YEAR = 2026
OUTDATED_YEAR = CURRENT_YEAR - 1  # 2025


# ─── Word counting for MDX ───

MDX_JSX_RE = re.compile(r"<([A-Za-z][A-Za-z0-9]*)[^>]*>.*?</\1>", re.DOTALL)
MDX_SELF_CLOSING_JSX_RE = re.compile(r"<[A-Za-z][A-Za-z0-9]*[^>]*/>")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^\)]+\)")
MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+.*$", re.MULTILINE)
MARKDOWN_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_|`)")
MARKDOWN_TABLE_RE = re.compile(r"\|[^\n]+\|\n\|[\s\-:|]+\|\n(?:\|[^\n]+\|\n?)*")
MARKDOWN_LIST_RE = re.compile(r"^(\s*[-*+]|\s*\d+\.)\s+", re.MULTILINE)
MARKDOWN_BLOCKQUOTE_RE = re.compile(r"^>\s*", re.MULTILINE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
URL_RE = re.compile(r"https?://\S+")
WHITESPACE_RE = re.compile(r"\s+")


def count_prose_words(text: str) -> int:
    """
    Count prose words in MDX body text.
    Strips frontmatter, JSX components, markdown syntax, HTML, URLs.
    """
    # Remove frontmatter
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]

    # Remove JSX components (including multi-line)
    text = MDX_JSX_RE.sub(" ", text)
    text = MDX_SELF_CLOSING_JSX_RE.sub(" ", text)

    # Remove markdown tables entirely (they're mostly syntax, not prose)
    text = MARKDOWN_TABLE_RE.sub(" ", text)

    # Remove markdown images
    text = MARKDOWN_IMAGE_RE.sub(" ", text)

    # Replace markdown links with their link text only
    text = MARKDOWN_LINK_RE.sub(r"\1", text)

    # Remove markdown headers
    text = MARKDOWN_HEADER_RE.sub(" ", text)

    # Remove markdown emphasis markers
    text = MARKDOWN_EMPHASIS_RE.sub(" ", text)

    # Remove list markers
    text = MARKDOWN_LIST_RE.sub(" ", text)

    # Remove blockquote markers
    text = MARKDOWN_BLOCKQUOTE_RE.sub(" ", text)

    # Remove any remaining HTML tags
    text = HTML_TAG_RE.sub(" ", text)

    # Remove URLs
    text = URL_RE.sub(" ", text)

    # Normalize whitespace
    text = WHITESPACE_RE.sub(" ", text)

    # Count words
    words = text.strip().split()
    return len(words)


def extract_frontmatter(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None, ""
    if not text.startswith("---"):
        return None, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, text
    try:
        return yaml.safe_load(parts[1]), text
    except Exception:
        return None, text


def parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None


def get_recommendation(days):
    if days > 730:
        return "archive"
    if days > 365:
        return "rewrite"
    if days > STALE_DAYS:
        return "maj"
    return "ok"


def scan_site(site_dir: Path):
    """Scan a single site for stale, thin, and outdated content."""
    stale_results = []
    thin_results = []
    outdated_results = []

    content_dirs = [d for d in site_dir.iterdir() if d.is_dir() and d.name.startswith("content")]
    for cdir in content_dirs:
        for mdx in cdir.rglob("*.mdx"):
            fm, raw_text = extract_frontmatter(mdx)
            if not fm:
                continue

            title = fm.get("title", "")
            slug = fm.get("slug", "")
            date_modified = parse_date(fm.get("dateModified")) or parse_date(fm.get("datePublished"))
            if not date_modified:
                mtime = datetime.fromtimestamp(mdx.stat().st_mtime).date()
                date_modified = mtime

            days_since = (datetime.now().date() - date_modified).days
            rel_path = str(mdx.relative_to(BASE_DIR))

            # ── Stale check ──
            if days_since > STALE_DAYS:
                stale_results.append({
                    "site": site_dir.name,
                    "locale": cdir.name,
                    "title": title,
                    "slug": slug,
                    "path": rel_path,
                    "last_modified": date_modified.isoformat(),
                    "days_since": days_since,
                    "recommendation": get_recommendation(days_since),
                })

            # ── Thin content check ──
            word_count = count_prose_words(raw_text)
            if word_count < THIN_THRESHOLD:
                thin_results.append({
                    "site": site_dir.name,
                    "locale": cdir.name,
                    "title": title,
                    "slug": slug,
                    "path": rel_path,
                    "word_count": word_count,
                })

            # ── Outdated year check ──
            outdated_count = raw_text.count(str(OUTDATED_YEAR))
            # Only flag if the title mentions CURRENT_YEAR but body mentions OUTDATED_YEAR,
            # or if it's a seasonal article (Prime Day, Black Friday, etc.) that references last year
            if outdated_count > 0:
                # Skip if it's just a frontmatter date field
                body_text = raw_text.split("---", 2)[2] if raw_text.startswith("---") else raw_text
                body_outdated = body_text.count(str(OUTDATED_YEAR))
                if body_outdated > 0:
                    outdated_results.append({
                        "site": site_dir.name,
                        "locale": cdir.name,
                        "title": title,
                        "slug": slug,
                        "path": rel_path,
                        f"{OUTDATED_YEAR}_count": body_outdated,
                    })

    stale_results.sort(key=lambda x: x["days_since"], reverse=True)
    thin_results.sort(key=lambda x: x["word_count"])
    return stale_results, thin_results, outdated_results


def generate_markdown(stale, thin, outdated):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 🍂 Content Decay Report — {today}",
        "",
        f"**Seuil fraîcheur :** articles non modifiés depuis plus de {STALE_DAYS} jours  ",
        f"**Seuil contenu fin :** moins de {THIN_THRESHOLD} mots de prose  ",
        f"**Année obsolète :** références à {OUTDATED_YEAR} dans du contenu {CURRENT_YEAR}",
        "",
        "| Site | Total articles | Stale | Thin | Outdated year |",
        "|------|---------------|-------|------|---------------|",
    ]

    # Build summary table
    all_sites_data = {}
    for item in stale:
        all_sites_data.setdefault(item["site"], {"stale": 0, "thin": 0, "outdated": 0})["stale"] += 1
    for item in thin:
        all_sites_data.setdefault(item["site"], {"stale": 0, "thin": 0, "outdated": 0})["thin"] += 1
    for item in outdated:
        all_sites_data.setdefault(item["site"], {"stale": 0, "thin": 0, "outdated": 0})["outdated"] += 1

    for site in SITES:
        data = all_sites_data.get(site, {"stale": 0, "thin": 0, "outdated": 0})
        total = data["stale"] + data["thin"] + data["outdated"]
        lines.append(f"| {site.capitalize()} | {total} | {data['stale']} | {data['thin']} | {data['outdated']} |")

    lines.extend(["", "---", ""])

    # ── Stale articles ──
    lines.extend([
        "## 📅 Stale Articles (> 90 days)",
        "",
    ])
    if not stale:
        lines.append("✅ No stale articles found.")
        lines.append("")
    else:
        by_site = {}
        for item in stale:
            by_site.setdefault(item["site"], []).append(item)
        for site in SITES:
            items = by_site.get(site, [])
            if not items:
                continue
            lines.append(f"### {site.capitalize()}")
            lines.append("")
            lines.append("| Titre | Slug | Dernière modif | Jours | Recommandation |")
            lines.append("|-------|------|----------------|-------|----------------|")
            for it in items[:10]:  # limit to top 10 per site
                rec_emoji = {"archive": "🗑️", "rewrite": "✍️", "maj": "🔄", "ok": "✅"}.get(it["recommendation"], "")
                title_short = it["title"][:50] + "..." if len(it["title"]) > 50 else it["title"]
                lines.append(
                    f"| {title_short} | `{it['slug']}` | {it['last_modified']} | {it['days_since']} | {rec_emoji} {it['recommendation']} |"
                )
            lines.append("")

    # ── Thin content ──
    lines.extend([
        "## 🪶 Thin Content (< 500 words of prose)",
        "",
    ])
    if not thin:
        lines.append("✅ No thin articles found.")
        lines.append("")
    else:
        by_site = {}
        for item in thin:
            by_site.setdefault(item["site"], []).append(item)
        for site in SITES:
            items = by_site.get(site, [])
            if not items:
                continue
            lines.append(f"### {site.capitalize()}")
            lines.append("")
            lines.append("| Fichier | Titre | Mots |")
            lines.append("|---------|-------|------|")
            for it in items[:15]:  # limit to worst 15 per site
                title_short = it["title"][:50] + "..." if len(it["title"]) > 50 else it["title"]
                lines.append(
                    f"| `{it['path']}` | {title_short} | {it['word_count']} |"
                )
            lines.append("")

    # ── Outdated year references ──
    lines.extend([
        f"## 📆 Articles with Outdated Year References ({OUTDATED_YEAR})",
        "",
    ])
    if not outdated:
        lines.append(f"✅ No {OUTDATED_YEAR} references found.")
        lines.append("")
    else:
        by_site = {}
        for item in outdated:
            by_site.setdefault(item["site"], []).append(item)
        for site in SITES:
            items = by_site.get(site, [])
            if not items:
                continue
            lines.append(f"### {site.capitalize()}")
            lines.append("")
            lines.append("| Fichier | Titre | Mentions |")
            lines.append("|---------|-------|----------|")
            for it in items[:15]:
                title_short = it["title"][:50] + "..." if len(it["title"]) > 50 else it["title"]
                lines.append(
                    f"| `{it['path']}` | {title_short} | {it[f'{OUTDATED_YEAR}_count']} |"
                )
            lines.append("")

    lines.extend([
        "---",
        "",
        "## 💡 Plan d'action",
        "",
        f"- **maj** → Rafraîchir les données (prix, dispos, FAQ), mettre à jour `dateModified`.",
        f"- **rewrite** → Réécriture partielle ou complète du contenu, nouveau angle SEO.",
        f"- **archive** → Contenu obsolète, redirection 301 ou noindex.",
        f"- **expand** → Contenu fin (< {THIN_THRESHOLD} mots) : ajouter des sections, approfondir les critères, enrichir les FAQ.",
        f"- **year-fix** → Remplacer les références {OUTDATED_YEAR} par {CURRENT_YEAR} (ou historiser si pertinent).",
        "",
        f"*Rapport généré automatiquement le {datetime.now().strftime('%Y-%m-%d %H:%M')}*",
        "",
    ])
    return "\n".join(lines)


def main():
    print("=" * 60)
    print("Content Decay Watcher")
    print("=" * 60)

    all_stale = []
    all_thin = []
    all_outdated = []

    for site in SITES:
        site_dir = BASE_DIR / site
        if not site_dir.exists():
            print(f"⚠️  Site directory not found: {site_dir}")
            continue
        stale, thin, outdated = scan_site(site_dir)
        print(f"📂 {site}: {len(stale)} stale | {len(thin)} thin | {len(outdated)} outdated year")
        all_stale.extend(stale)
        all_thin.extend(thin)
        all_outdated.extend(outdated)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Markdown report
    today = datetime.now().strftime("%Y-%m-%d")
    md = generate_markdown(all_stale, all_thin, all_outdated)
    md_path = REPORTS_DIR / f"content-decay-{today}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"✅ Report saved: {md_path}")

    # JSON snapshot
    json_path = REPORTS_DIR / "content-decay-latest.json"
    json_path.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "threshold_days": STALE_DAYS,
            "thin_threshold": THIN_THRESHOLD,
            "total_stale": len(all_stale),
            "total_thin": len(all_thin),
            "total_outdated_year": len(all_outdated),
            "stale": all_stale,
            "thin_content": all_thin,
            "outdated_year": all_outdated,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"✅ JSON snapshot saved: {json_path}")

    total_flagged = len(all_stale) + len(all_thin) + len(all_outdated)
    if total_flagged:
        msg = f"{total_flagged} problèmes détectés : {len(all_stale)} stale, {len(all_thin)} thin, {len(all_outdated)} year"
        script = f'display notification "{msg}" with title "Content Decay Watcher" subtitle "Alerte fraîcheur" sound name "Glass"'
        os.system(f"osascript -e '{script}' 2>/dev/null")
        print("🔔 Notification sent")
    else:
        print("✨ All content is fresh")

    print("\n✨ Done")


if __name__ == "__main__":
    main()

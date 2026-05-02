#!/usr/bin/env python3
"""
Seasonal Page Broken Link Fixer

Seasonal pages (Black Friday, Prime Day, Mother's Day, etc.) have high link rot
because Amazon delists promo products after events. This script:

1. Finds all seasonal MDX files by slug pattern
2. Checks all /dp/ links in those files
3. Converts broken /dp/ links to /s?k= search links
4. Uses product context (anchor text, nearby text) for the search term

Usage:
    python scripts/fix_seasonal_broken_links.py [--dry-run]
"""

import argparse
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse, quote

BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
SITES = ["matelas", "aspirateur", "bureau", "cafe", "pixinstant"]

SEASONAL_PATTERNS = re.compile(
    r'(black[-_]?friday|prime[-_]?day|fete[-_]?des[-_]?meres|mothers[-_]?day|'
    r'noel|christmas|soldes|sales|cyber[-_]?monday)',
    re.IGNORECASE
)

URL_RE = re.compile(r'https?://[^\s"\'>\)\]]+')
DP_RE = re.compile(r'/dp/([A-Z0-9]{10})')

# Map domains to tracking tags
TAGS = {
    "amazon.fr": "zoomzen05-21",
    "amazon.de": "zoomzen-21",
    "amazon.es": "zoomzen08-21",
    "amazon.it": "zoomzen01-21",
    "amazon.co.uk": "zoomzen07-21",
    "amazon.com": "zoomzus-20",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
)


def is_seasonal_file(path: Path) -> bool:
    return bool(SEASONAL_PATTERNS.search(path.name))


def find_seasonal_files(site_dir: Path) -> list[Path]:
    """Find all seasonal MDX files in a site."""
    files = []
    for cdir in site_dir.iterdir():
        if cdir.is_dir() and cdir.name.startswith("content"):
            for mdx in cdir.rglob("*.mdx"):
                if is_seasonal_file(mdx):
                    files.append(mdx)
    return files


def check_url(url: str) -> bool:
    """Quick curl check. Returns True if 200."""
    result = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         "-A", USER_AGENT, "--max-time", "10", "-L", url],
        capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip() == "200"


def extract_search_term(line: str, url: str) -> str:
    """Extract a good search term from markdown link context."""
    # Try to get anchor text from markdown link [text](url)
    escaped = re.escape(url)
    m = re.search(r'\[([^\]]+)\]\(' + escaped + r'\)', line)
    if m:
        term = m.group(1)
    else:
        # Fallback: extract product name from surrounding bold text
        m = re.search(r'\*\*([^*]+)\*\*', line)
        if m:
            term = m.group(1)
        else:
            # Fallback: use ASIN as last resort
            asin_m = DP_RE.search(url)
            term = asin_m.group(1) if asin_m else ""

    # Clean markdown artifacts
    term = re.sub(r'[*_]', '', term)
    term = re.sub(r'\s+', ' ', term).strip()

    # Filter generic terms
    generic = {'voir le prix', 'see price', 'ver el precio', 'preis', 'amazon',
               'voir le prix sur amazon', 'price on amazon', '→', 'check price'}
    if term.lower() in generic or len(term) < 3:
        # Try to extract from table cell or nearby context
        # For tables: look for product name in same row before the link
        m = re.search(r'\|\s*\*?\*?([^|*]+)\*?\*?.*?' + re.escape(url), line)
        if m:
            term = re.sub(r'[*_]', '', m.group(1)).strip()
        if not term or term.lower() in generic or len(term) < 3:
            asin_m = DP_RE.search(url)
            term = asin_m.group(1) if asin_m else "produit"

    return term


def make_search_url(url: str, term: str) -> str:
    """Convert a /dp/ URL to a /s?k= search URL preserving tag."""
    parsed = urlparse(url)
    domain = parsed.netloc
    tag = TAGS.get(domain, "")
    query = quote(term, safe='')
    new_url = f"https://{domain}/s?k={query}"
    if tag:
        new_url += f"&tag={tag}"
    return new_url


def fix_file(path: Path, dry_run: bool = False) -> list[dict]:
    """Fix broken /dp/ links in a single seasonal file."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    fixes = []
    modified = False

    for i, line in enumerate(lines):
        for match in URL_RE.findall(line):
            if DP_RE.search(match):
                # Check if broken
                if check_url(match):
                    continue  # Working, skip

                time.sleep(0.5)

                term = extract_search_term(line, match)
                new_url = make_search_url(match, term)

                fixes.append({
                    "line": i + 1,
                    "old": match,
                    "new": new_url,
                    "term": term,
                })

                if not dry_run:
                    line = line.replace(match, new_url)
                    modified = True
                    lines[i] = line

    if modified and not dry_run:
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")

    return fixes


def main():
    parser = argparse.ArgumentParser(description="Fix broken links in seasonal pages")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without modifying files")
    args = parser.parse_args()

    print("=" * 60)
    print("Seasonal Page Broken Link Fixer")
    print("=" * 60)
    if args.dry_run:
        print("🔍 DRY RUN — no files will be modified")
    print()

    all_fixes = []

    for site_name in SITES:
        site_dir = BASE_DIR / site_name
        if not site_dir.exists():
            continue

        files = find_seasonal_files(site_dir)
        if not files:
            continue

        print(f"📂 {site_name}: {len(files)} seasonal file(s)")
        for fpath in files:
            print(f"   🔍 {fpath.name}")
            fixes = fix_file(fpath, dry_run=args.dry_run)
            if fixes:
                print(f"      Found {len(fixes)} broken link(s):")
                for fix in fixes:
                    action = "WOULD FIX" if args.dry_run else "FIXED"
                    print(f"      [{action}] line {fix['line']}: {fix['term']}")
                all_fixes.extend([
                    {"site": site_name, "file": str(fpath.relative_to(BASE_DIR)), **f}
                    for f in fixes
                ])
            else:
                print(f"      ✅ All links working")

    print()
    if all_fixes:
        print(f"🎄 Total fixes{' (dry run)' if args.dry_run else ''}: {len(all_fixes)}")
    else:
        print("🎄 No broken seasonal links found.")


if __name__ == "__main__":
    main()

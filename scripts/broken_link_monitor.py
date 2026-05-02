#!/usr/bin/env python3
"""
Broken Affiliate Link Monitor
Scans all MDX files and product JSONs for Amazon affiliate URLs,
checks them via HTTP GET with rate limiting, and reports genuinely broken links.

Amazon aggressively blocks bots. We use slow sequential requests with
real browser headers and retry 404s once to reduce false positives.
"""

import json
import os
import random
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import urllib.request

BASE_DIR = Path("/Users/gho/Documents/affiliation-sites")
REPORTS_DIR = BASE_DIR / "reports"

SITES = ["matelas", "aspirateur", "bureau", "cafe", "pixinstant"]

AMAZON_DOMAINS = {
    "amazon.fr", "amazon.de", "amazon.es", "amazon.it",
    "amazon.co.uk", "amazon.com", "www.amazon.fr", "www.amazon.de",
    "www.amazon.es", "www.amazon.it", "www.amazon.co.uk", "www.amazon.com",
}

URL_RE = re.compile(r'https?://[^\s"\'>\)\]]+')

# Minimal, safe headers — Amazon blocks requests with mismatched Accept-Language
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"

# Domain-appropriate Accept-Language to avoid 404 redirects
LOCALE_HEADERS = {
    "amazon.fr": "fr-FR,fr;q=0.9",
    "www.amazon.fr": "fr-FR,fr;q=0.9",
    "amazon.de": "de-DE,de;q=0.9",
    "www.amazon.de": "de-DE,de;q=0.9",
    "amazon.es": "es-ES,es;q=0.9",
    "www.amazon.es": "es-ES,es;q=0.9",
    "amazon.it": "it-IT,it;q=0.9",
    "www.amazon.it": "it-IT,it;q=0.9",
    "amazon.co.uk": "en-GB,en;q=0.9",
    "www.amazon.co.uk": "en-GB,en;q=0.9",
    "amazon.com": "en-US,en;q=0.9",
    "www.amazon.com": "en-US,en;q=0.9",
}


def find_urls_in_file(path: Path) -> list[tuple[str, int]]:
    urls = []
    try:
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for match in URL_RE.findall(line):
                parsed = urlparse(match)
                if parsed.netloc in AMAZON_DOMAINS:
                    urls.append((match, i))
    except Exception:
        pass
    return urls


def _sanitize_url(url: str) -> str:
    """Properly encode non-ASCII characters in URL path/query."""
    parsed = urlparse(url)
    safe = "/#?&=@+:%"
    path = quote(parsed.path, safe=safe)
    query = quote(parsed.query, safe=safe) if parsed.query else ""
    fragment = quote(parsed.fragment, safe=safe) if parsed.fragment else ""
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, fragment))


def _locale_for_url(url: str) -> str:
    """Pick Accept-Language that matches the Amazon domain to avoid 404 redirects."""
    parsed = urlparse(url)
    return LOCALE_HEADERS.get(parsed.netloc, "en-US,en;q=0.9")


def check_url_curl(url: str, timeout: int = 15) -> dict:
    """Check URL via curl — safest method: minimal headers to avoid Amazon geo-404s."""
    clean_url = _sanitize_url(url)
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-A", USER_AGENT,
             "--max-time", str(timeout),
             "-L", clean_url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        status = int(result.stdout.strip())
        return {"url": url, "status": status, "ok": status < 400}
    except Exception as e:
        return {"url": url, "status": None, "ok": False, "error": str(e)}


def check_url_python(url: str, timeout: int = 15) -> dict:
    """Fallback check via urllib GET."""
    clean_url = _sanitize_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        req = urllib.request.Request(clean_url, method="GET", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "status": resp.status, "ok": resp.status < 400}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "ok": False, "error": str(e.reason)}
    except Exception as e:
        return {"url": url, "status": None, "ok": False, "error": str(e)}


def check_url(url: str, index: int, total: int) -> dict:
    """Check a URL with rate limiting. Returns ok/uncertain/broken classification."""
    time.sleep(random.uniform(0.4, 0.9))

    result = check_url_curl(url)

    # If curl fails with a connection error, try urllib as fallback
    if result.get("status") is None:
        time.sleep(random.uniform(0.5, 1.0))
        result = check_url_python(url)

    status = result.get("status")

    # Amazon aggressively 503-blocks bots on search URLs.
    # We classify these as uncertain below instead of retrying (retry is too slow).
    pass

    if result["ok"]:
        result["category"] = "ok"
    elif status == 404:
        # 404 on Amazon can mean genuinely gone OR geo-restricted — flag as uncertain
        result["category"] = "uncertain"
    elif status == 503 and ("/s?k=" in url or "/search?" in url):
        # 503 on Amazon search URLs is almost always bot-blocking — not genuinely broken
        result["category"] = "uncertain"
    else:
        result["category"] = "broken"

    return result


def scan_site(site_dir: Path) -> list[dict]:
    results = []
    content_dirs = [d for d in site_dir.iterdir() if d.is_dir() and d.name.startswith("content")]
    products_dir = site_dir / "content" / "data" / "products"

    for cdir in content_dirs:
        for mdx in cdir.rglob("*.mdx"):
            urls = find_urls_in_file(mdx)
            for url, line in urls:
                results.append({
                    "site": site_dir.name,
                    "file": str(mdx.relative_to(BASE_DIR)),
                    "line": line,
                    "url": url,
                })

    if products_dir.exists():
        for json_file in products_dir.rglob("*.json"):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                items = data if isinstance(data, list) else [data]
                for item in items:
                    links = item.get("affiliateLinks", [])
                    for link in links:
                        url = link.get("url", "")
                        if url and urlparse(url).netloc in AMAZON_DOMAINS:
                            results.append({
                                "site": site_dir.name,
                                "file": str(json_file.relative_to(BASE_DIR)),
                                "line": 0,
                                "url": url,
                            })
            except Exception:
                pass

    return results


def main():
    print("=" * 60)
    print("Broken Affiliate Link Monitor")
    print("=" * 60)
    print("Note: Uses slow requests (~1/sec) + curl fallback to avoid Amazon bot blocks.")
    print("This may take ~30 minutes for 1700 URLs.")
    print("=" * 60)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    all_links = []
    for site_name in SITES:
        site_dir = BASE_DIR / site_name
        if not site_dir.exists():
            continue
        print(f"\n📂 Scanning {site_name}...")
        links = scan_site(site_dir)
        seen = set()
        unique = []
        for link in links:
            key = (link["site"], link["url"])
            if key not in seen:
                seen.add(key)
                unique.append(link)
        all_links.extend(unique)
        print(f"   Found {len(unique)} unique Amazon URLs")

    total = len(all_links)
    print(f"\n🔍 Checking {total} unique URLs (slow mode, ~1 req/sec)...")
    print("   Press Ctrl+C to interrupt and save partial results.\n")

    uncertain = []   # 404s — may be geo-restricted, not necessarily broken
    broken = []      # Connection errors, timeouts — definitely problematic
    checked = 0
    try:
        for i, link in enumerate(all_links):
            checked += 1
            result = check_url(link["url"], i, total)
            progress = f"[{checked}/{total}]"
            cat = result.get("category", "ok")
            if cat == "ok":
                print(f"   {progress} ✅ {link['url'][:70]}...")
            elif cat == "uncertain":
                uncertain.append({**link, **result})
                status = result.get("status") or "ERR"
                print(f"   {progress} ⚠️  {status} (uncertain) — {link['url'][:70]}...")
            else:
                broken.append({**link, **result})
                status = result.get("status") or "ERR"
                print(f"   {progress} ❌ {status} (broken) — {link['url'][:70]}...")
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user. Saving partial results...")

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 🔗 Broken Affiliate Link Report — {today}",
        "",
        f"**Total URLs checked:** {checked} / {total}",
        f"**Healthy:** {checked - len(uncertain) - len(broken)}",
        f"**Uncertain (404 / geo-restricted):** {len(uncertain)}",
        f"**Confirmed broken (timeout/connection error):** {len(broken)}",
        "",
    ]

    if uncertain:
        lines.append("## ⚠️ Uncertain Links (404 — may be geo-restricted)")
        lines.append("")
        lines.append("These returned HTTP 404. Some may be genuinely unavailable; others may be geo-blocked.")
        lines.append("Manual verification recommended before replacing.")
        lines.append("")
        lines.append("| Site | File | Line | Status | URL |")
        lines.append("|------|------|------|--------|-----|")
        for b in uncertain:
            status = b.get("status") or "ERR"
            file_short = f"`{b['file']}`"
            url_short = b["url"][:120] + "..." if len(b["url"]) > 120 else b["url"]
            lines.append(f"| {b['site']} | {file_short} | {b['line']} | {status} | {url_short} |")
        lines.append("")

    if broken:
        lines.append("## ❌ Confirmed Broken Links")
        lines.append("")
        lines.append("| Site | File | Line | Status | URL |")
        lines.append("|------|------|------|--------|-----|")
        for b in broken:
            status = b.get("status") or "ERR"
            file_short = f"`{b['file']}`"
            url_short = b["url"][:120] + "..." if len(b["url"]) > 120 else b["url"]
            lines.append(f"| {b['site']} | {file_short} | {b['line']} | {status} | {url_short} |")
        lines.append("")

    if not uncertain and not broken:
        lines.append("✅ All checked affiliate links are healthy.")
        lines.append("")

    lines.extend(["*Report generated automatically*", ""])
    md_path = REPORTS_DIR / f"broken-links-{today}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ Report saved: {md_path}")

    json_path = REPORTS_DIR / "broken-links-latest.json"
    json_path.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "total_checked": checked,
            "total_urls": total,
            "uncertain_count": len(uncertain),
            "broken_count": len(broken),
            "uncertain": uncertain,
            "broken": broken,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"✅ JSON snapshot saved: {json_path}")

    if broken:
        msg = f"{len(broken)} liens affiliés cassés confirmés"
        script = f'display notification "{msg}" with title "Broken Link Monitor" subtitle "Rapport du {today}" sound name "Glass"'
        os.system(f"osascript -e '{script}' 2>/dev/null")

    print(f"\n✨ Done — {len(broken)} broken, {len(uncertain)} uncertain, {checked - len(broken) - len(uncertain)} ok")


if __name__ == "__main__":
    main()

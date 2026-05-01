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
from urllib.parse import urlparse

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

# Browser-like headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
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


def check_url_python(url: str, timeout: int = 15) -> dict:
    """Check URL via urllib GET."""
    try:
        req = urllib.request.Request(url, method="GET", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "status": resp.status, "ok": resp.status < 400}
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "ok": False, "error": str(e.reason)}
    except Exception as e:
        return {"url": url, "status": None, "ok": False, "error": str(e)}


def check_url_curl(url: str, timeout: int = 15) -> dict:
    """Fallback verification using curl — better at bypassing Amazon bot detection."""
    try:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-A", HEADERS["User-Agent"],
             "-H", f"Accept: {HEADERS['Accept']}",
             "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
             "--max-time", str(timeout),
             "-L", url],
            capture_output=True, text=True, timeout=timeout + 5
        )
        status = int(result.stdout.strip())
        return {"url": url, "status": status, "ok": status < 400}
    except Exception as e:
        return {"url": url, "status": None, "ok": False, "error": str(e)}


def check_url(url: str, index: int, total: int) -> dict:
    """Check a URL with rate limiting and fallback verification for 404s."""
    # Rate limit: ~1 req/sec to avoid Amazon bot detection
    time.sleep(random.uniform(0.3, 0.8))

    result = check_url_python(url)

    # If we got a 404, verify with curl once after a delay (many are false positives)
    if result.get("status") == 404:
        time.sleep(random.uniform(1.0, 2.0))
        fallback = check_url_curl(url)
        if fallback["ok"]:
            return {"url": url, "status": fallback["status"], "ok": True, "verified_by": "curl"}
        else:
            return {"url": url, "status": fallback.get("status") or 404, "ok": False, "verified_by": "curl"}

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

    broken = []
    checked = 0
    try:
        for i, link in enumerate(all_links):
            checked += 1
            result = check_url(link["url"], i, total)
            progress = f"[{checked}/{total}]"
            if result["ok"]:
                print(f"   {progress} ✅ {link['url'][:70]}...")
            else:
                broken.append({**link, **result})
                status = result.get("status") or "ERR"
                verified = result.get("verified_by", "")
                vmark = f" [{verified}]" if verified else ""
                print(f"   {progress} ❌ {status}{vmark} — {link['url'][:70]}...")
    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user. Saving partial results...")

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 🔗 Broken Affiliate Link Report — {today}",
        "",
        f"**Total URLs checked:** {checked} / {total}",
        f"**Confirmed broken links:** {len(broken)}",
        "",
    ]

    if not broken:
        lines.append("✅ All checked affiliate links are healthy.")
    else:
        lines.append("| Site | File | Line | Status | URL |")
        lines.append("|------|------|------|--------|-----|")
        for b in broken:
            status = b.get("status") or "ERR"
            verified = b.get("verified_by", "")
            vmark = f" [{verified}]" if verified else ""
            file_short = f"`{b['file']}`"
            url_short = b["url"][:120] + "..." if len(b["url"]) > 120 else b["url"]
            lines.append(f"| {b['site']} | {file_short} | {b['line']} | {status}{vmark} | {url_short} |")

    lines.extend(["", "*Report generated automatically*", ""])
    md_path = REPORTS_DIR / f"broken-links-{today}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ Report saved: {md_path}")

    json_path = REPORTS_DIR / "broken-links-latest.json"
    json_path.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "total_checked": checked,
            "total_urls": total,
            "broken_count": len(broken),
            "broken": broken,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"✅ JSON snapshot saved: {json_path}")

    if broken:
        msg = f"{len(broken)} liens affiliés cassés confirmés"
        script = f'display notification "{msg}" with title "Broken Link Monitor" subtitle "Rapport du {today}" sound name "Glass"'
        os.system(f"osascript -e '{script}' 2>/dev/null")

    print(f"\n✨ Done — {len(broken)} broken out of {checked} checked")


if __name__ == "__main__":
    main()

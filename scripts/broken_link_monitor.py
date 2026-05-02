#!/usr/bin/env python3
"""
Broken Affiliate Link Monitor v2
Scans all MDX files and product JSONs for Amazon /dp/ URLs only,
checks them via HTTP GET with rate limiting, and reports genuinely broken links.

Changes from v1:
- Only checks /dp/ product URLs (skips /s?k= search links — always valid)
- 404 on /dp/ = genuinely broken (not "uncertain")
- Partial save every 50 URLs (survives interrupts)
- amazon.com fallback: if ASIN 404s on .com, checks .fr before flagging
- Seasonal pages flagged separately (high expected rot)
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

# Primary locale domain for each site (used for amazon.com fallback)
SITE_PRIMARY_DOMAIN = {
    "matelas": "amazon.fr",
    "aspirateur": "amazon.fr",
    "bureau": "amazon.fr",
    "cafe": "amazon.fr",
    "pixinstant": "amazon.fr",
}

# Locale detection from file path
LOCALE_DOMAIN_MAP = {
    "content": "amazon.fr",
    "content-en": "amazon.co.uk",  # EN content usually targets UK
    "content-de": "amazon.de",
    "content-es": "amazon.es",
    "content-it": "amazon.it",
}

SEASONAL_SLUG_PATTERNS = re.compile(
    r'(black[-_]?friday|prime[-_]?day|fete[-_]?des[-_]?meres|mothers[-_]?day|'
    r'noel|christmas|soldes|sales|cyber[-_]?monday)',
    re.IGNORECASE
)

URL_RE = re.compile(r'https?://[^\s"\'>\)\]]+')
DP_RE = re.compile(r'/dp/[A-Z0-9]{10}')

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"


def _sanitize_url(url: str) -> str:
    """Properly encode non-ASCII characters in URL path/query."""
    parsed = urlparse(url)
    safe = "/#?&=@+:%"
    path = quote(parsed.path, safe=safe)
    query = quote(parsed.query, safe=safe) if parsed.query else ""
    fragment = quote(parsed.fragment, safe=safe) if parsed.fragment else ""
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, query, fragment))


def check_url_curl(url: str, timeout: int = 15) -> dict:
    """Check URL via curl — safest method: minimal headers."""
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


def extract_asin(url: str) -> str | None:
    """Extract 10-char ASIN from Amazon /dp/ URL."""
    m = re.search(r'/dp/([A-Z0-9]{10})', url)
    return m.group(1) if m else None


def check_url_with_fallback(url: str, site_name: str) -> dict:
    """
    Check a /dp/ URL. For amazon.com 404s, also check the primary locale domain
    before classifying as broken (US inventory divergence).
    """
    time.sleep(random.uniform(0.8, 1.5))

    result = check_url_curl(url)

    # If curl fails with a connection error, try urllib as fallback
    if result.get("status") is None:
        time.sleep(random.uniform(0.5, 1.0))
        result = check_url_python(url)

    status = result.get("status")

    # amazon.com fallback: if 404, check same ASIN on primary locale domain
    parsed = urlparse(url)
    if status == 404 and parsed.netloc in ("amazon.com", "www.amazon.com"):
        asin = extract_asin(url)
        if asin:
            primary_domain = SITE_PRIMARY_DOMAIN.get(site_name, "amazon.fr")
            fallback_url = f"https://www.{primary_domain}/dp/{asin}"
            time.sleep(random.uniform(0.5, 1.0))
            fb_result = check_url_curl(fallback_url)
            if fb_result.get("ok"):
                result["category"] = "ok"
                result["note"] = f"amazon.com 404 but works on {primary_domain}"
                return result

    if result["ok"]:
        result["category"] = "ok"
    elif status == 404:
        # 404 on /dp/ = genuinely broken product page
        result["category"] = "broken"
    elif status == 503:
        # 503 on /dp/ is usually bot-blocking; flag as uncertain for retry
        result["category"] = "uncertain"
    else:
        result["category"] = "broken"

    return result


def is_seasonal_file(path: Path) -> bool:
    """Detect seasonal pages by slug patterns."""
    return bool(SEASONAL_SLUG_PATTERNS.search(path.name))


def find_urls_in_file(path: Path) -> list[tuple[str, int]]:
    """Find only /dp/ Amazon URLs in a file."""
    urls = []
    try:
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            for match in URL_RE.findall(line):
                parsed = urlparse(match)
                if parsed.netloc in AMAZON_DOMAINS and DP_RE.search(match):
                    urls.append((match, i))
    except Exception:
        pass
    return urls


def scan_site(site_dir: Path) -> list[dict]:
    results = []
    content_dirs = [d for d in site_dir.iterdir() if d.is_dir() and d.name.startswith("content")]
    products_dir = site_dir / "content" / "data" / "products"

    for cdir in content_dirs:
        for mdx in cdir.rglob("*.mdx"):
            urls = find_urls_in_file(mdx)
            seasonal = is_seasonal_file(mdx)
            for url, line in urls:
                results.append({
                    "site": site_dir.name,
                    "file": str(mdx.relative_to(BASE_DIR)),
                    "line": line,
                    "url": url,
                    "seasonal": seasonal,
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
                        if url and urlparse(url).netloc in AMAZON_DOMAINS and DP_RE.search(url):
                            results.append({
                                "site": site_dir.name,
                                "file": str(json_file.relative_to(BASE_DIR)),
                                "line": 0,
                                "url": url,
                                "seasonal": False,
                            })
            except Exception:
                pass

    return results


def save_checkpoint(checked: list, uncertain: list, broken: list, seasonal_broken: list, checkpoint_num: int):
    """Save partial results so an interrupt doesn't lose everything."""
    checkpoint_path = REPORTS_DIR / f"broken-links-checkpoint-{checkpoint_num}.json"
    checkpoint_path.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "checkpoint": checkpoint_num,
            "checked_so_far": len(checked),
            "uncertain_count": len(uncertain),
            "broken_count": len(broken),
            "seasonal_broken_count": len(seasonal_broken),
            "checked": checked,
            "uncertain": uncertain,
            "broken": broken,
            "seasonal_broken": seasonal_broken,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"   💾 Checkpoint {checkpoint_num} saved ({len(checked)} checked)")


def main():
    print("=" * 60)
    print("Broken Affiliate Link Monitor v2")
    print("=" * 60)
    print("Only checks /dp/ product URLs. Search links (/s?k=) are skipped.")
    print("Amazon.com 404s get fallback-checked on the site's primary domain.")
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
        print(f"   Found {len(unique)} unique /dp/ URLs")

    total = len(all_links)
    print(f"\n🔍 Checking {total} /dp/ URLs (~1 req/sec)...")
    print("   Press Ctrl+C to interrupt and save partial results.\n")

    uncertain = []
    broken = []
    seasonal_broken = []
    checked = []
    checkpoint_counter = 0

    try:
        for i, link in enumerate(all_links):
            result = check_url_with_fallback(link["url"], link["site"])
            checked.append({**link, **result})
            progress = f"[{i+1}/{total}]"
            cat = result.get("category", "ok")

            if cat == "ok":
                note = result.get("note", "")
                icon = "🌐" if note else "✅"
                print(f"   {progress} {icon} {link['url'][:70]}...")
            elif cat == "uncertain":
                uncertain.append({**link, **result})
                status = result.get("status") or "ERR"
                print(f"   {progress} ⚠️  {status} (uncertain) — {link['url'][:70]}...")
            else:
                if link.get("seasonal"):
                    seasonal_broken.append({**link, **result})
                    status = result.get("status") or "ERR"
                    print(f"   {progress} 🎄 {status} (seasonal broken) — {link['url'][:70]}...")
                else:
                    broken.append({**link, **result})
                    status = result.get("status") or "ERR"
                    print(f"   {progress} ❌ {status} (broken) — {link['url'][:70]}...")

            # Save checkpoint every 50 URLs
            if (i + 1) % 50 == 0:
                checkpoint_counter += 1
                save_checkpoint(checked, uncertain, broken, seasonal_broken, checkpoint_counter)

    except KeyboardInterrupt:
        print("\n\n⚠️ Interrupted by user. Saving partial results...")

    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"# 🔗 Broken Affiliate Link Report — {today}",
        "",
        f"**Total /dp/ URLs checked:** {len(checked)} / {total}",
        f"**Healthy:** {len([c for c in checked if c.get('category') == 'ok'])}",
        f"**Uncertain (503/bot-blocked):** {len(uncertain)}",
        f"**Confirmed broken:** {len(broken)}",
        f"**Seasonal broken (expected rot):** {len(seasonal_broken)}",
        "",
    ]

    if seasonal_broken:
        lines.append("## 🎄 Seasonal Broken Links (High Expected Rot)")
        lines.append("")
        lines.append("These are from seasonal pages (Black Friday, Prime Day, etc). "
                     "Products are often delisted after events. Consider converting to `/s?k=` search links.")
        lines.append("")
        lines.append("| Site | File | Line | Status | URL |")
        lines.append("|------|------|------|--------|-----|")
        for b in seasonal_broken:
            status = b.get("status") or "ERR"
            file_short = f"`{b['file']}`"
            url_short = b["url"][:120] + "..." if len(b["url"]) > 120 else b["url"]
            lines.append(f"| {b['site']} | {file_short} | {b['line']} | {status} | {url_short} |")
        lines.append("")

    if uncertain:
        lines.append("## ⚠️ Uncertain Links (503 — likely bot-blocking)")
        lines.append("")
        lines.append("These returned HTTP 503. Retry later or verify manually.")
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
        lines.append("## ❌ Confirmed Broken /dp/ Links")
        lines.append("")
        lines.append("| Site | File | Line | Status | URL |")
        lines.append("|------|------|------|--------|-----|")
        for b in broken:
            status = b.get("status") or "ERR"
            file_short = f"`{b['file']}`"
            url_short = b["url"][:120] + "..." if len(b["url"]) > 120 else b["url"]
            lines.append(f"| {b['site']} | {file_short} | {b['line']} | {status} | {url_short} |")
        lines.append("")

    if not uncertain and not broken and not seasonal_broken:
        lines.append("✅ All checked /dp/ affiliate links are healthy.")
        lines.append("")

    lines.extend(["*Report generated automatically*", ""])
    md_path = REPORTS_DIR / f"broken-links-{today}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ Report saved: {md_path}")

    json_path = REPORTS_DIR / "broken-links-latest.json"
    json_path.write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(),
            "total_checked": len(checked),
            "total_urls": total,
            "uncertain_count": len(uncertain),
            "broken_count": len(broken),
            "seasonal_broken_count": len(seasonal_broken),
            "uncertain": uncertain,
            "broken": broken,
            "seasonal_broken": seasonal_broken,
        }, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(f"✅ JSON snapshot saved: {json_path}")

    # Clean up old checkpoints
    for cp in REPORTS_DIR.glob("broken-links-checkpoint-*.json"):
        cp.unlink()
    print("🧹 Old checkpoints cleaned up")

    if broken:
        msg = f"{len(broken)} liens affiliés /dp/ cassés confirmés"
        script = f'display notification "{msg}" with title "Broken Link Monitor" subtitle "Rapport du {today}" sound name "Glass"'
        os.system(f"osascript -e '{script}' 2>/dev/null")

    print(f"\n✨ Done — {len(broken)} broken, {len(seasonal_broken)} seasonal, {len(uncertain)} uncertain, {len(checked) - len(broken) - len(seasonal_broken) - len(uncertain)} ok")


if __name__ == "__main__":
    main()

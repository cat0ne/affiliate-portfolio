#!/usr/bin/env python3
"""
GSC Sitemap (Re)Submitter

Submits the sitemap.xml for each affiliation site to its corresponding Google
Search Console property. Run after a deploy to nudge Google to recrawl.

Usage:
    python scripts/gsc_submit_sitemaps.py                 # submit all sites
    python scripts/gsc_submit_sitemaps.py --site cafe     # one site
    python scripts/gsc_submit_sitemaps.py --dry-run       # show plan, no API calls

Note: requires the credentials at gsc-credentials.json to have the writable
scope `webmasters` (not just `webmasters.readonly`). If the service account
was provisioned read-only, IAM grant must be widened — the script will
surface a clear 403 in that case.
"""

import argparse
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = Path(__file__).resolve().parent.parent
CREDENTIALS_PATH = BASE_DIR / "gsc-credentials.json"

# Each entry: GSC property → list of sitemap URLs to (re)submit.
# `property` uses the same `sc-domain:` form the existing scripts use.
# Multiple sitemaps per site are supported (matelas has sitemap-images.xml).
SITES = [
    {
        "key": "cafe",
        "property": "sc-domain:brewmance.fr",
        "sitemaps": ["https://www.brewmance.fr/sitemap.xml"],
    },
    {
        "key": "matelas",
        "property": "sc-domain:matelas-expert.fr",
        "sitemaps": [
            "https://www.matelas-expert.fr/sitemap.xml",
            "https://www.matelas-expert.fr/sitemap-images.xml",
        ],
    },
    {
        "key": "aspirateur",
        "property": "sc-domain:top-aspirateur.fr",
        "sitemaps": ["https://www.top-aspirateur.fr/sitemap.xml"],
    },
    {
        "key": "pixinstant",
        "property": "sc-domain:pixinstant.com",
        "sitemaps": ["https://www.pixinstant.com/sitemap.xml"],
    },
    {
        "key": "bureau",
        "property": "sc-domain:bureau-expert.fr",
        "sitemaps": ["https://www.bureau-expert.fr/sitemap.xml"],
    },
]


def get_gsc_service():
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(f"Credentials missing: {CREDENTIALS_PATH}")
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/webmasters"],
    )
    return build("webmasters", "v3", credentials=creds, cache_discovery=False)


def submit_sitemap(service, site_property, sitemap_url, dry_run=False):
    if dry_run:
        print(f"  [dry-run] sitemaps.submit(siteUrl={site_property!r}, feedpath={sitemap_url!r})")
        return True
    try:
        service.sitemaps().submit(siteUrl=site_property, feedpath=sitemap_url).execute()
        print(f"  ✓ submitted {sitemap_url}")
        return True
    except HttpError as e:
        status = e.resp.status if e.resp is not None else "?"
        if status == 403:
            print(f"  ✗ 403 on {sitemap_url} — service account lacks writable webmasters scope")
        elif status == 404:
            print(f"  ✗ 404 on {sitemap_url} — sitemap URL unreachable or property mismatch")
        else:
            print(f"  ✗ HTTP {status} on {sitemap_url}: {e}")
        return False


def get_sitemap_status(service, site_property, sitemap_url):
    try:
        return service.sitemaps().get(siteUrl=site_property, feedpath=sitemap_url).execute()
    except HttpError:
        return None


def main():
    parser = argparse.ArgumentParser(description="Submit/resubmit sitemaps to GSC.")
    parser.add_argument(
        "--site",
        choices=[s["key"] for s in SITES],
        help="Limit to one site (default: all).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be submitted without calling the API.",
    )
    parser.add_argument(
        "--show-status",
        action="store_true",
        help="After submission, fetch and print sitemap status from GSC.",
    )
    args = parser.parse_args()

    sites = [s for s in SITES if not args.site or s["key"] == args.site]
    service = get_gsc_service() if not args.dry_run else None

    total_ok = 0
    total_fail = 0
    for site in sites:
        print(f"\n[{site['key']}] {site['property']}")
        for sitemap_url in site["sitemaps"]:
            ok = submit_sitemap(service, site["property"], sitemap_url, dry_run=args.dry_run)
            if ok:
                total_ok += 1
                if args.show_status and not args.dry_run:
                    status = get_sitemap_status(service, site["property"], sitemap_url)
                    if status:
                        last_submitted = status.get("lastSubmitted", "?")
                        last_downloaded = status.get("lastDownloaded", "?")
                        warnings = status.get("warnings", "0")
                        errors = status.get("errors", "0")
                        print(
                            f"    status: lastSubmitted={last_submitted} "
                            f"lastDownloaded={last_downloaded} warnings={warnings} errors={errors}"
                        )
            else:
                total_fail += 1

    print(f"\n=== Summary: {total_ok} submitted, {total_fail} failed ===")
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()

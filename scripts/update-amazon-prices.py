#!/usr/bin/env python3
"""
Amazon Price Auto-Update Script

Updates public/amazon-prices.json across all affiliation sites by fetching
current prices from Amazon Creators API (preferred) or PA-API (fallback).

NOTE (2026-04-27): Creators API credentials are ACTIVE in the Associates
  dashboard but the API returns AssociateNotEligible. This is a propagation
  delay — retry after 24-48h. See scripts/AMAZON_API_STATUS.md for details.

Usage:
    python3 update-amazon-prices.py [--dry-run]

Auth Method 1 — Amazon Creators API (RECOMMENDED):
    Set environment variables:
        CREATORS_API_CLIENT_ID      - Your Creators API credential ID
        CREATORS_API_CLIENT_SECRET  - Your Creators API credential secret

Auth Method 2 — PA-API OAuth 2.0:
    Set environment variables:
        PA_API_CLIENT_ID      - Login with Amazon OAuth client ID
        PA_API_CLIENT_SECRET  - Login with Amazon OAuth client secret

Auth Method 3 — PA-API AWS SigV4 (legacy):
    Set environment variables:
        PA_API_ACCESS_KEY     - Your AWS Access Key for PA-API
        PA_API_SECRET_KEY     - Your AWS Secret Key for PA-API

Partner tags are extracted automatically from affiliate URLs.

If no credentials are configured, the script prints a message and exits
with code 0 (no failure) so it can be used in CI safely.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


# --- Configuration ---
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

SITES = ["matelas", "aspirateur", "cafe", "pixinstant", "bureau"]

# Marketplace mapping for Creators API & PA-API
MARKETPLACES: dict[str, dict[str, str]] = {
    "fr": {"host": "webservices.amazon.fr", "region": "eu-west-1", "marketplace": "www.amazon.fr", "creators_api": "https://creatorsapi.amazon"},
    "de": {"host": "webservices.amazon.de", "region": "eu-west-1", "marketplace": "www.amazon.de", "creators_api": "https://creatorsapi.amazon"},
    "es": {"host": "webservices.amazon.es", "region": "eu-west-1", "marketplace": "www.amazon.es", "creators_api": "https://creatorsapi.amazon"},
    "it": {"host": "webservices.amazon.it", "region": "eu-west-1", "marketplace": "www.amazon.it", "creators_api": "https://creatorsapi.amazon"},
    "co.uk": {"host": "webservices.amazon.co.uk", "region": "eu-west-1", "marketplace": "www.amazon.co.uk", "creators_api": "https://creatorsapi.amazon"},
    "com": {"host": "webservices.amazon.com", "region": "us-east-1", "marketplace": "www.amazon.com", "creators_api": "https://creatorsapi.amazon"},
}

PA_API_SERVICE = "ProductAdvertisingAPI"
PA_API_TARGET = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems"


# ---------------------------------------------------------------------------
# Creators API helpers
# ---------------------------------------------------------------------------

def get_creators_api_token(client_id: str, client_secret: str) -> str:
    """Get OAuth token for Creators API via Login with Amazon (LWA)."""
    payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "creatorsapi::default",
    }
    response = requests.post(
        "https://api.amazon.co.uk/auth/o2/token",
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {data}")
    return token


def creators_api_get_items(
    asins: list[str],
    partner_tag: str,
    marketplace: str,
    access_token: str,
) -> dict[str, Any]:
    """Call Amazon Creators API GetItems for a batch of ASINs.

    marketplace must be the full domain, e.g. 'www.amazon.fr'.
    """
    endpoint = "https://creatorsapi.amazon/catalog/v1/getItems"

    payload = {
        "itemIds": asins,
        "itemIdType": "ASIN",
        "resources": [
            "offersV2.listings.price",
            "itemInfo.title",
        ],
        "partnerTag": partner_tag,
        "partnerType": "Associates",
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "x-marketplace": marketplace,
    }

    response = _creators_api_call(endpoint, headers, payload)
    return response.json()


def creators_api_search_items(
    partner_tag: str,
    marketplace: str,
    access_token: str,
    keywords: str | None = None,
    search_index: str = "All",
    item_count: int = 10,
    brand: str | None = None,
    min_price: int | None = None,
    max_price: int | None = None,
    sort_by: str | None = None,
    resources: list[str] | None = None,
) -> dict[str, Any]:
    """Call Amazon Creators API SearchItems.

    marketplace must be the full domain, e.g. 'www.amazon.fr'.
    Returns up to 10 items per request.
    """
    endpoint = "https://creatorsapi.amazon/catalog/v1/searchItems"

    payload: dict[str, Any] = {
        "partnerTag": partner_tag,
        "partnerType": "Associates",
        "searchIndex": search_index,
        "itemCount": max(1, min(item_count, 10)),
    }
    if keywords:
        payload["keywords"] = keywords
    if brand:
        payload["brand"] = brand
    if min_price is not None:
        payload["minPrice"] = min_price
    if max_price is not None:
        payload["maxPrice"] = max_price
    if sort_by:
        payload["sortBy"] = sort_by
    payload["resources"] = resources or [
        "itemInfo.title",
        "offersV2.listings.price",
        "images.primary.medium",
    ]

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "x-marketplace": marketplace,
    }

    response = _creators_api_call(endpoint, headers, payload)
    return response.json()


def creators_api_get_variations(
    asin: str,
    partner_tag: str,
    marketplace: str,
    access_token: str,
    variation_page: int = 1,
    variation_count: int = 10,
    resources: list[str] | None = None,
) -> dict[str, Any]:
    """Call Amazon Creators API GetVariations for a parent or child ASIN.

    marketplace must be the full domain, e.g. 'www.amazon.fr'.
    Returns all product variants (size, color, etc.) with price ranges.
    """
    endpoint = "https://creatorsapi.amazon/catalog/v1/getVariations"

    payload: dict[str, Any] = {
        "asin": asin,
        "partnerTag": partner_tag,
        "partnerType": "Associates",
        "variationPage": variation_page,
        "variationCount": max(1, min(variation_count, 10)),
        "resources": resources or [
            "itemInfo.title",
            "offersV2.listings.price",
            "variationSummary.price.highestPrice",
            "variationSummary.price.lowestPrice",
            "variationSummary.variationDimension",
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "x-marketplace": marketplace,
    }

    response = _creators_api_call(endpoint, headers, payload)
    return response.json()


def creators_api_get_browse_nodes(
    browse_node_ids: list[str],
    partner_tag: str,
    marketplace: str,
    access_token: str,
    resources: list[str] | None = None,
) -> dict[str, Any]:
    """Call Amazon Creators API GetBrowseNodes.

    marketplace must be the full domain, e.g. 'www.amazon.fr'.
    Returns category hierarchy information.
    Response: {"browseNodesResult": {"browseNodes": [...]}, "errors": [...]}
    """
    endpoint = "https://creatorsapi.amazon/catalog/v1/getBrowseNodes"

    payload: dict[str, Any] = {
        "browseNodeIds": browse_node_ids,
        "partnerTag": partner_tag,
        "partnerType": "Associates",
        "resources": resources or [
            "browseNodes.ancestor",
            "browseNodes.children",
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "x-marketplace": marketplace,
    }

    response = _creators_api_call(endpoint, headers, payload)
    return response.json()


def _creators_api_call(endpoint: str, headers: dict[str, str], payload: dict[str, Any]) -> requests.Response:
    """Make a Creators API call and handle common errors."""
    response = requests.post(endpoint, headers=headers, json=payload, timeout=30)
    if response.status_code == 403:
        try:
            err = response.json()
            if err.get("reason") == "AssociateNotEligible":
                print(
                    "\n   ❌ Creators API: Account not eligible.\n"
                    "      Your Amazon Associates account does not meet the eligibility\n"
                    "      requirements for the Creators API.\n\n"
                    "      Next steps:\n"
                    "      1. Log into https://affiliate-program.amazon.com\n"
                    "      2. Go to Tools → Product Advertising API\n"
                    "      3. Register your AWS access key for PA-API access, OR\n"
                    "      4. Apply for Creators API eligibility if available\n"
                )
                response.raise_for_status()
        except json.JSONDecodeError:
            pass
    response.raise_for_status()
    return response


# ---------------------------------------------------------------------------
# PA-API OAuth 2.0 helpers
# ---------------------------------------------------------------------------

def get_paapi_oauth_token(client_id: str, client_secret: str) -> str:
    """Exchange OAuth client credentials for a PA-API access token."""
    payload = {
        "grant_type": "client_credentials",
        "scope": "advertising::campaign_management",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    response = requests.post(
        "https://api.amazon.com/auth/o2/token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in OAuth response: {data}")
    return token


def paapi_get_items_oauth(
    asins: list[str],
    partner_tag: str,
    marketplace_cfg: dict[str, str],
    access_token: str,
) -> dict[str, Any]:
    """Call PA-API 5.0 GetItems using OAuth 2.0 Bearer token."""
    host = marketplace_cfg["host"]
    marketplace = marketplace_cfg["marketplace"]
    endpoint = f"https://{host}/paapi5/getitems"

    payload = {
        "ItemIds": asins,
        "Resources": [
            "Offers.Listings.Price",
            "ItemInfo.Title",
        ],
        "PartnerTag": partner_tag,
        "PartnerType": "Associates",
        "Marketplace": marketplace,
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Host": host,
        "Authorization": f"Bearer {access_token}",
        "X-Amz-Target": PA_API_TARGET,
    }

    response = requests.post(endpoint, headers=headers, data=payload_bytes, timeout=30)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# AWS Signature Version 4 helpers
# ---------------------------------------------------------------------------

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


def paapi_get_items_aws_sigv4(
    asins: list[str],
    partner_tag: str,
    marketplace_cfg: dict[str, str],
    access_key: str,
    secret_key: str,
) -> dict[str, Any]:
    """Call PA-API 5.0 GetItems using AWS Signature Version 4."""
    host = marketplace_cfg["host"]
    region = marketplace_cfg["region"]
    marketplace = marketplace_cfg["marketplace"]
    endpoint = f"https://{host}/paapi5/getitems"

    payload = {
        "ItemIds": asins,
        "Resources": [
            "Offers.Listings.Price",
            "ItemInfo.Title",
        ],
        "PartnerTag": partner_tag,
        "PartnerType": "Associates",
        "Marketplace": marketplace,
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    t = datetime.now(timezone.utc)
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = t.strftime("%Y%m%d")

    method = "POST"
    canonical_uri = "/paapi5/getitems"
    canonical_querystring = ""
    canonical_headers = (
        f"content-type:application/json; charset=UTF-8\n"
        f"host:{host}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-date"
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    canonical_request = "\n".join(
        [
            method,
            canonical_uri,
            canonical_querystring,
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{PA_API_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            algorithm,
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    signing_key = _get_signature_key(secret_key, date_stamp, region, PA_API_SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth_header = (
        f"{algorithm} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Host": host,
        "X-Amz-Date": amz_date,
        "Authorization": auth_header,
        "X-Amz-Target": PA_API_TARGET,
    }

    response = requests.post(endpoint, headers=headers, data=payload_bytes, timeout=30)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Discovery & parsing helpers
# ---------------------------------------------------------------------------

def extract_marketplace_from_url(url: str) -> str | None:
    """Extract marketplace suffix (fr, de, es, it, co.uk, com) from an Amazon URL."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if not netloc.startswith("amazon."):
        return None
    suffix = netloc.replace("amazon.", "")
    return suffix if suffix in MARKETPLACES else None


def extract_partner_tag(url: str) -> str | None:
    """Extract the 'tag' query parameter from an Amazon affiliate URL."""
    match = re.search(r"[?&]tag=([^&]+)", url)
    return match.group(1) if match else None


def discover_price_files() -> dict[str, Path]:
    """Discover public/amazon-prices.json files across all sites."""
    files: dict[str, Path] = {}
    for site in SITES:
        path = PROJECT_ROOT / site / "public" / "amazon-prices.json"
        if path.exists():
            files[site] = path
    return files


def load_prices(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_prices(path: Path, data: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def format_static_price(price: int | float) -> str:
    return f"{float(price):.2f}"


# ---------------------------------------------------------------------------
# Stale-price detector runner
# ---------------------------------------------------------------------------

def run_stale_price_detector(site_dir: Path) -> None:
    script = site_dir / "scripts" / "detect-stale-prices.ts"
    if not script.exists():
        return
    print(f"  🔍 Running stale-price detector for {site_dir.name}…")
    try:
        result = subprocess.run(
            ["npx", "tsx", str(script)],
            cwd=str(site_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"     {line}")
        if result.returncode != 0 and result.stderr:
            for line in result.stderr.strip().split("\n"):
                print(f"     ⚠️  {line}")
    except FileNotFoundError:
        print(f"     ⚠️  npx/tsx not found, skipping stale-price detector")
    except subprocess.TimeoutExpired:
        print(f"     ⚠️  Stale-price detector timed out")
    except Exception as exc:
        print(f"     ⚠️  Stale-price detector failed: {exc}")


# ---------------------------------------------------------------------------
# Auth test helper
# ---------------------------------------------------------------------------

def test_auth() -> int:
    """Test all configured auth methods and report results."""
    print("=" * 60)
    print("Amazon API Auth Test")
    print("=" * 60)

    creators_client_id = os.environ.get("CREATORS_API_CLIENT_ID", "").strip()
    creators_client_secret = os.environ.get("CREATORS_API_CLIENT_SECRET", "").strip()
    paapi_oauth_client_id = os.environ.get("PA_API_CLIENT_ID", "").strip()
    paapi_oauth_client_secret = os.environ.get("PA_API_CLIENT_SECRET", "").strip()
    aws_access_key = os.environ.get("PA_API_ACCESS_KEY", "").strip()
    aws_secret_key = os.environ.get("PA_API_SECRET_KEY", "").strip()

    results = []

    # Test 1: Creators API
    if creators_client_id and creators_client_secret:
        print("\n1️⃣  Creators API (CREATORS_API_CLIENT_ID + CREATORS_API_CLIENT_SECRET)")
        try:
            token = get_creators_api_token(creators_client_id, creators_client_secret)
            print("   ✅ Token acquired")
            # Try a single-item call to test full access
            try:
                creators_api_get_items(
                    ["B0DT13JGY2"], "zoomzen05-21", "www.amazon.fr", token
                )
                print("   ✅ API call succeeded — full access confirmed")
                results.append(("Creators API", "OK"))
            except requests.exceptions.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 403:
                    err = exc.response.json()
                    reason = err.get("reason", "")
                    if reason == "AssociateNotEligible":
                        print("   ❌ Account not eligible for Creators API")
                        results.append(("Creators API", "NOT_ELIGIBLE"))
                    else:
                        print(f"   ❌ API call failed: {err.get('message', reason)}")
                        results.append(("Creators API", "API_ERROR"))
                else:
                    print(f"   ❌ API call failed: {exc}")
                    results.append(("Creators API", "API_ERROR"))
        except Exception as exc:
            print(f"   ❌ Token failed: {exc}")
            results.append(("Creators API", "TOKEN_FAILED"))
    else:
        print("\n1️⃣  Creators API — not configured")
        results.append(("Creators API", "NOT_CONFIGURED"))

    # Test 2: PA-API OAuth
    if paapi_oauth_client_id and paapi_oauth_client_secret:
        print("\n2️⃣  PA-API OAuth 2.0 (PA_API_CLIENT_ID + PA_API_CLIENT_SECRET)")
        try:
            token = get_paapi_oauth_token(paapi_oauth_client_id, paapi_oauth_client_secret)
            print("   ✅ Token acquired")
            results.append(("PA-API OAuth", "TOKEN_OK"))
        except Exception as exc:
            print(f"   ❌ Token failed: {exc}")
            results.append(("PA-API OAuth", "TOKEN_FAILED"))
    else:
        print("\n2️⃣  PA-API OAuth 2.0 — not configured")
        results.append(("PA-API OAuth", "NOT_CONFIGURED"))

    # Test 3: PA-API AWS SigV4
    if aws_access_key and aws_secret_key:
        print("\n3️⃣  PA-API AWS SigV4 (PA_API_ACCESS_KEY + PA_API_SECRET_KEY)")
        print(f"   Access key: {aws_access_key[:8]}...")
        try:
            paapi_get_items_aws_sigv4(
                ["B0DT13JGY2"], "zoomzen05-21", MARKETPLACES["fr"], aws_access_key, aws_secret_key
            )
            print("   ✅ API call succeeded — full access confirmed")
            results.append(("PA-API AWS", "OK"))
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None:
                status = exc.response.status_code
                if status == 404:
                    print("   ❌ Access key not registered in Amazon Associates (404)")
                    print("      → Go to https://affiliate-program.amazon.com → Tools → Product Advertising API")
                    print("      → Register this access key to enable PA-API access")
                    results.append(("PA-API AWS", "NOT_REGISTERED"))
                elif status == 429:
                    print("   ⚠️  Rate limited (429) — credentials are valid but throttled")
                    results.append(("PA-API AWS", "RATE_LIMITED"))
                else:
                    print(f"   ❌ API call failed: {status} {exc.response.text[:200]}")
                    results.append(("PA-API AWS", f"HTTP_{status}"))
            else:
                print(f"   ❌ API call failed: {exc}")
                results.append(("PA-API AWS", "ERROR"))
        except Exception as exc:
            print(f"   ❌ API call failed: {exc}")
            results.append(("PA-API AWS", "ERROR"))
    else:
        print("\n3️⃣  PA-API AWS SigV4 — not configured")
        results.append(("PA-API AWS", "NOT_CONFIGURED"))

    print("\n" + "=" * 60)
    print("Test Results Summary")
    print("=" * 60)
    for name, status in results:
        icon = "✅" if status == "OK" else "❌"
        print(f"   {icon} {name:<20} {status}")
    print("")

    # Return non-zero if nothing works
    if not any(s == "OK" for _, s in results):
        print("⚠️  No working auth method found. Price updates cannot proceed.")
        print("   See error messages above for specific next steps.")
        return 1
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update Amazon product prices across affiliation sites"
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate without writing files")
    parser.add_argument("--test-auth", action="store_true", help="Test API credentials and exit")
    args = parser.parse_args()

    if args.test_auth:
        return test_auth()

    print("=" * 60)
    print("Amazon Price Auto-Update")
    print("=" * 60)

    # 1. Discover price files
    price_files = discover_price_files()
    if not price_files:
        print("No public/amazon-prices.json files found.")
        return 0

    print(f"\n📁 Discovered price files:")
    for site, path in price_files.items():
        print(f"   • {site}: {path.relative_to(PROJECT_ROOT)}")

    # 2. Extract ASINs and group by marketplace / partner tag
    marketplace_batches: dict[str, dict[str, list[tuple[str, str]]]] = defaultdict(
        lambda: defaultdict(list)
    )

    all_asins: set[str] = set()
    site_data: dict[str, dict[str, Any]] = {}

    for site, path in price_files.items():
        data = load_prices(path)
        site_data[site] = data
        for asin, entry in data.items():
            all_asins.add(asin)
            url = entry.get("affiliateUrl", "")
            mp = extract_marketplace_from_url(url)
            if not mp:
                print(f"   ⚠️  Could not detect marketplace for {asin} in {site}")
                continue
            partner_tag = extract_partner_tag(url)
            if not partner_tag:
                print(f"   ⚠️  Could not detect partner tag for {asin} in {site}")
                continue
            marketplace_batches[mp][partner_tag].append((site, asin))

    print(f"\n🆔 Found {len(all_asins)} unique ASIN(s) across {len(price_files)} site(s)")

    # 3. Detect auth method and validate credentials
    creators_client_id = os.environ.get("CREATORS_API_CLIENT_ID", "").strip()
    creators_client_secret = os.environ.get("CREATORS_API_CLIENT_SECRET", "").strip()
    paapi_oauth_client_id = os.environ.get("PA_API_CLIENT_ID", "").strip()
    paapi_oauth_client_secret = os.environ.get("PA_API_CLIENT_SECRET", "").strip()
    aws_access_key = os.environ.get("PA_API_ACCESS_KEY", "").strip()
    aws_secret_key = os.environ.get("PA_API_SECRET_KEY", "").strip()

    auth_mode: str | None = None
    auth_submode: str | None = None
    oauth_token: str | None = None

    if creators_client_id and creators_client_secret:
        auth_mode = "creators"
        print("\n🔐 Using Amazon Creators API (CREATORS_API_CLIENT_ID + CREATORS_API_CLIENT_SECRET)")
        try:
            oauth_token = get_creators_api_token(creators_client_id, creators_client_secret)
            print("   ✅ Creators API token acquired successfully")
        except Exception as exc:
            print(f"   ❌ Creators API token acquisition failed: {exc}")
            return 1
    elif paapi_oauth_client_id and paapi_oauth_client_secret:
        auth_mode = "paapi"
        auth_submode = "oauth"
        print("\n🔐 Using PA-API OAuth 2.0 (PA_API_CLIENT_ID + PA_API_CLIENT_SECRET)")
        try:
            oauth_token = get_paapi_oauth_token(paapi_oauth_client_id, paapi_oauth_client_secret)
            print("   ✅ PA-API OAuth token acquired successfully")
        except Exception as exc:
            print(f"   ❌ PA-API OAuth token acquisition failed: {exc}")
            return 1
    elif aws_access_key and aws_secret_key:
        auth_mode = "paapi"
        auth_submode = "aws"
        print("\n🔐 Using PA-API AWS SigV4 (PA_API_ACCESS_KEY + PA_API_SECRET_KEY)")
    else:
        print("\n⏸️  API credentials not configured.")
        print("   Set ONE of the following credential pairs:")
        print("       Creators API (recommended): CREATORS_API_CLIENT_ID + CREATORS_API_CLIENT_SECRET")
        print("       PA-API OAuth:               PA_API_CLIENT_ID + PA_API_CLIENT_SECRET")
        print("       PA-API AWS (legacy):        PA_API_ACCESS_KEY + PA_API_SECRET_KEY")
        print("\n   The script will now exit gracefully (no failure).")
        return 0

    # 4. Fetch prices
    print("\n🌐 Fetching prices…")
    fetched_prices: dict[str, dict[str, Any]] = {}  # {asin: {price, currency, title}}

    for marketplace, tag_groups in marketplace_batches.items():
        cfg = MARKETPLACES.get(marketplace)
        if not cfg:
            continue

        for partner_tag, items in tag_groups.items():
            asins = list({asin for _, asin in items})
            batch_size = 10
            for i in range(0, len(asins), batch_size):
                batch = asins[i : i + batch_size]
                try:
                    if auth_mode == "creators" and oauth_token:
                        response = creators_api_get_items(
                            batch, partner_tag, cfg["marketplace"], oauth_token
                        )
                    elif auth_mode == "paapi" and auth_submode == "oauth" and oauth_token:
                        response = paapi_get_items_oauth(batch, partner_tag, cfg, oauth_token)
                    else:
                        response = paapi_get_items_aws_sigv4(
                            batch, partner_tag, cfg, aws_access_key, aws_secret_key
                        )

                    # Parse Creators API response
                    if auth_mode == "creators":
                        # Log API-level errors (lowercase keys per Creators API spec)
                        for err in response.get("errors", []):
                            err_msg = err.get("message", "unknown error")
                            err_code = err.get("code", "Unknown")
                            print(f"   ⚠️  Creators API error: {err_code} – {err_msg}")

                        # Parse items (SDK uses 'itemsResult'; docs show 'itemResults' — try both)
                        items_result = response.get("itemsResult") or response.get("itemResults")
                        for item in (items_result or {}).get("items", []):
                            item_asin = item.get("asin")
                            if not item_asin:
                                continue
                            offers = item.get("offersV2", {}).get("listings", [])
                            price = None
                            currency = "EUR"
                            if offers:
                                price_data = offers[0].get("price", {})
                                price = price_data.get("amount")
                                currency = price_data.get("currency", "EUR")
                            title = (
                                item.get("itemInfo", {})
                                .get("title", {})
                                .get("displayValue")
                            )
                            if price is not None:
                                fetched_prices[item_asin] = {
                                    "price": int(float(price)),
                                    "currency": currency,
                                    "title": title,
                                }
                    else:
                        # Parse PA-API response
                        for item_result in response.get("ItemsResult", {}).get("Items", []):
                            item_asin = item_result.get("ASIN")
                            if not item_asin:
                                continue

                            listings = item_result.get("Offers", {}).get("Listings", [])
                            price_info = listings[0].get("Price", {}) if listings else {}
                            amount = price_info.get("Amount")
                            currency = price_info.get("CurrencyCode", "EUR")

                            title = (
                                item_result.get("ItemInfo", {})
                                .get("Title", {})
                                .get("DisplayValue")
                            )

                            if amount is not None:
                                fetched_prices[item_asin] = {
                                    "price": int(float(amount)),
                                    "currency": currency,
                                    "title": title,
                                }
                except requests.exceptions.HTTPError as exc:
                    print(f"   ⚠️  HTTP error for {marketplace} batch {batch}: {exc}")
                except Exception as exc:
                    print(f"   ⚠️  Error fetching {marketplace} batch {batch}: {exc}")

    if not fetched_prices:
        print("\n⚠️  No prices fetched. Exiting without changes.")
        return 0

    # 5. Update site files
    print(f"\n💾 Updating price files{' (dry-run)' if args.dry_run else ''}…")
    changes_summary: list[dict[str, Any]] = []

    for site, path in price_files.items():
        data = site_data[site]
        changed = 0

        for asin, entry in data.items():
            if asin not in fetched_prices:
                continue

            fetched = fetched_prices[asin]
            old_price = entry.get("price")
            new_price = fetched["price"]

            if old_price != new_price:
                changes_summary.append(
                    {
                        "site": site,
                        "asin": asin,
                        "title": entry.get("title", ""),
                        "old_price": old_price,
                        "new_price": new_price,
                        "currency": fetched.get("currency", entry.get("currency", "EUR")),
                    }
                )
                entry["price"] = new_price
                entry["staticPrice"] = format_static_price(new_price)
                entry["dateUpdated"] = datetime.now(timezone.utc).isoformat()
                if fetched.get("title") and entry.get("source") != "static":
                    entry["title"] = fetched["title"]
                changed += 1

        if changed > 0 or args.dry_run:
            save_prices(path, data, args.dry_run)
            print(f"   • {site}: {changed} price(s) updated")
        else:
            print(f"   • {site}: no changes")

    # 6. Run stale-price detectors
    print("\n🔍 Running stale-price detectors…")
    for site in price_files.keys():
        site_dir = PROJECT_ROOT / site
        run_stale_price_detector(site_dir)

    # 7. Summary report
    print("\n" + "=" * 60)
    print("Summary Report")
    print("=" * 60)

    if not changes_summary:
        print("✅ No price changes detected.")
    else:
        print(f"🔄 {len(changes_summary)} price change(s) detected:\n")
        print(f"{'SITE':<12} {'ASIN':<12} {'TITLE':<25} {'OLD':>6} {'NEW':>6}")
        print("-" * 70)
        for c in changes_summary:
            title = c["title"][:24] if c["title"] else ""
            print(
                f"{c['site']:<12} {c['asin']:<12} {title:<25} "
                f"{c['old_price']:>6} {c['new_price']:>6}"
            )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

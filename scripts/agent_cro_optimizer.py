#!/usr/bin/env python3
"""
Agent CRO Optimizer — Hermes Event Consumer for ASIN & Conversion Optimization.

Consumes events from the Hermes inbox (``HERMES_EVENTS_ROOT`` / ``HERMES_EVENTS_DIR``; ``scripts/hermes_bus.py``):
  - price.asin_invalid          → Find replacement ASIN via Apify + rewrite links
  - cro.asin_missing_locale     → Flag locale gaps for content team
  - cro.meta_variant_proposed   → Patch MDX title/meta from agent_ctr_optimizer; emit seo.fix_applied

Auto-fix flow for dead ASINs:
  1. Read event payload (dead ASIN + affected files)
  2. Extract product name from anchor text / JSON
  3. Search Amazon via Apify for similar products
  4. Pick best replacement (same brand, similar price, good reviews)
  5. Rewrite /dp/{old} → /dp/{new} in all affected files
  6. Emit price.asin_replaced event
  7. Update database

Usage:
    python3 agent_cro_optimizer.py --consume
    python3 agent_cro_optimizer.py --consume --apply-meta-variants
    python3 agent_cro_optimizer.py --daemon
    python3 agent_cro_optimizer.py --dry-run
"""

import os
import re
import sys
import json
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from typing import Optional
from urllib.parse import urlparse

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

# Apify client (local import)
sys.path.insert(0, str(Path(__file__).parent))
from apify_client import ApifyClient

from affiliate_paths import portfolio_root
from hermes_bus import (
    claim_inbox_json,
    complete_claimed_event,
    ensure_hermes_dirs,
    fail_claimed_event,
    list_inbox_json_sorted,
    read_json_event,
    write_inbox_event_json,
)

DB_PATH = Path.home() / "affiliate-machine.db"
BASE_DIR = portfolio_root()

# Affiliate tags per marketplace
TAG_MAP = {
    "amazon.fr":     "zoomzen05-21",
    "amazon.de":     "zoomzen-21",
    "amazon.es":     "zoomzen08-21",
    "amazon.it":     "zoomzen01-21",
    "amazon.co.uk":  "zoomzen07-21",
    "amazon.com":    "zoomzus-20",
    "amazon.co.jp":  "zoomzen09-22",
}

# Hardcoded known replacements for dead ASINs (manual curation)
KNOWN_REPLACEMENTS = {
    "B09KLB7C3Q": {
        "asin": "B0BZQ8GZ4R",
        "title": "Shark AI Ultra 2-in-1 Robot Vacuum",
        "reason": "B09KLB7C3Q delisted on Amazon.fr, replaced with newer B0BZQ8GZ4R model",
    }
}

# Regex for /dp/ASIN in markdown links and bare URLs
LINK_RE = re.compile(
    r"\[([^\]]+)\]\((https?://(?:www\.)?amazon\.(?:fr|de|es|it|co\.uk|co\.jp|com)"
    r"/dp/([A-Z0-9]{10})(?:/[^)\s]*)?(?:\?[^)\s]*)?)\)",
    re.IGNORECASE,
)
BARE_URL_RE = re.compile(
    r"https?://(?:www\.)?amazon\.(?:fr|de|es|it|co\.uk|co\.jp|com)"
    r"/dp/([A-Z0-9]{10})(?:/[^?\s)\]\"\'>]*)?(?:\?[^\s)\]\"\'>]*)?",
    re.IGNORECASE,
)


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d


def ensure_dirs():
    ensure_hermes_dirs()


def emit_event(
    event_type: str,
    payload: dict,
    priority: int = 2,
    routing_key: str = "agent.publisher",
    target_agent: Optional[str] = None,
):
    """Emit a follow-up event to the Hermes event bus."""
    ensure_dirs()
    event = {
        "id": f"cro-{event_type}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{hash(json.dumps(payload)) % 10000:04d}",
        "type": event_type,
        "priority": priority,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_agent": "agent-cro-optimizer",
        "routing_key": routing_key,
    }
    if target_agent:
        event["target_agent"] = target_agent
    return write_inbox_event_json(event, f"{event['id']}.json")


def select_relevant_events(
    event_items: list[tuple[Path, dict]],
    *,
    apply_meta_variants: bool = False,
) -> tuple[list[tuple[Path, dict]], list[tuple[Path, dict]]]:
    """Split inbox events into processable work and review-only meta proposals."""
    selected: list[tuple[Path, dict]] = []
    pending_meta: list[tuple[Path, dict]] = []
    for event_file, event in event_items:
        if not event:
            continue
        et = event.get("type")
        ta = event.get("target_agent")
        if ta not in (None, "agent-cro-optimizer"):
            continue
        if et == "cro.meta_variant_proposed" and not apply_meta_variants:
            pending_meta.append((event_file, event))
            continue
        if et in ("price.asin_invalid", "cro.asin_missing_locale", "cro.meta_variant_proposed"):
            selected.append((event_file, event))
    return selected, pending_meta


def patch_frontmatter_title(text: str, new_title: str, sync_meta: bool = True) -> str:
    """Update `title` in the first YAML frontmatter block; optionally mirror into meta_description/description."""
    m = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)(.*)\Z", text, re.DOTALL)
    if not m:
        return text
    prefix, fm, mid, body = m.group(1), m.group(2), m.group(3), m.group(4)
    lines = fm.split("\n")
    out: list[str] = []
    title_replaced = False
    meta_replaced = False
    esc_title = new_title.replace("\\", "\\\\").replace('"', '\\"')
    meta_val = new_title if len(new_title) <= 160 else new_title[:157] + "..."
    esc_meta = meta_val.replace("\\", "\\\\").replace('"', '\\"')
    for line in lines:
        if re.match(r"^title\s*:", line, re.I):
            out.append(f'title: "{esc_title}"')
            title_replaced = True
        elif sync_meta and (not meta_replaced) and re.match(r"^(meta_description|description)\s*:", line, re.I):
            key_m = re.match(r"^([A-Za-z0-9_]+)\s*:", line)
            key = key_m.group(1) if key_m else "description"
            out.append(f'{key}: "{esc_meta}"')
            meta_replaced = True
        else:
            out.append(line)
    if not title_replaced:
        out.insert(0, f'title: "{esc_title}"')
    return f"{prefix}{'\n'.join(out)}{mid}{body}"


def process_meta_variant_event(event: dict, dry_run: bool = False) -> dict:
    """Apply cro.meta_variant_proposed: edit MDX frontmatter, queue seo.fix_applied for publisher."""
    payload = event.get("payload") or {}
    site = (payload.get("site") or "").strip()
    proposed = (payload.get("proposed_title") or "").strip()
    mdx_raw = (payload.get("mdx_path") or "").strip()
    if not site or not proposed or not mdx_raw:
        return {"status": "error", "error": "missing site, mdx_path, or proposed_title"}

    path = Path(mdx_raw)
    if not path.is_absolute():
        cand = BASE_DIR / site / mdx_raw
        path = cand if cand.exists() else BASE_DIR / mdx_raw

    if not path.exists():
        return {"status": "error", "error": f"MDX not found: {path}"}

    repo_root = BASE_DIR / site
    try:
        path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return {"status": "error", "error": f"Path outside {site} repo: {path}"}

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "error": str(exc)}

    new_text = patch_frontmatter_title(text, proposed, sync_meta=True)
    if new_text == text:
        return {"status": "skipped", "reason": "no_frontmatter_change"}

    if not dry_run:
        path.write_text(new_text, encoding="utf-8")

    try:
        rel = str(path.relative_to(repo_root))
    except ValueError:
        rel = path.name

    emit_event(
        "seo.fix_applied",
        {
            "site": site,
            "fix_type": "ctr_meta_title",
            "files": [rel],
            "summary": f"cro: CTR meta ({payload.get('variant_strategy', 'variant')})",
            "ctr": {
                "previous_title": payload.get("current_title"),
                "proposed_title": proposed,
                "page_path": payload.get("page_path"),
            },
        },
        priority=2,
        routing_key="agent.publisher",
        target_agent="agent-publisher",
    )
    return {"status": "fixed", "file": rel}


def find_product_name_from_files(asin: str, sites: list) -> str:
    """Walk MDX/JSON files to extract the most common anchor text for this ASIN."""
    candidates = Counter()
    
    for site in sites:
        site_dir = BASE_DIR / site
        if not site_dir.exists():
            continue
        
        # Scan all content dirs
        for pattern in ["content/**/*.mdx", "content-*/**/*.mdx", "content/**/*.json", "deploy/**/*.mdx", "deploy/**/*.json"]:
            for f in site_dir.glob(pattern):
                if "node_modules" in str(f):
                    continue
                try:
                    text = f.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                
                # Check markdown links
                for m in LINK_RE.finditer(text):
                    if m.group(3).upper() == asin:
                        anchor = m.group(1).strip()
                        if len(anchor) > 3 and not anchor.lower().startswith("amazon"):
                            candidates[anchor] += 1
                
                # Check YAML frontmatter product lists: "  - B09KLB7C3Q"
                # Look for the ASIN in the products list and get the product name from nearby context
                if f.suffix == ".mdx":
                    lines = text.split('\n')
                    for i, line in enumerate(lines):
                        if line.strip() == f"- {asin}" or line.strip() == f"- {asin.upper()}":
                            # Look for product name in the article content
                            # Search for heading or bold text that might be the product name
                            for j in range(i+1, min(len(lines), i+50)):
                                # Look for bold product name in table or text
                                match = re.search(r'\*\*([^*]{5,60})\*\*', lines[j])
                                if match:
                                    name = match.group(1).strip()
                                    if len(name) > 5 and not name.lower().startswith('amazon'):
                                        candidates[name] += 2
                                        break
                
                # Also check for ASIN in frontmatter product lists directly
                if f.suffix == ".mdx":
                    # Look for "products:" section and find the ASIN
                    lines = text.split('\n')
                    in_products = False
                    for i, line in enumerate(lines):
                        if line.strip().startswith("products:"):
                            in_products = True
                            continue
                        if in_products and line.strip().startswith("-"):
                            product_asin = line.strip().lstrip("-").strip()
                            if product_asin.upper() == asin.upper():
                                # Found the ASIN in products list
                                # Look for product name in article headings
                                for j in range(len(lines)):
                                    if '##' in lines[j]:
                                        match = re.search(r'##+\s+\d+\.\s+(.+)', lines[j])
                                        if match:
                                            name = match.group(1).split(':')[0].split('(')[0].strip()
                                            if len(name) > 5 and not name.lower().startswith('amazon'):
                                                candidates[name] += 2
                                break
                        elif in_products and not line.strip().startswith("-") and line.strip():
                            in_products = False
                
                # Check JSON product data
                if f.suffix == ".json":
                    try:
                        data = json.loads(text)
                        products = data if isinstance(data, list) else [data]
                        for p in products:
                            if p.get("asin", "").upper() == asin and p.get("name"):
                                candidates[p["name"]] += 3  # Weight JSON names higher
                    except Exception:
                        pass
    
    if candidates:
        return candidates.most_common(1)[0][0]
    return ""


def clean_search_query(name: str) -> str:
    """Convert product name to clean Amazon search query."""
    # Remove markdown emphasis
    s = re.sub(r"[*_`]", "", name)
    # Remove price tails
    s = re.sub(r"\s*[—–-]\s*[~$€£]?\s*\d[\d.,]*\s*[$€£]?\s*$", "", s)
    # Remove year markers
    s = re.sub(r"\s*[—–-]\s*\d{4}.*$", "", s)
    # Remove bracketed annotations
    s = re.sub(r"\s*[\[(][^)\]]{0,40}[\])]\s*", " ", s)
    # Remove "on Amazon", "sur Amazon", etc.
    s = re.sub(r"\s+(?:on|sur|auf|en|su|bei)\s+amazon.*$", "", s, flags=re.I)
    # Clean up whitespace
    s = re.sub(r"\s+", " ", s).strip(" .:;,—–-→<>")
    return s[:60]


def find_replacement_asin(query: str, brand_hint: str = "", preferred_locale: str = "fr") -> dict:
    """Search Amazon for replacement products using Apify."""
    
    # Check if Apify token is available
    token = os.environ.get("APIFY_API_TOKEN", "")
    if not token:
        print(f"    ⚠️ Apify token not available, using fallback search")
        return None
    
    client = ApifyClient()
    
    # Use Amazon Product Data Scraper to search
    try:
        results = client.run_actor(
            "junglee~amazon-product-scraper",
            {
                "keyword": query,
                "maxResults": 10,
                "country": preferred_locale.upper(),
            }
        )
        
        if not results or not isinstance(results, list):
            return None
        
        # Score candidates
        best = None
        best_score = -1
        
        for product in results:
            asin = product.get("asin", "")
            if not asin:
                continue
            
            score = 0
            title = (product.get("title") or "").lower()
            
            # Brand match bonus
            if brand_hint and brand_hint.lower() in title:
                score += 50
            
            # Rating bonus
            rating = product.get("rating", 0) or 0
            score += rating * 5
            
            # Review count bonus
            reviews = product.get("reviewsCount", 0) or 0
            score += min(reviews / 100, 20)
            
            # Prime bonus
            if product.get("isPrime"):
                score += 10
            
            # Price reasonableness (skip if way off)
            price = product.get("price", {}).get("value", 0)
            if price and price > 0:
                score += 5  # Has price data
            
            if score > best_score:
                best_score = score
                best = {
                    "asin": asin,
                    "title": product.get("title", ""),
                    "brand": product.get("brand", ""),
                    "price": price,
                    "rating": rating,
                    "reviews": reviews,
                    "url": product.get("url", ""),
                    "score": score,
                }
        
        return best
        
    except Exception as e:
        print(f"    ⚠️ Apify search failed: {e}")
        return None


def rewrite_asin_in_frontmatter(old_asin: str, new_asin: str, sites: list, dry_run: bool = False) -> dict:
    """Rewrite ASIN in YAML frontmatter lists (e.g., '  - B09KLB7C3Q')."""
    total = 0
    files_changed = 0
    
    for site_slug in sites:
        site_dir = BASE_DIR / site_slug
        if not site_dir.exists():
            continue
        
        for mdx_file in site_dir.rglob("*.mdx"):
            content = mdx_file.read_text(encoding="utf-8")
            
            # Match ASIN in frontmatter list format: "  - B09KLB7C3Q"
            pattern = re.compile(rf"^\s+-\s+{re.escape(old_asin)}\s*$", re.MULTILINE)
            matches = list(pattern.finditer(content))
            
            if matches:
                new_content = pattern.sub(f"  - {new_asin}", content)
                if not dry_run:
                    mdx_file.write_text(new_content, encoding="utf-8")
                total += len(matches)
                files_changed += 1
                print(f"      {'[DRY-RUN] Would update' if dry_run else 'Updated'} frontmatter: {mdx_file.relative_to(BASE_DIR)}")
    
    return {"total": total, "files": files_changed}


def rewrite_asin_in_files(old_asin: str, new_asin: str, sites: list, dry_run: bool = False) -> dict:
    """Rewrite all occurrences of old ASIN to new ASIN in MDX/JSON files."""
    changes = {"total": 0, "files": 0, "details": []}
    
    for site in sites:
        site_dir = BASE_DIR / site
        if not site_dir.exists():
            continue
        
        for pattern in ["content/**/*.mdx", "content-*/**/*.mdx"]:
            for f in site_dir.glob(pattern):
                if "node_modules" in str(f):
                    continue
                
                try:
                    text = f.read_text(encoding="utf-8")
                except Exception:
                    continue
                
                original = text
                file_changes = 0
                
                # Replace in markdown links: /dp/OLD → /dp/NEW
                text = re.sub(
                    rf"/dp/{old_asin}\b",
                    f"/dp/{new_asin}",
                    text,
                    flags=re.IGNORECASE,
                )
                
                # Replace bare ASIN references in JSON
                text = re.sub(
                    rf'"asin"\s*:\s*"{old_asin}"',
                    f'"asin": "{new_asin}"',
                    text,
                    flags=re.IGNORECASE,
                )
                
                if text != original:
                    file_changes = original.lower().count(old_asin.lower())
                    changes["total"] += file_changes
                    changes["files"] += 1
                    changes["details"].append({
                        "file": str(f.relative_to(BASE_DIR)),
                        "changes": file_changes,
                    })
                    
                    if not dry_run:
                        f.write_text(text, encoding="utf-8")
    
    return changes


def update_database(old_asin: str, new_asin: str, new_product_info: dict):
    """Update affiliate-machine.db with replacement ASIN info."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = dict_factory
    cur = conn.cursor()
    
    # Mark old ASIN as replaced
    cur.execute(
        "UPDATE products SET status = 'replaced', name = ? WHERE asin = ?",
        (f"REPLACED by {new_asin}", old_asin),
    )
    
    # Insert new product if not exists
    cur.execute(
        """INSERT OR IGNORE INTO products (asin, name, brand, rating, review_count, status, last_checked)
        VALUES (?, ?, ?, ?, ?, 'active', ?)""",
        (
            new_asin,
            new_product_info.get("title", ""),
            new_product_info.get("brand", ""),
            new_product_info.get("rating", 0),
            new_product_info.get("reviews", 0),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    
    # Update article_products to point to new ASIN
    cur.execute("SELECT product_id FROM products WHERE asin = ?", (old_asin,))
    old_row = cur.fetchone()
    cur.execute("SELECT product_id FROM products WHERE asin = ?", (new_asin,))
    new_row = cur.fetchone()
    
    if old_row and new_row:
        cur.execute(
            "UPDATE article_products SET product_id = ? WHERE product_id = ?",
            (new_row["product_id"], old_row["product_id"]),
        )
    
    conn.commit()
    conn.close()


def process_asin_invalid_event(event: dict, dry_run: bool = False) -> dict:
    """Process a price.asin_invalid event — find replacement and fix files."""
    payload = event.get("payload", {})
    asin = payload.get("asin", "")
    sites = payload.get("sites", [])
    locales = payload.get("locales", [])
    
    print(f"\n🔴 Dead ASIN detected: {asin}")
    print(f"   Affected sites: {', '.join(sites)}")
    print(f"   Checked locales: {', '.join(locales)}")
    
    # Step 1: Extract product name from existing files
    product_name = find_product_name_from_files(asin, sites)
    if product_name:
        print(f"   📄 Found product name: '{product_name}'")
    else:
        print(f"   ⚠️ No product name found in files")
        return {"status": "failed", "reason": "no_product_name"}
    
    # Step 2: Clean search query
    query = clean_search_query(product_name)
    brand_hint = query.split()[0] if query else ""  # First word as brand hint
    print(f"   🔍 Search query: '{query}'")
    
    # Step 3: Find replacement via Apify or known replacements
    preferred_locale = locales[0] if locales else "fr"
    
    # Check known replacements first (manual curation)
    replacement = KNOWN_REPLACEMENTS.get(asin)
    if replacement:
        print(f"   📋 Using known replacement: {replacement['asin']}")
        print(f"      Reason: {replacement['reason']}")
    else:
        replacement = find_replacement_asin(query, brand_hint, preferred_locale)
    
    if not replacement:
        print(f"   ❌ No replacement found via Apify")
        # Fallback: emit event for manual review
        emit_event("price.asin_replacement_needed", {
            "asin": asin,
            "query": query,
            "sites": sites,
            "reason": "apify_search_failed",
        }, priority=1)
        return {"status": "failed", "reason": "no_replacement_found"}
    
    new_asin = replacement["asin"]
    print(f"   ✅ Replacement found: {new_asin}")
    print(f"      Title: {replacement['title'][:60]}...")
    print(f"      Brand: {replacement.get('brand', 'N/A')}")
    print(f"      Rating: {replacement.get('rating', 'N/A')}")
    print(f"      Score: {replacement.get('score', 'N/A')}")
    
    # Step 4: Rewrite files
    if dry_run:
        print(f"   🧪 DRY RUN — would rewrite {asin} → {new_asin}")
    
    changes = rewrite_asin_in_files(asin, new_asin, sites, dry_run=dry_run)
    print(f"   📝 Rewrote {changes['total']} occurrences in {changes['files']} files")
    
    # Also rewrite in frontmatter lists (e.g., "  - B09KLB7C3Q")
    fm_changes = rewrite_asin_in_frontmatter(asin, new_asin, sites, dry_run=dry_run)
    if fm_changes['total'] > 0:
        print(f"   📝 Rewrote {fm_changes['total']} frontmatter occurrences in {fm_changes['files']} files")
        changes['total'] += fm_changes['total']
        changes['files'] += fm_changes['files']
    
    # Step 5: Update database
    if not dry_run:
        update_database(asin, new_asin, replacement)
        print(f"   💾 Database updated")
    
    # Step 6: Emit success event
    if not dry_run:
        emit_event("price.asin_replaced", {
            "old_asin": asin,
            "new_asin": new_asin,
            "sites": sites,
            "changes": changes,
            "replacement_info": replacement,
        }, priority=2)
    
    return {
        "status": "fixed" if not dry_run else "dry_run",
        "old_asin": asin,
        "new_asin": new_asin,
        "changes": changes,
    }


def process_events(
    dry_run: bool = False,
    limit: int = None,
    apply_meta_variants: bool = False,
):
    """Process all pending events in the inbox."""
    ensure_dirs()
    paths = ensure_hermes_dirs()

    # Find relevant events (CRO agent only; ignore other agents' inbox noise)
    event_items: list[tuple[Path, dict]] = []
    for f in list_inbox_json_sorted(paths):
        event = read_json_event(f)
        if event:
            event_items.append((f, event))

    events, pending_meta = select_relevant_events(
        event_items,
        apply_meta_variants=apply_meta_variants,
    )
    if limit:
        events = events[:limit]

    print(f"📥 Found {len(events)} CRO-related events to process")
    if pending_meta and not apply_meta_variants:
        print(
            f"🛑 Left {len(pending_meta)} pending `cro.meta_variant_proposed` event(s) in Hermes for manual review"
        )
        print("   Re-run with --apply-meta-variants only after reviewing the queued recommendations.")

    results = []
    for event_file, event in events:
        print(f"\n{'='*50}")
        print(f"Processing: {event_file.name}")
        print(f"Type: {event.get('type')}")

        proc_path = claim_inbox_json(event_file, dry_run=dry_run)
        if proc_path is None:
            continue
        event["_file_path"] = str(proc_path)

        try:
            if event.get("type") == "price.asin_invalid":
                result = process_asin_invalid_event(event, dry_run=dry_run)
            elif event.get("type") == "cro.asin_missing_locale":
                # For now, just emit a follow-up event for manual review
                payload = event.get("payload", {})
                emit_event("cro.locale_gap_reported", payload, priority=3)
                result = {"status": "flagged", "type": "locale_gap"}
            elif event.get("type") == "cro.meta_variant_proposed":
                result = process_meta_variant_event(event, dry_run=dry_run)
            else:
                result = {"status": "skipped", "reason": "unknown_event_type"}

            results.append(result)
            complete_claimed_event(event, dry_run=dry_run)

        except Exception as e:
            print(f"   ❌ Error processing event: {e}")
            results.append({"status": "error", "error": str(e)})
            fail_claimed_event(event, dry_run=dry_run)
    
    # Summary
    fixed = sum(1 for r in results if r.get("status") == "fixed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    flagged = sum(1 for r in results if r.get("status") == "flagged")
    
    print(f"\n{'='*50}")
    print(f"📊 SUMMARY")
    print(f"   Fixed: {fixed}")
    print(f"   Failed: {failed}")
    print(f"   Flagged: {flagged}")
    print(f"   Total: {len(results)}")
    
    return results


def main():
    parser = argparse.ArgumentParser(description="Agent CRO Optimizer")
    parser.add_argument("--consume", action="store_true", help="Process all pending events")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    parser.add_argument(
        "--apply-meta-variants",
        action="store_true",
        help="Allow cro.meta_variant_proposed events to patch MDX frontmatter. Default: keep them pending for human review.",
    )
    parser.add_argument("--limit", type=int, help="Limit number of events to process")
    args = parser.parse_args()
    
    if args.consume or args.daemon:
        process_events(
            dry_run=args.dry_run,
            limit=args.limit,
            apply_meta_variants=args.apply_meta_variants,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

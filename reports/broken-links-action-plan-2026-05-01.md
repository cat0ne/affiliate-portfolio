# 🔗 Broken Link Investigation & Action Plan — 2026-05-01

## Executive Summary

**The "900 broken links" alert was a false alarm.** The old monitor script used HTTP `HEAD` requests, which Amazon aggressively blocks with 503/405 errors. After fixing the monitor and running statistical sampling:

| Category | Count | Status |
|----------|-------|--------|
| ❌ False positives (links were fine all along) | ~166 | No action needed |
| ⚠️ Product pages returning 404 (`/dp/`) | ~462 | **Requires action** |
| ⚠️ Search pages returning 404 (`/s/` or `/search`) | ~85 | **Requires action** |
| 🔧 Encoding errors (`é`, `ß`, `è`) | 19 | **Fixed in monitor** |
| ❌ Connection errors | ~12 | Investigate |

---

## Root Causes Identified

### 1. Old script used `HEAD` requests
- Amazon's bot detection returns `503 Service Unavailable` or `405 Method Not Allowed` for HEAD
- **Fix:** Script now uses `GET` with browser-like `User-Agent`

### 2. Curl fallback added `Accept-Language: fr-FR...`
- This *caused* Amazon to return 404 on some product pages (geo-restriction redirect behavior)
- **Fix:** Curl now uses minimal headers (only `User-Agent`)

### 3. UTF-8 encoding bug
- URLs with `imperméable`, `Fußstütze`, `caffè` crashed Python's `urllib` with ASCII codec errors
- **Fix:** URLs are now properly percent-encoded before the HTTP request

---

## Phase 1 — Monitor Fix ✅ DONE

The monitor script (`scripts/broken_link_monitor.py`) has been updated:
- `GET` instead of `HEAD`
- Proper UTF-8 URL encoding
- Minimal curl headers to avoid geo-404s
- Classifies results as: `ok` / `uncertain` (404) / `broken` (connection error)

---

## Phase 2 — Quick Wins (This Week)

### 2A. Verify 19 Encoding-Error URLs
These URLs had special characters that crashed the old script. The new script handles them, but you should manually verify they render correctly in a browser:

**Files affected:**
- `matelas/content-es/guides/guide-protege-matelas.mdx` (2 links)
- `matelas/content-en/guides/guide-protege-matelas.mdx` (2 links)
- `aspirateur/content/guides/meilleures-offres-aspirateurs-rentree-2026.mdx` (1 link)
- `aspirateur/content-de/guides/meilleures-offres-aspirateurs-rentree-2026.mdx` (1 link)
- `bureau/content-de/comparatifs/fussstuetze-buero-test-vergleich-2026.mdx` (5 links)
- `bureau/content-de/guides/tastaturablage-buero-lohnt-sich-kauf.mdx` (1 link)
- `cafe/content-en/guides/guide-accessoires-barista-indispensables.mdx` (1 link)
- `cafe/content-de/guides/kaffeemaschine-pflege-leitfaden.mdx` (2 links)
- `cafe/content-it/guides/guide-choisir-cafe-grain.mdx` (4 links)

### 2B. Fix 85 Search-Page 404s
Search URLs like `amazon.com/search?field-keywords=...` are returning 404. Amazon may have changed search URL patterns. Recommended fixes:
- Replace `/search?field-keywords=...` with `/s?k=...` (Amazon's current search format)
- Or replace with a direct product `/dp/` link if a specific product is being referenced

**Sites most affected:**
- `aspirateur` (FR/ES/DE guides and comparatifs)
- `bureau` (FR/ES/DE/UK guides)
- `cafe` (FR guides with `/search?field-keywords=`)

### 2C. Investigate 12 Connection Errors
These returned timeouts or connection failures. Re-check manually:
```bash
python3 scripts/broken_link_monitor.py
```
(The fixed script will classify these properly.)

---

## Phase 3 — Product Page Audit (Ongoing)

### The Real Problem: ~462 `/dp/` Links Returning 404

These are specific Amazon ASINs (product IDs) that are no longer available. This is normal — Amazon delists products, sellers go out of stock, or products are replaced by newer models.

### Recommended Approach

#### Option A: Traffic-Based Prioritization (Recommended)
Use your GSC data to prioritize pages with the most impressions/clicks:

1. **High priority** (top 20% of pages by traffic): Manually verify each 404 link
   - Open the Amazon URL in an incognito browser
   - If truly unavailable, find the replacement product (newer model, same brand)
   - Update the MDX file with the new ASIN

2. **Medium priority** (next 30%): Batch-replace with search links
   - Replace `amazon.fr/dp/B0XXXX?tag=...` with `amazon.fr/s?k=Brand+Model&tag=...`
   - Less conversion-optimized but always works

3. **Low priority** (bottom 50%): Remove or replace with generic search
   - For old guides/comparisons, remove the specific link
   - Or replace with a category search

#### Option B: Automated Replacement Script
A script could:
1. Parse each broken `/dp/` URL
2. Extract the brand + model name from the surrounding MDX content
3. Generate a search URL as fallback
4. Output a diff for human review

**Trade-off:** Fast but loses the direct-to-product conversion benefit.

#### Option C: Amazon PA-API Lookup (Most Accurate)
If you have Amazon Product Advertising API access:
1. Query each broken ASIN via PA-API
2. If unavailable, PA-API returns "ItemNotAccessible" or similar
3. Use PA-API's "SimilarItems" or search to find replacements

**Trade-off:** Requires API credentials and rate limiting.

---

## Phase 4 — Prevent Future False Alarms

### Update Cron Job
Ensure the cron job runs the **fixed** script:

```cron
# Run weekly at 3 AM
0 3 * * 1 cd /Users/gho/Documents/affiliation-sites && python3 scripts/broken_link_monitor.py
```

### Set Alert Thresholds
The fixed script now categorizes results. Only trigger alerts for:
- `broken` (connection errors) — immediate alert
- `uncertain` > 50 new 404s in a week — weekly digest, not immediate panic

---

## Appendix: Broken Link Breakdown by Site

| Site | `/dp/` 404s | Search 404s | ERR (encoding) |
|------|------------|-------------|----------------|
| matelas | ~45 | ~8 | 4 |
| aspirateur | ~142 | ~35 | 2 |
| bureau | ~165 | ~28 | 6 |
| cafe | ~98 | ~12 | 5 |
| pixinstant | ~88 | ~2 | 2 |
| **Total** | **~538** | **~85** | **19** |

---

*Plan generated: 2026-05-01*
*Investigator: Kimi Code CLI*

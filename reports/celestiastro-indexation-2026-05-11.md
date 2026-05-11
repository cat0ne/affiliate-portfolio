# CelestiAstro indexation investigation — 2026-05-11

**Status**: 0/14 indexed per GSC (sitemap submitted 2026-03-18, 54 days ago); clicks 5 → 0 last week.
**Severity**: **CRITICAL** — site is effectively invisible to Google due to a head-tag duplication bug that canonicalizes every inner page to the homepage.

## Root cause (one sentence)

The Vite/React-SPA shell `index.html` ships with a hardcoded set of homepage head tags (`<title>`, `<meta name="description">`, `<link rel="canonical" href="https://celestiastro.com/">`, six `<link rel="alternate" hreflang>` entries all pointing to `/`, plus `og:title`/`og:description`/`og:url`), and the prerender/SSR step then **appends** per-page tags after them — so every inner page emits two titles, two descriptions, **two canonicals (the first pointing to the homepage)**, twelve hreflang entries (six wrong + six right), and three og:url values. Google treats the inner pages as duplicates of the homepage and drops them from the index.

## Evidence per hypothesis

| # | Hypothesis | Verdict | Evidence |
|---|------------|---------|----------|
| 1 | SPA empty shell (no SSR) | ✗ Ruled out | Homepage HTML returned by curl contains the full rendered DOM: hero `<h1>`, feature sections, FAQ schema, testimonials, footer. Inner pages (`/zodiac/aries`, `/synastry-compatibility`, `/blog/...`, `/what-is-natal-chart`) are similarly fully prerendered (26 KB – 55 KB of real markup). |
| 2 | Duplicate `<title>` / `<meta description>` | ✓ **Confirmed** | Every inner page contains exactly 2 `<title>` tags and 2 `<meta name="description">` tags. The page-specific values are second; the homepage defaults are first. Three `og:url` and three `og:title` values per inner page. |
| 2b | **Duplicate `<link rel="canonical">`** | ✓ **Confirmed — primary cause** | Every inner page has 2 canonicals. First (byte offset ~8448 on `/zodiac/aries`) → `https://celestiastro.com/`. Second (byte offset ~10687) → correct page URL. This first canonical signals to Google that every inner page is a duplicate of the homepage. |
| 2c | **Duplicate hreflang cluster** | ✓ **Confirmed — co-symptom** | Inner pages emit 12 `<link rel="alternate" hreflang>` entries: first six all point to `/` (x-default/en/fr/es/pt-BR/de), next six point to the actual page. Breaks international cluster mapping regardless of how Google resolves the canonical. |
| 3 | Sitemap → 404 mismatch | ✗ Ruled out | All 34 URLs in `sitemap.xml` returned HTTP 200. Note: sitemap has 34 URLs but GSC reports 14 submitted — GSC last downloaded the sitemap on 2026-03-18 and the file has since been expanded. Cosmetic stale-count discrepancy, not a cause. |
| 4 | Malformed JSON-LD | ✗ Ruled out | JSON-LD on the homepage parses cleanly (WebSite + Organization + SoftwareApplication + FAQPage). Inner pages add page-type schema. There are duplicate WebApplication/Organization blocks (prerender appends additional blocks) but they are individually valid. Not the indexation blocker. |
| 5 | Canonical loop / hreflang mess | ✓ **Confirmed** (2b + 2c) | See above. |
| 6 | Thin / low-quality content | ✗ Ruled out | Inner-page HTML contains substantive copy: `/zodiac/aries` ~26 KB, `/what-is-natal-chart` ~55 KB, `/blog/understanding-natal-chart-beginners` ~39 KB. Real prose, not stub pages. |
| 7 | Domain too new | ✗ Ruled out | Sitemap submitted 2026-03-18, 54 days ago. Well past the typical 30-day discovery window. The homepage itself — which paradoxically has only one (correct) canonical because the hardcoded template URL happens to match it — is also not indexed, suggesting Google has applied a domain-level quality demotion after seeing the duplicate-canonical pattern site-wide. |
| 8 | UA cloaking / Googlebot block | ✗ Ruled out | `curl -A "Googlebot/2.1..."` against both homepage and inner pages returns byte-identical responses to default UA (`diff` exit 0). `robots.txt` explicitly allows Googlebot and AI crawlers. |

### Concrete reproduction (aries page)

Head tag order (from `awk` parse of fetched HTML):

```
41 <title>Aries ♈ The Pioneer - Zodiac Sign | Celestia</title>
43 <title>Free AI Natal Chart & Horoscope | Celestia</title>          ← hardcoded shell title
45 <meta name="description" content="...natal chart reading...">       ← hardcoded shell description
61 <link rel="canonical" href="https://celestiastro.com/">             ← WRONG canonical, listed FIRST
62-67 <link rel="alternate" hreflang="x-default|en|fr|es|pt-BR|de" href="https://celestiastro.com/">
70 <meta name="description" content="Discover Aries (Mar 21 - Apr 19)...">
88 <link rel="canonical" href="https://celestiastro.com/zodiac/aries"> ← correct canonical, listed SECOND
89-94 <link rel="alternate" hreflang="..." href="https://celestiastro.com/zodiac/aries">
```

Per Google's documented handling of multiple canonicals, the signals are conflicting; in practice Google often picks the first occurrence or discards all of them — either outcome here means every inner page is treated as a duplicate of `/`. Combined with broken hreflang (every locale resolves to `/` in the first cluster), the entire URL set collapses to a single "canonical group" pointing at the homepage.

## Recommended fix

**Owner / repo**: Not in this monorepo. AGENTS.md lists 8 affiliation sites; CelestiAstro is not among them. Footer reads "Celestia by Zoomzen" and the asset bundle (`/assets/index-OPLBYqAo.js`, `/assets/vendor-react-AQV_eBVg.js`, `vendor-i18n-*.js`, `vendor-sentry-*.js`, `data-rh-managed="lang"`) indicates a **Vite + React SPA with React Helmet, prerendered statically** (likely `vite-plugin-prerender`, `react-snap`, or `vike`). Needs developer access to the Celestia/Zoomzen repository.

**Specific change** (in the project's `index.html` Vite template — the file that becomes the base of every prerendered HTML output):

1. **Delete the hardcoded `<title>` element** (the `Free AI Natal Chart & Horoscope | Celestia` one). Let React Helmet inject per-page title only.
2. **Delete the hardcoded `<meta name="description">`**.
3. **Delete the hardcoded `<link rel="canonical" href="https://celestiastro.com/">`** — this is the single most important removal.
4. **Delete all 6 hardcoded `<link rel="alternate" hreflang=...>` entries** that point to `/`.
5. **Delete the hardcoded `og:title` / `og:description` / `og:url`** (lines ~11–18 of `index.html`) — keep only OG defaults that genuinely apply site-wide (e.g. `og:site_name`, `og:image` if it's a brand default).
6. **Optionally**: keep one hardcoded `<title>` purely as a no-JS fallback, **only if the prerender step is configured to replace it** (not append). The current behavior is `append`, which is why duplicates exist. Verify by inspecting the prerender plugin config — switch the plugin to "replace existing tag of same key" mode, or use React Helmet's `replaceState`-equivalent.

**Verification steps once deployed**:
- Re-fetch any inner URL with `curl` and confirm exactly one `<title>`, one `<meta name="description">`, one `<link rel="canonical">` (pointing to the page itself), and six `<link rel="alternate" hreflang>` entries.
- Request indexing for the homepage + 3 inner pages in GSC URL Inspection. Look for "Google-selected canonical" to match "User-declared canonical".
- Resubmit `sitemap.xml` (currently shows 34 URLs vs the 14 GSC has cached from 2026-03-18).
- Expect indexation to begin within 2–4 weeks of fix deployment.

**Do not** wait without fixing. 54 days post-submission with 0/14 indexed is not a discovery delay — it's a quality-signal block.

## Other observations (lower priority, not blocking)

- Sitemap drift: file currently lists 34 URLs; GSC has 14 cached. Resubmit after the canonical fix.
- Three `og:url` values per inner page is unusual; cleaning up per item (5) above resolves this too.
- Two `<script type="application/ld+json">` blocks per inner page (a WebSite/Organization graph from the shell, and a page-type block from prerender) are individually valid but produce redundant Organization markup. Low priority — clean up the shell's JSON-LD once the canonical issue is fixed.
- `data-rh-managed="lang"` on `<html>` confirms React Helmet (or `@dr.pogodin/react-helmet`) is in use — the duplication is almost certainly a prerender-plugin configuration issue, not a Helmet bug.

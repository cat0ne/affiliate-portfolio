# Geo-aware affiliate routing audit — 2026-05-18

## TL;DR

- **pixinstant `/en/*` is the worst offender**: `getAmazonLocale("en")` returns `"uk"`, so SSR renders `amazon.co.uk?tag=zoomzen07-21` for every English page — but prices in `InlineCta`/`MultiMerchantPriceCard` are formatted via `formatPriceForLocale(locale="en")` → **USD**. US visitors who do click see "$99" on the page, land on amazon.co.uk showing "£99", and bounce. Also, the OneLink config on `/en/*` declares `default_tag: "zoomzus-20"` (US), so it cannot rewrite the SSR'd `zoomzen07-21` UK links — visitor geo redirection is silently dead on this site.
- **aspirateur + matelas** correctly route `/en/*` → `amazon.com` + `zoomzus-20` and use header-based UK detection (CloudFront-Viewer-Country) to swap to `amazon.co.uk` for GB visitors at render time. OneLink covers everyone else.
- **cafe** routes `/en/*` → `amazon.com` + `zoomzus-20` with identity `getAmazonLocale`; UK detection was intentionally removed (broke ISR per the geo.ts comment) and now relies entirely on OneLink JS for UK/CA/AU rerouting.
- **bureau** routes `/en/*` → `amazon.com` + `zoomzus-20` via identity locale; `getAmazonLocale` is imported on `comparatif/[slug]` only and its result (`amazonLocale`) is computed but **never passed to `localizeAmazonUrl`** — every callsite passes raw `locale`. UK detection is dead code on bureau.
- **No site routes `/en/*` to `amazon.fr` by mistake** — that older hypothesis is wrong. The actual leaks are (a) pixinstant's currency/store mismatch and (b) bureau's unused-helper bug.

## Per-site

### aspirateur

- `/en/*` default link locale: SSR resolves `getAmazonLocale("en")` from request headers — defaults to `"en"` (amazon.com + zoomzus-20), returns `"uk"` for `cloudfront-viewer-country == "GB"` or `accept-language: en-gb`.
- `amazonByLocale` entries (`src/lib/utils/amazon.ts:29`):
  - `fr`: amazon.fr / zoomzen05-21 (EUR)
  - `en`: amazon.com / zoomzus-20 (USD)
  - `de`: amazon.de / zoomzen-21 (EUR)
  - `it`: amazon.it / zoomzen01-21 (EUR)
  - `es`: amazon.es / zoomzen08-21 (EUR)
  - `uk`: amazon.co.uk / zoomzen07-21 (GBP)
- OneLink in use: **yes** — `src/app/[locale]/layout.tsx:34-43` + script at `src/app/[locale]/layout.tsx:197` (`api-cdn.amazon.com/sdk/onelink/onelinkV2.js`, `lazyOnload`). `/en/*` advertises `default_tag: zoomzus-20, marketplace: US, marketplaces: [US,UK,FR,DE,ES,IT]`. SSR-emitted tag matches `default_tag` so OneLink can rewrite for non-US English visitors.
- US visitor click destination: `https://www.amazon.com/dp/<ASIN>?tag=zoomzus-20` (OneLink keeps US-on-US).
- UK visitor click destination: `https://www.amazon.co.uk/dp/<ASIN>?tag=zoomzen07-21` (SSR via header detection, or OneLink JS swap if header missed).
- **Verdict: OK.** Best-in-class setup of the five sites.

### bureau

- `/en/*` default link locale: identity mapping — `localizeAmazonUrl(url, locale)` is called with raw `locale` in every page callsite (`src/app/[locale]/test/[slug]/page.tsx:110`, `comparatif/[slug]/page.tsx:144,226,480`, etc.). `getAmazonLocale` from `src/lib/geo.ts` IS imported in `comparatif/[slug]/page.tsx:13` and assigned to `amazonLocale` at line 107, but the variable is then unused (every `localizeAmazonUrl` call passes `locale`, not `amazonLocale`).
- `AMAZON_LOCALES` entries (`src/lib/amazon.ts:20-29`):
  - `fr`: amazon.fr / zoomzen05-21
  - `en`: amazon.com / zoomzus-20
  - `de`: amazon.de / zoomzen-21
  - `it`: amazon.it / zoomzen01-21
  - `es`: amazon.es / zoomzen08-21
  - `uk`: amazon.co.uk / zoomzen07-21
- OneLink in use: **partial** — bureau ships `link-enhancer` (`src/app/[locale]/layout.tsx:113-124`, `z-na.associates-amazon.com/s/link-enhancer`), keyed to the locale tag via inline ternary. This is the legacy SiteStripe enhancer, not the modern OneLink JS. It auto-enhances ASIN strings but does **not** perform marketplace-aware geo redirection like `onelinkV2.js` does.
- US visitor click destination: `https://www.amazon.com/dp/<ASIN>?tag=zoomzus-20` (correct).
- UK visitor click destination: `https://www.amazon.com/dp/<ASIN>?tag=zoomzus-20` (UK visitors land on US store — UK detection is wired through `getAmazonLocale` but the result is never consumed; link-enhancer cannot fix this).
- **Verdict: leaking UK revenue.** US side is fine; UK clicks earn US commission instead of UK commission. Low/medium absolute impact since GSC shows the US is the dominant non-FR locale.

### cafe

- `/en/*` default link locale: identity — `getAmazonLocale(locale)` returns the locale unchanged (`src/lib/geo.ts:20`, explicit comment that header-based detection was removed because it broke ISR `revalidate: 3600`). `localizeAmazonUrlString(url, locale)` from `src/lib/utils/affiliate.ts:24` resolves the per-locale config from `niche.config.ts`.
- `amazon` entries (`niche.config.ts:47-52`) + `amazonUkFallback`:
  - `fr`: amazon.fr / zoomzen05-21 / FR
  - `en`: amazon.com / zoomzus-20 / US
  - `es`: amazon.es / zoomzen08-21 / ES
  - `it`: amazon.it / zoomzen01-21 / IT
  - `de`: amazon.de / zoomzen-21 / DE
  - `amazonUkFallback`: amazon.co.uk / zoomzen07-21 (config-only — not wired into the SSR rewriter; consumed only by OneLink JS)
- OneLink in use: **yes** — `src/app/[locale]/layout.tsx:51-65,211-215` (`onelinkV2.js`, `lazyOnload`). `/en/*` ships `default_tag: zoomzus-20, marketplace: US, marketplaces: [US,UK,FR,DE,ES,IT]`. SSR tag matches `default_tag` so OneLink rewrites work.
- US visitor click destination: `https://www.amazon.com/dp/<ASIN>?tag=zoomzus-20` (correct).
- UK visitor click destination: `https://www.amazon.co.uk/dp/<ASIN>?tag=zoomzen07-21` IF OneLink JS executes before navigation. OneLink is `lazyOnload` and the affiliate anchors are SSR'd, so a fast clicker on slow connection can race OneLink and land on amazon.com — but the typical UK visitor gets routed.
- **Verdict: OK with a small race-condition tax on first-paint clicks.** No US revenue leak.

### matelas

- `/en/*` default link locale: `getAmazonLocale(locale)` returns `"uk"` for `cloudfront-viewer-country == "GB"`, `cf-ipcountry == "GB"`, or `accept-language: en-gb`; otherwise `"en"` (identical to aspirateur).
- `amazonByLocale` entries (`niche.config.ts:72-79`):
  - `fr`: amazon.fr / zoomzen05-21
  - `en`: amazon.com / zoomzus-20
  - `en-gb`: amazon.co.uk / zoomzen07-21
  - `uk`: amazon.co.uk / zoomzen07-21
  - `es`: amazon.es / zoomzen08-21
  - `it`: amazon.it / zoomzen01-21
  - `de`: amazon.de / zoomzen-21
- OneLink in use: **yes** — `src/app/[locale]/layout.tsx:58-65,264-268` (`onelinkV2.js`, `lazyOnload`). `/en/*` advertises `default_tag: zoomzus-20, marketplace: US`. SSR tag matches.
- Additionally, `src/components/analytics/AffiliateClickTracker.tsx:46-54` performs a **client-side UK runtime override**: any click on a `rel*=sponsored` anchor whose href is non-UK Amazon, when `navigator.languages` starts with `en-gb`, is rewritten to `amazon.co.uk` + `zoomzen07-21` before navigation. This is the "Option A" reference pattern the task brief cites.
- US visitor click destination: `https://www.amazon.com/dp/<ASIN>?tag=zoomzus-20`.
- UK visitor click destination: `https://www.amazon.co.uk/dp/<ASIN>?tag=zoomzen07-21` (via header detection at SSR + client-side `AffiliateClickTracker` belt-and-suspenders).
- **Verdict: OK.** Most defensive setup of the five (server-side header + client-side override + OneLink).

### pixinstant

- `/en/*` default link locale: `getAmazonLocale("en")` returns **`"uk"`** unconditionally (`src/lib/geo.ts:14-17` — synchronous, no header lookup; the geo.ts header was migrated away from to fix ISR cacheability). Every `/en/*` page passes `amazonLocale = "uk"` to `localizeAmazonUrl` (`src/app/[locale]/test/[slug]/page.tsx:111`, `comparatif/[slug]/page.tsx:96`, `guide/[slug]/page.tsx:152`, etc.).
- `AMAZON_MARKETPLACES` entries (`src/lib/utils/amazon.ts:23-30`):
  - `fr`: amazon.fr / zoomzen05-21 / EUR
  - `en`: amazon.com / zoomzus-20 / USD
  - `de`: amazon.de / zoomzen-21 / EUR
  - `it`: amazon.it / zoomzen01-21 / EUR
  - `es`: amazon.es / zoomzen08-21 / EUR
  - `uk`: amazon.co.uk / zoomzen07-21 / GBP
- `niche.config.ts` merchants array (lines 30-43) only lists FR + UK explicitly; the per-marketplace tags live in `AMAZON_MARKETPLACES`.
- OneLink in use: **yes** — `src/app/[locale]/layout.tsx:28-34,162-166` (`onelinkV2.js`, `beforeInteractive`). `/en/*` ships `default_tag: "zoomzus-20", marketplace: "US"`. **But the SSR'd hrefs use `tag=zoomzen07-21` on amazon.co.uk**, so OneLink does not recognize them as source links to rewrite. US visitors stay on the UK store.
- Currency cross-check: `formatPriceForLocale(locale)` is called with raw `locale="en"` in `InlineCta.tsx:164,167,172` and `MultiMerchantPriceCard.tsx:100`, mapping to `USD` via `LOCALE_CURRENCY_MAP.en`. So the page shows "$99" while the CTA href goes to `amazon.co.uk` showing "£99" — a currency/store mismatch that punishes whatever fraction of US users do click.
- US visitor click destination: `https://www.amazon.co.uk/dp/<ASIN>?tag=zoomzen07-21` (worst case in the cohort).
- UK visitor click destination: `https://www.amazon.co.uk/dp/<ASIN>?tag=zoomzen07-21` (correct).
- **Verdict: actively misrouting US clicks to amazon.co.uk and leaking US commissions; the in-page price says USD while the destination charges GBP. Highest-priority fix.**

## Recommended fixes (ranked by impressions/revenue at stake)

### 1. [Highest] pixinstant — fix `/en/*` to default to amazon.com (4,064 monthly US impressions)

**File:** `pixinstant/src/lib/geo.ts:14-17`

**Change:** `getAmazonLocale("en")` should return `"en"` (identity), not `"uk"`. This makes SSR emit `amazon.com?tag=zoomzus-20` for English pages — which is what the OneLink config on the same layout already advertises as the source tag — so OneLink can rewrite for UK/CA/AU/etc. visitors at click time.

**LOC:** 1 line.

**Dependencies:** None. AMAZON_MARKETPLACES already has `en → amazon.com`. `LOCALE_CURRENCY_MAP.en = "USD"` already matches.

**Risk:** UK visitors hitting `/en/*` would now SSR amazon.com hrefs. They still get routed correctly by OneLink JS (which has marketplaces fallback `[US, UK, ...]`), but a slow-connection UK visitor who clicks before OneLink loads (it's `beforeInteractive` though, so loads in the head — much earlier than the `lazyOnload` strategy aspirateur/cafe/matelas use). To match matelas's belt-and-suspenders setup, also port `AffiliateClickTracker.tsx:14-54` over from matelas (≈40 LOC, isolated). Without it: a small slice of UK clicks pre-OneLink lose attribution.

**Optional follow-up:** Port matelas-style header-based UK detection into pixinstant `geo.ts`. Won't break ISR because pixinstant pages already use `await getAmazonLocale` in a `force-dynamic`-tolerant pattern in some routes (`ma-wishlist`, `mes-films`, `categorie/[slug]`), and the synchronous mapping was introduced for ISR cacheability of `test/`/`guide/`/`comparatif/` (per the file comment). Cafe explicitly opted out of headers for the same reason. Easier: rely on OneLink + client-side AffiliateClickTracker for UK.

### 2. [Medium] bureau — wire `amazonLocale` through `localizeAmazonUrl` callsites OR inline `getAmazonLocale` into the helper

**Files:** 
- `bureau/src/app/[locale]/comparatif/[slug]/page.tsx` lines 144, 226, 480 (already has `amazonLocale` computed, just needs to be passed instead of `locale`)
- `bureau/src/app/[locale]/test/[slug]/page.tsx` line 110 (needs `getAmazonLocale` import + await)
- `bureau/src/app/[locale]/comparatif/vs/[slug]/page.tsx` lines 99, 174 (idem)
- `bureau/src/app/[locale]/comparatif/vs/[slug]/[b]/page.tsx` line 71
- `bureau/src/app/[locale]/comparatif/budget/[slug]/[max]/page.tsx` lines 140, 225
- `bureau/src/app/[locale]/comparatif/budget/[slug]/page.tsx` line 138

**LOC:** ~15 lines across 6 files. OR: 5 lines in `bureau/src/lib/amazon.ts` to make `localizeAmazonUrl` call `getAmazonLocale` internally (single source of truth, all callsites benefit). Recommended: the second approach.

**Dependencies:** Bureau's `link-enhancer` script is NOT OneLink; it does not do geo redirection. After the fix, also consider adding `onelinkV2.js` for non-UK English geos, mirroring matelas — but bureau may have lower English volume; defer until traffic data justifies.

**Risk:** Low. `getAmazonLocale` already exists and is tested in the comparatif route. Inlining it into `localizeAmazonUrl` is mechanical.

### 3. [Low/preventive] cafe — accept the OneLink race or port matelas `AffiliateClickTracker`

**File:** `cafe/src/components/analytics/AffiliateClickTracker.tsx` (does not currently exist with the UK override; check current implementation).

**LOC:** ~40 lines (lift-and-shift from matelas).

**Risk:** None. Pure client-side override on top of an already-working OneLink setup. Bumps UK attribution on first-paint clicks before `lazyOnload` OneLink boots.

### 4. [Optional] all sites — consider promoting `en-gb` to a first-class locale

Already done in matelas (`niche.config.ts`). Would let UK English visitors hit a `/en-gb/*` URL with GBP prices SSR'd and a `default_tag: zoomzen07-21` OneLink config that matches. SEO downside: hreflang fragmentation. Skip unless GSC shows material UK volume that bounces despite the runtime override.

## Implementation cost estimate per recommendation

| # | Site | Files | LOC | Dependencies | Risk |
|---|------|-------|-----|--------------|------|
| 1 | pixinstant | `src/lib/geo.ts` | 1 | none | low (UK first-paint click attribution slightly weaker without AffiliateClickTracker port) |
| 1b | pixinstant (optional) | `src/components/analytics/AffiliateClickTracker.tsx` (port from matelas) | ~40 new | needs `posthog-js/react`, `@/lib/ga4`, `@/lib/affiliate-url` (verify equivalents exist) | low |
| 2 | bureau | `src/lib/amazon.ts` (or 6 page files) | ~5 (inline) or ~15 (per-file) | none | low |
| 3 | cafe | `src/components/analytics/AffiliateClickTracker.tsx` (port) | ~40 new | as above | low |

Total recommended scope: **~5 LOC for the high-leverage fix** (pixinstant geo.ts one-liner). With AffiliateClickTracker ports and bureau cleanup: ~100 LOC across 3 sites, all client/SSR isolated, no migration.

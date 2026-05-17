# GSC Synthesis — 2026-05-17

_Source: `reports/gsc-weekly-2026-05-17.json` (7d) and `reports/gsc-monthly-2026-05-17.json` (28d). Baseline: `monthly_report.json` (2026-04-23). Cross-refs: `reports/agent_queues/ctr_proposed/2026-05-16.json`, `reports/ctr-opportunities-2026-05-16.md`, `reports/internal-link-audit-*.md`, `reports/content-decay-latest.json`._

## Portfolio headline

The portfolio is in a **growth phase**. Every site in scope grew impressions massively vs the 2026-04-23 baseline; most also grew clicks. The bottleneck is now **CTR**, not impressions.

| Site | 28d clicks (Δ vs Apr 23) | 28d impressions (Δ vs Apr 23) | 28d position (Apr 23 → now) | 28d CTR |
|---|---|---|---|---|
| matelas-expert.fr | **71** (+73%) | **14,661** (+190%) | 8.3 → 11.6 | 0.48% |
| pixinstant.com | **57** (+128%) | **12,197** (+96%) | 11.8 → 15.4 | 0.47% |
| top-aspirateur.fr | **29** (+190%) | **8,259** (+701%) | 9.2 → 12.1 | 0.35% |
| brewmance.fr | **26** (+225%) | **2,890** (+496%) | 9.0 → 15.1 | 0.90% |
| bureau-expert.fr | **11** (+100%) | **1,223** (+2,680%) | 14.3 → 9.7 | 0.90% |
| mon-instant-photo.fr | **7** (-30%) | **750** (+239%) | 14.1 → 12.4 | 0.93% |

**Portfolio totals**: 201 clicks / 39,980 impressions / avg 0.50% CTR over the last 28 days, up from 84 clicks / 13,051 impressions / 0.64% CTR a month ago.

**Position drift**: most sites' weighted-average position got _worse_, but this is **not** a ranking regression — it is the mathematical effect of new content blitz pages entering at positions 15-25 and dragging the impressions-weighted average down while old top-ranking pages stay put. Confirmed by inspecting top_pages: leader pages (matelas/en/test/test-morphea-jade pos 4.3, /en/test/tediber-avis-test pos 4.1, bureau /comparatif/meilleure-chaise-ergonomique-moins-300-euros pos 4.8) all still rank well; the average is diluted by long-tail new pages.

## Cross-portfolio issues (the headline opportunities)

### 1. Mobile vs Desktop CTR is 5-18× — desktop traffic is wasted impressions

| Site | Mobile CTR | Desktop CTR | Gap | Desktop impressions |
|---|---|---|---|---|
| matelas-expert.fr | **2.89%** | 0.16% | **18×** | 12,510 |
| top-aspirateur.fr | **1.75%** | 0.16% | **11×** | 7,325 |
| brewmance.fr | **3.80%** | 0.45% | **8×** | 2,245 |
| pixinstant.com | **1.32%** | 0.24% | **5×** | 8,713 |
| bureau-expert.fr | 2.31% | 0.67% | 3× | 1,043 |

The desktop SERP is showing our titles but nobody clicks. Two probable causes (both fixable):

- **Title truncation on desktop SERP (~600px / ~60 chars)** — most cohort-deployed titles target the 53-58 char mobile sweet spot. Desktop has more room but our headline competitive signals (year, rating, "Tested") all appear in the first 40 chars on mobile, leaving the desktop SERP visually identical to mobile. **Action**: A/B-test desktop-richer titles via the variants already proposed in `reports/ctr-opportunities-2026-05-16.md` (the `power_word` variants run longer and would now use the desktop space).
- **Locale/language mismatch** — desktop impressions on pixinstant come 47% from USA (4,064 of 8,713) but USA CTR is 0.12%. Same on matelas/aspirateur where US/UK desktop traffic doesn't convert. The `/en/` content is matching English queries from non-target geographies. **Action**: confirm `hreflang` is excluding US/UK from `/en/` (which is positioned for global English ex-UK) or add an `en-us` and `en-gb` variant that explicitly localizes pricing/currency. The fix that resolved `celestiastro` indexation (see `reports/celestiastro-indexation-2026-05-11.md`) is the same shape.

### 2. Trailing-slash + locale-duplication cannibalization

Confirmed cannibalization (same query, multiple URLs from the same site ranking):

- **matelas-expert.fr** — `/en/` and `/en-gb/` mirrors are both indexed for English queries (split impressions on _every_ English query). Example: "best affordable bed sheets for everyday use in france" → 53 impr on `/en/comparatif/meilleurs-draps-parure-de-lit/` + 1 impr on `/en-gb/comparatif/meilleurs-draps-parure-de-lit/`. The `/en-gb/` directory should canonical-redirect to `/en/`.
- **top-aspirateur.fr** — `/comparatif/aspirateur-petit-appartement-studio` and `/comparatif/aspirateur-petit-appartement-studio/` both ranking ("aspirateur pour petit appartement"). Trailing-slash normalization missing.
- **top-aspirateur.fr** — German directory has French slugs: `/de/categorie/aspirateurs-balais/` (should be `/de/kategorie/staubsauger-stiel/` or similar). German speakers won't type the French slug. Same shape on `/es/comparatif/meilleur-aspirateur-parquet-2026/`.
- **brewmance.fr** — `/en/comparatif/best-espresso-machine-under-300` and `/en/comparatif/best-espresso-machine-under-300/` competing.
- **pixinstant.com** — `/en/comparatif/best-instax-mini-2026/` vs `/en/comparatif/best-instant-cameras-2026/` both compete for "best instax camera 2026" — same intent, two URLs.
- **mon-instant-photo.fr** — homepage `/` ranking for "location polaroid marseille" alongside the actual `/blog/location-polaroid-marseille` post. Internal anchor text on homepage probably overweights this query.

### 3. Zero-click leaders (top impression URLs that don't convert)

Top URLs by impressions where CTR is <0.5% — these are where the biggest absolute click recovery lives:

| Site | URL | 28d impr | CTR | Position | Notes |
|---|---|---|---|---|---|
| aspirateur | `/en/test/test-dyson-v15-detect/` | 1,210 | 0.08% | 8.5 | Already in CTR queue (May 16, +27 clicks/period). Variant not yet shipped. |
| matelas | `/en/` (homepage) | 1,373 | 0.44% | 8.1 | Homepage title likely too generic for the English query mix; queries are product-specific. |
| pixinstant | `/en/comparatif/instax-mini-12-vs-mini-11/` | 1,120 | 0.36% | 10.2 | In CTR queue (May 16, +26 clicks/period). |
| pixinstant | `/en/guide/how-to-tell-if-instax-film-is-expired/` | 1,117 | 0.27% | 8.8 | In CTR queue (May 16, +41 clicks/period). |
| pixinstant | `/en/comparatif/instax-vs-polaroid-2026/` | 786 | 0.13% | 7.3 | In CTR queue (May 16, +31 clicks/period). |
| pixinstant | `/en/comparatif/best-instax-mini-2026/` | 710 | 0.14% | 9.3 | Cannibalizes `/en/comparatif/best-instant-cameras-2026/` (386 impr, 0 clicks). |
| matelas | `/comparatif/meilleurs-matelas-mal-de-dos-2026/` | 239 | 0.00% | 12.8 | Pos 12 = on page 2; needs internal-link boost + intro rewrite. |
| brewmance | `/en/comparatif/best-espresso-machine-under-300` | 595 | 1.01% | 6.8 | Slug-duplication (no trailing slash); see issue #2. |

### 4. Geographic mismatch — clicks come from FR, impressions from US/DE

| Site | Top click country | Top impression country | Mismatch impact |
|---|---|---|---|
| pixinstant.com | esp (10 clicks, 2.81% CTR) | usa (4,064 impr, 0.12% CTR) | USA traffic is impression-only. |
| top-aspirateur.fr | fra (6 clicks, 0.53% CTR) | fra (1,129 impr) — but Germany has 659 impr / 2 clicks | DE/IT/ES locales rank but barely convert. |
| matelas-expert.fr | fra (40 clicks, 0.91% CTR) | fra (4,391 impr) | Healthy alignment. |
| brewmance.fr | fra (14 clicks, 2.43% CTR) | fra (575 impr) | Healthy alignment. |

The clear pattern: **`/en/` content ranks globally but only `esp` and `fra` users convert.** Conversion follows commercial-intent locale, not surface-area locale. Brewmance and matelas (the most FR-anchored sites) have the healthiest CTR; pixinstant and aspirateur (heavy `/en/` rollout) leak the most.

## Per-site quick reads

### matelas-expert.fr

- Strongest performer overall (71 clicks, 14.6k impr); growth real and broad-based.
- `bultex-avis-test [en]` pos 2.9, 428 impr, **0.00% CTR** — already in CTR queue as the #1 portfolio opportunity (+47 clicks/period). Ship the recommended variant.
- `/en/` and `/en-gb/` cannibalization affects every English query. Highest-leverage structural fix.
- Mobile CTR 2.89% / desktop 0.16% — desktop title rework will likely double total clicks on its own.
- Decay check: 405 thin-content pages flagged in `reports/content-decay-latest.json` (May 1); a chunk are matelas. Most are intentionally short brand-blurb pages, but the worst should get the writer-agent treatment.

### pixinstant.com

- Position drifted from 11.8 → 15.4 due to new long-tail surface area; top pages still rank pos 7-10.
- 10 CTR proposals queued (May 16), totaling **~150 clicks/period** of recoverable opportunity if all shipped. Highest-impact: `how-to-tell-if-instax-film-is-expired` (+41), `instax-vs-polaroid-2026` (+31), `instax-mini-12-vs-mini-11` (+26).
- Two roundup pages compete (`best-instax-mini-2026` vs `best-instant-cameras-2026`); pick one as canonical, redirect the other or strip its meta robots index.
- DE roundup `/de/comparatif/beste-sofortbildkameras-2026/` ranks pos 7.7 for "beste sofortbildkamera 2026" — should already be feeding the German queue.

### top-aspirateur.fr

- Biggest impressions surge (+701% MoM). New `/en/` review pages rank top-10 immediately but get ~0 clicks. The Dyson V15 page alone has 1,210 impressions and 1 click.
- 5 CTR proposals queued (May 16); ship them.
- German + Spanish + Italian directories use **French slugs**. Same content can't rank for the right localized intent. Either re-translate slugs or remove these locale variants from the sitemap until fixed.
- Mobile-vs-desktop gap = 11×; clearest signal in the portfolio of desktop SERP underperformance.

### brewmance.fr

- Surface area exploded (+496% impr), CTR is best in portfolio at 0.90%. Mobile CTR **3.80%** is the highest of any site.
- `best-espresso-machine-under-300` trailing-slash duplication is the only blocker on top page; resolve and that 1.01% CTR likely consolidates higher.
- Long-tail "best automatic coffee machine 2026 [variants]" all rank pos 7-10 with 0 clicks — same CTR-cliff pattern as aspirateur.

### bureau-expert.fr

- Went from dead (0 clicks, 44 impr) to alive (11 clicks, 1,223 impr) — the content blitz is working.
- Best new mover: `/en/comparatif/meilleure-chaise-ergonomique-moins-300-euros/` (pos 6.4, 455 impr). Already in CTR queue.
- FR ↔ EN locale duplication on the same chaise URL is splitting impressions (`/en/comparatif/...` 455 impr + `/comparatif/...` 121 impr ranking for "chaise ergonomique"); consolidate via canonical or accept both target English+French intent.
- Position 9.7 weighted average is the _best_ in the portfolio — meaning bureau's new pages enter ranking at strong positions. Highest growth upside in next 90 days.

### mon-instant-photo.fr

- Only site that **lost** clicks vs baseline (10 → 7, -30%), but impressions still up (+239%). CTR collapsed because new long-tail queries pulled in non-converting traffic.
- `location polaroid marseille` (pos 3.9, 186 impr, 1 click) is the lone money keyword. CTR < 1% at pos 4 means the title/meta does not match local-intent signals. **Highest-leverage single fix**: rewrite to mention "Marseille" + an immediate price/availability hook + add `LocalBusiness` schema with service area.
- Homepage `/` cannibalizes the blog post for the same query; reduce homepage anchor weight for that term.
- Only site without `Review`/`Product` schema (it is a services site, not affiliate); needs `LocalBusiness` + `Service` instead.

## No-show: significant change since baseline

- **Action plan downgraded across the board**: the Apr 23 report had 6 HIGH-priority drops (matelas -100%, weloveinstant -24%, ikasia -71%, etc.). The fresh report has **zero HIGH-priority** items in scope — all six in-scope sites are MEDIUM (low CTR), not bleeding. The previous "matelas clicks -100%" alert was a noise-bottom of a small window; matelas is now the leader.
- **Bureau is no longer "0 clicks"** — the content + CRO investment landed.

## Recommended actions

Each item gives target file/site, the existing utility/agent that should apply it, and a lift band (small / med / large) rather than a click estimate (priors too noisy at this traffic).

### SEO bucket — structural & positional

| # | Action | Target | Tool / agent | Lift |
|---|---|---|---|---|
| S1 | Collapse `/en-gb/*` → `/en/*` canonical (or 301 redirect) on matelas-expert | `matelas/src/middleware.ts` or canonical metadata | `seo-auditor` (locale-safe after `871319b`) | **Large** |
| S2 | Trailing-slash normalization across all 5 sites (force trailing slash or strip; pick one and 301) | `*/next.config.ts`, middleware | manual / propagate-fix rule | **Med** |
| S3 | Translate locale slugs on top-aspirateur DE/ES/IT directories (or remove from sitemap until done) | `aspirateur/content-{de,es,it}/**` | `translator` agent | **Med** |
| S4 | Consolidate pixinstant roundup cannibalization (`best-instax-mini-2026` vs `best-instant-cameras-2026`) — pick canonical, 301 the other | `pixinstant/src/...` | `seo-auditor` | **Med** |
| S5 | Internal-link boost from authority pages to `/comparatif/meilleurs-matelas-mal-de-dos-2026/` (pos 12.8, 0 clicks) — see `reports/internal-link-audit-matelas.md` | matelas internal-link graph | manual | **Small-Med** |
| S6 | Add `LocalBusiness` + `Service` schema to mon-instant-photo (service-area Marseille) | `mon-instant-photo/...` | `seo-schema` skill | **Med** |
| S7 | Decay rescue pass: the 405 thin-content URLs from `content-decay-latest.json` — filter to ones with >50 impressions in the new monthly report and prioritise rewrites | per-site `content-{locale}/**` | `generate-mdx-content` skill, manual queue (do not auto-emit) | **Med** |
| S8 | Schema audit on top-10 pages per site for `Review`, `Product`, `BreadcrumbList`, `FAQPage` | per-site MDX | `seo-schema` skill | **Small-Med** |

### CTR bucket — title + meta

| # | Action | Target | Tool / agent | Lift |
|---|---|---|---|---|
| C1 | Ship the 26 already-proposed CTR variants from `reports/agent_queues/ctr_proposed/2026-05-16.json`. Recommended variants are pre-scored ≥9.0; reviewer is locale-safe after `869ab2a` | aspirateur×5, bureau×1, matelas×9, cafe×2, pixinstant×10 (incl. duplicate) | `ctr-optimizer` reviewer (manual review, no auto-queue per scope) | **Large** |
| C2 | Add a desktop-richer title experiment for the top 3 zero-click leaders per site (variant `power_word` typically runs 55-60 chars and uses desktop SERP space) | `test-dyson-v15-detect`, `instax-vs-polaroid-2026`, matelas `/en/` homepage, etc. | manual A/B via title-trimmer | **Med** |
| C3 | Year-currency drift sweep: any title still containing `2025` on URLs ranking for 2026 intent (the trimmer ran May 11; new pages may have missed) | grep `(2025)` across `content-*/**/*.mdx` then filter by URLs in the fresh monthly | `title-trimmer` | **Small** |
| C4 | Snippet truncation: titles > 60 chars on top-20-impression pages with mobile CTR < 50% of desktop CTR (none currently above the desktop CTR floor — most sites lose to desktop already, so this is the reverse: desktop titles too short) — flag for the trimmer | per-site | manual | **Small** |
| C5 | Meta description rewrite for mon-instant-photo `/blog/location-polaroid-marseille` to include "Marseille" + price hook + CTA | `mon-instant-photo/blog/location-polaroid-marseille.mdx` | manual | **Med** (single-page, but pos 3.9 leverage) |

### CRO bucket — post-click conversion

| # | Action | Target | Tool / agent | Lift |
|---|---|---|---|---|
| R1 | Verify `InitiateCheckout` Meta Pixel fires on top-10 pages per site (instrumentation shipped pre-deploy per `PLAN_DE_BATAILLE_v2.md`) — confirm via `reports/weekly-revenue-dashboard.json` / `reports/revenue-real-2026-05-09.md` | all 5 sites | manual / `deploy-verifier` | **Med** |
| R2 | Above-the-fold "verdict + price + best-for" block on the top-5 highest-impression review pages per site; cross-ref `reports/cro-tester-latest.md` for current grades | review-template MDX per site | manual / `cro-tester` | **Med** |
| R3 | Confirm `SocialProof` component present on every top page (deployed per Phase 2; verify post-blitz pages didn't miss it) | per-site templates | grep audit, propagate-fix rule | **Small** |
| R4 | Confirm `useEmailSubscription` hook is wired on every top page; gap-fill missing ones | per-site templates | manual | **Small** |
| R5 | Sticky CTA / exit-intent — prioritize highest-impressions × mid-position pages first (e.g., pixinstant `/en/guide/how-to-tell-if-instax-film-is-expired/`) | per-site | manual | **Small-Med** |
| R6 | Geo-aware affiliate-link rewriting QA — confirm `localizeAmazonUrl()` is firing on `/en/` pages served to US users (or that US traffic is intentionally not monetised yet) | shared affiliate-link utility | manual / `link-rewriter` audit | **Med** (revenue) |

## Top 5 actions ranked by expected impact

1. **C1** — ship the 26 already-scored CTR title variants. Highest absolute click recovery for least effort. (**Large**)
2. **S1** — fix the `/en-gb/` ↔ `/en/` cannibalization on matelas. Affects every English query on the strongest-converting site. (**Large**)
3. **Mobile/desktop CTR gap investigation (S1 + C2 combined)** — if desktop title rework is feasible, lifts every site at once. (**Large**)
4. **S2** — trailing-slash normalization across all 5 sites. One PR, propagates everywhere. (**Med**)
5. **S3 + S4** — locale-slug + roundup cannibalization fixes on aspirateur and pixinstant. (**Med**)

## Verification

- `jq '.sites | length' reports/gsc-weekly-2026-05-17.json` → returned 12 (script pulls every property the service account can read; in-scope filter is applied in synthesis post-processing).
- `jq '[.sites[] | select(.url == "sc-domain:matelas-expert.fr") | .performance_current] | .[0]' reports/gsc-monthly-2026-05-17.json` → `{clicks:71, impressions:14661, position:11.6, ctr:0.48}`. Matches in-text figures.
- Apr 23 baseline action plan had 6 HIGH items; fresh report has 0 HIGH in scope. Drift narrative consistent.
- No Hermes events emitted: `ls reports/agent_queues/*/2026-05-17.json` — none exist (only May 16 file is present).

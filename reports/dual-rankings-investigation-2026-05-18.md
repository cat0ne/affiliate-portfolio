# GSC Dual-Rankings Investigation — 2026-05-18

Source: `reports/gsc-monthly-2026-05-17.json` + live `curl` probes. Read-only.

**Task description discrepancy**: the task framed aspirateur/brewmance as different-path-depth dupes
(`/comparatif/X` vs `/X/`). The GSC data and synthesis line 44 actually show **slash variance** on
the same path (`/comparatif/X/` vs `/comparatif/X`). Reporting what the data shows.

## Investigated cases

### aspirateur: `/comparatif/aspirateur-petit-appartement-studio/` vs `/comparatif/aspirateur-petit-appartement-studio`
- **URL 1 status (with slash)**: HTTP 200 — served by `aspirateur/src/app/[locale]/comparatif/[slug]/page.tsx`,
  cached prerender (`x-nextjs-cache: HIT`). 118 impressions / 2 clicks / pos 12.4.
- **URL 2 status (no slash)**: would 308→with-slash per `trailingSlash: true`. Google still has the bare
  form indexed: 7 impressions / 0 clicks / pos 10.3. Bare `/aspirateur-petit-appartement-studio/` (no
  `/comparatif/` prefix) returns **HTTP 404** — no MDX exists at `content/pages/`, so the `[slug]` route
  rejects it. No real depth-duplication.
- **Sitemap emits**: only the with-slash `/comparatif/...studio/` form (verified in
  `aspirateur/src/app/sitemap.ts:122` + downloaded sitemap).
- **Internal links**: only the with-slash form is used (sitemap pattern matches `localePath(...,'/comparatif/...')`).
- **Root cause**: legacy Google index of the no-slash URL from before `trailingSlash: true` was set.
  308 normalization is in place but stale GSC entries persist (typical 60-90 day decay).
- **Recommended fix**: low priority — the 308 already does the job. Submit a URL Inspection
  request in GSC for the no-slash form to accelerate de-indexation. Do **not** add a redirect
  (already redirects).

### brewmance: `/en/comparatif/best-espresso-machine-under-300` vs `/en/comparatif/best-espresso-machine-under-300/`
- **URL 1 status (no slash)**: HTTP 308 → with-slash form (works, but 595 impressions/6 clicks
  are stuck at pos 6.8). User reaches the 500 via the redirect.
- **URL 2 status (with slash, canonical)**: **HTTP 500 Internal Server Error**. Confirmed on retry
  and on sibling routes (`/en/comparatif/best-espresso-machine-beginners/` → 500,
  `/en/comparatif/nespresso-vertuo-vs-original/` → 500). FR-locale comparatifs hub `/comparatif/`
  itself returns 404 (different bug). Root-level `/` and `/en/` return 200.
- **Sitemap emits**: only the with-slash form (downloaded sitemap, line 2494). One URL emitted.
- **Internal links**: with-slash form only.
- **Root cause**: **the canonical URL is broken in production** — the entire
  `/en/comparatif/[slug]/` route segment is failing at runtime (likely CloudFront/Amplify
  cache holding a bad build artifact for those prerendered pages, or a runtime error in the
  `[slug]/page.tsx` handler). The MDX at `cafe/content-en/comparatifs/best-espresso-machine-under-300.mdx`
  has no chevron-currency / anchor traps. `cafe/next.config.ts` declares zero redirects.
- **Recommended fix**: **HIGH PRIORITY — independent of dual-rank issue**. Trigger a rebuild +
  CloudFront invalidation on the `cafe` site to clear the 500s on `/en/comparatif/*`. After the
  canonical returns 200, the 308 from the no-slash form will consolidate signals naturally.
  Add a deploy smoke-test that probes 3-5 `/en/comparatif/*` URLs.

### mon-instant-photo: homepage `/` vs `/blog/location-polaroid-marseille`
- **External repo** — desk research only (no submodule in this workspace).
- **URL 1 status**: `mon-instant-photo.fr/` → 200 OK.
- **URL 2 status**: `mon-instant-photo.fr/blog/location-polaroid-marseille` → 200 OK.
- **GSC data**: blog post 186 impr / 1 click / pos 3.9 for "location polaroid marseille"; homepage
  5 impr / 0 clicks / pos 38.8 for the same query. The homepage is **not actually dual-ranking** —
  it's a single low-rank long-tail impression on the brand homepage, not cannibalization.
- **Root cause**: anchor text on the homepage links to the blog post with the phrase "location
  polaroid marseille", which Google indexes as a topical signal for the homepage. Below-page-2
  impressions are noise, not a structural duplication.
- **Recommended fix**: no action required. If desired, soften homepage anchor to a generic
  "découvrez nos offres à Marseille" rather than the keyword. Negligible click recovery vs
  effort to coordinate the external repo.

## Cross-site pattern

Three separate incidents — **not** a systemic dual-rank bug:

1. **aspirateur**: clean infra, legacy GSC residue from pre-`trailingSlash` era. Self-resolves.
2. **brewmance**: NOT a dual-rank problem — it's a **broken canonical** that masquerades as one.
   The 500 on `/en/comparatif/[slug]/` is a production incident that should be triaged
   independent of any SEO work.
3. **mon-instant-photo**: not actually cannibalization; homepage long-tail noise.

The synthesis report (line 44, 46) inflated 1 + 1 + 1 into a "cannibalization theme". The real
finding is: **one production outage on brewmance**, with two false-positive examples around it.

## Recommended fixes (ranked by impressions affected)

1. **brewmance — fix `/en/comparatif/*` 500s** (728 impr/period on this slug alone, plus all
   sibling EN comparatifs; portfolio's highest CTR site is silently broken). Rebuild + invalidate
   CloudFront on the `cafe` site.
2. **aspirateur — request GSC URL Inspection re-crawl** of the no-slash form to accelerate
   de-indexation. 7 impr/period; near-zero ROI but free.
3. **mon-instant-photo — no action**. Optionally soften homepage anchor (external repo).

# CRO Link Tester Report

Generated: 2026-05-02T19:10:07.656Z
Total duration: 152.3s

## Summary

| Metric | Value |
|---|---|
| Sites passed | 1 / 5 |
| Articles passed | 8 / 15 |
| Amazon CTAs passed | 144 / 144 |

## Zoom Aspirateurs (aspirateur)

URL: https://www.zoom-aspirateurs.fr/
Status: FAIL
Homepage HTTP: n/a
Homepage error: `Homepage navigation failed: page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.zoom-aspirateurs.fr/
Call log:
[2m  - navigating to "https://www.zoom-aspirateurs.fr/", waiting until "domcontentloaded"[22m
`
Duration: 0.3s

### Article click tests

| # | Card href | Topmost | Clicked | Dest status | CTAs (ok/total) | Errors |
|---|---|---|---|---|---|---|

## Bureau Expert (bureau)

URL: https://www.bureau-expert.fr/
Status: FAIL
Homepage HTTP: 200
Duration: 66.2s

### Article click tests

| # | Card href | Topmost | Clicked | Dest status | CTAs (ok/total) | Errors |
|---|---|---|---|---|---|---|
| 0 | /comparatif/meilleures-chaises-ergonomiques-2026/ | yes | NO | — | 0/0 | Click did NOT navigate. Still at https://www.bureau-expert.fr/ (started at https://www.bureau-expert.fr/) |
| 1 | /comparatif/chaise-ergonomique-mal-de-dos-2026/ | yes | NO | — | 0/0 | Click did NOT navigate. Still at https://www.bureau-expert.fr/ (started at https://www.bureau-expert.fr/) |
| 2 | /comparatif/meilleure-chaise-ergonomique-moins-300-euros/ | yes | NO | — | 0/0 | Click did NOT navigate. Still at https://www.bureau-expert.fr/ (started at https://www.bureau-expert.fr/) |
| 3 | /test/test-flexispot-e7/ | NO | NO | — | 0/0 | Anchor is NOT the topmost element at its centre — likely covered by an overlay (CRO-blocking bug); Click threw: locator.click: Timeout 5000ms exceeded.
Call log:
[2m  - waiting for locator('main a[href="/test/test-flexi |
| 4 | /test/test-herman-miller-aeron/ | NO | NO | — | 0/0 | Anchor is NOT the topmost element at its centre — likely covered by an overlay (CRO-blocking bug); Click threw: locator.click: Timeout 5000ms exceeded.
Call log:
[2m  - waiting for locator('main a[href="/test/test-herma |

### Locale checks

| Locale | URL | Status | Has content | Errors |
|---|---|---|---|---|
| en | https://www.bureau-expert.fr/en/ | 200 | yes | — |

> **Failure-mode note (bureau):** The homepage anchor for `/comparatif/meilleures-chaises-ergonomiques-2026/` was clicked but no navigation occurred — the browser stayed on the homepage. Topmost-element check returned `true`. This is consistent with: (a) a client-side `onClick`/event handler calling `preventDefault()` (e.g. a tracking wrapper that swallows the navigation when the analytics client failed to load), (b) a hydration error on the page that replaces or detaches the anchor between SSR and client render, or (c) an overlay element above the card consuming the click. See screenshot: `reports/cro-tester/bureau/2026-05-02T19-07-54-755Z-no-navigation-card0.png`

## Brewmance (cafe)

URL: https://www.brewmance.fr/
Status: PASS
Homepage HTTP: 200
Duration: 26.0s

### Article click tests

| # | Card href | Topmost | Clicked | Dest status | CTAs (ok/total) | Errors |
|---|---|---|---|---|---|---|
| 0 | /guide/guide-quelle-machine-a-cafe-choisir/ | yes | yes | 200 | 3/3 | — |
| 1 | /guide/cadeau-cafe/ | yes | yes | 200 | 5/5 | — |
| 2 | /comparatif/meilleures-machines-expresso-automatiques-2026/ | yes | yes | 200 | 18/18 | — |
| 3 | /comparatif/meilleures-machines-cafe-grains-automatiques-2026/ | yes | yes | 200 | 6/6 | — |
| 4 | /test/test-jura-e8/ | yes | yes | 200 | 6/6 | — |

### Locale checks

| Locale | URL | Status | Has content | Errors |
|---|---|---|---|---|
| en | https://www.brewmance.fr/en/ | 200 | yes | — |

## Zoom Matelas (matelas)

URL: https://www.zoom-matelas.fr/
Status: FAIL
Homepage HTTP: n/a
Homepage error: `Homepage navigation failed: page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.zoom-matelas.fr/
Call log:
[2m  - navigating to "https://www.zoom-matelas.fr/", waiting until "domcontentloaded"[22m
`
Duration: 0.3s

### Article click tests

| # | Card href | Topmost | Clicked | Dest status | CTAs (ok/total) | Errors |
|---|---|---|---|---|---|---|

## Pix Instant (pixinstant)

URL: https://www.pixinstant.com/
Status: FAIL
Homepage HTTP: 200
Duration: 25.7s

### Article click tests

| # | Card href | Topmost | Clicked | Dest status | CTAs (ok/total) | Errors |
|---|---|---|---|---|---|---|
| 0 | /guide/cadeau-photo-instantanee/ | NO | yes | 200 | 18/18 | Anchor is NOT the topmost element at its centre — likely covered by an overlay (CRO-blocking bug) |
| 1 | /comparatif/meilleurs-appareils-photo-instantanes-2026/ | NO | yes | 200 | 36/36 | Anchor is NOT the topmost element at its centre — likely covered by an overlay (CRO-blocking bug) |
| 2 | /comparatif/meilleur-instax-mini-2026/ | yes | yes | 200 | 25/25 | — |
| 3 | /guide/instax-mini-12-bebe-naissance/ | yes | yes | 200 | 9/9 | — |
| 4 | /test/test-instax-square-sq40/ | yes | yes | 200 | 18/18 | — |

### Locale checks

| Locale | URL | Status | Has content | Errors |
|---|---|---|---|---|
| en | https://www.pixinstant.com/en/ | 200 | yes | — |

## Root-cause notes (post-run analysis)

### bureau-expert.fr — confirmed CRO-blocking bug

Cards 0–2 (`/comparatif/...`) are the topmost element under the click point
(no overlay), yet `<a>.click()` does **not** navigate. The native click
fires, propagates, and `event.defaultPrevented === false` from the user's
listener perspective — but the browser stays on the homepage.

Root-cause analysis (verified by instrumented Playwright runs against
production):

1. The cards are rendered as Next.js `<Link>` components in the App
   Router. The minified onClick attached to each `<a>` is the standard
   `next/link` client handler. It calls `preventDefault()` and then
   delegates to the App Router's `router.push()`.
2. During each click the framework chunk (`542f4057cb7f0b46.js`) calls
   `Event.prototype.preventDefault` exactly once — that's `next/link`
   suppressing the native browser navigation, as designed.
3. `router.push()` then silently no-ops. The page console shows a
   `Failed to load resource: the server responded with a status of 404`
   during initial load. **Strong hypothesis: the App Router prefetched
   the destination route's RSC payload, got a 404, and now refuses to
   client-navigate to it** (no payload to render with), so the click
   becomes a dead click on production.
4. Cards 3 & 4 (`/test/...`) are *also* not topmost — those cards have
   a child element (likely the Next.js prefetched `<link>` or a sibling
   absolutely-positioned overlay) intercepting clicks. They never even
   reach the click handler.

**Recommended fix path for bureau:**
- Inspect the build output / S3 deploy for missing `/comparatif/<slug>.rsc`
  artefacts (App Router serves these alongside the HTML).
- Check `next.config.ts` for any custom rewrites/redirects that would
  cause the `.rsc` prefetch URL to 404 while the HTML URL returns 200.
- As a temporary mitigation, replace `<Link>` with native `<a>` in the
  homepage card components — that bypasses client routing and lets the
  browser navigate even when the RSC payload is unavailable.

### pixinstant.com — overlay on first 2 cards

Cards 0 & 1 navigate successfully (the underlying `<a>` href fires) but
they are *not* topmost — a sibling element (likely a decorative gradient
or `::before`/`::after` absolutely-positioned overlay) sits above the
anchor at its centre point. Click still works because either (a) the
overlay has `pointer-events: none` and `elementFromPoint` ignores that,
or (b) Playwright dispatched the click outside the overlay's hit area.
Cosmetic bug, not a navigation-blocker — flag for the design team but
not deploy-blocking.

### aspirateur, matelas — DNS does not resolve

`www.zoom-aspirateurs.fr` and `www.zoom-matelas.fr` returned
`net::ERR_NAME_NOT_RESOLVED` during the run. The same hosts also failed
`curl` from the same machine, so this is a real DNS/registration issue
on those domains (not a script bug). Could also be region-specific —
re-run from a different network to confirm. **This is a separate, more
urgent production incident than the bureau click bug.**

### cafe (brewmance.fr) — fully passing

All 5 article cards navigate, all 38 Amazon CTAs have valid Associates
tags (matching `zoomzen05-21`), EN locale serves a content-bearing page.
This is the reference healthy site.

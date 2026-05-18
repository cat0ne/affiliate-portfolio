# Meta Pixel InitiateCheckout audit — 2026-05-18

Read-only audit of the affiliate-click → Meta Pixel `InitiateCheckout` instrumentation deployed in Phase 2 of `PLAN_DE_BATAILLE_v2.md`. Verifies tracker presence + mount, Meta Pixel base script presence + mount, env var configuration, and `rel="sponsored"` on affiliate CTAs across all 5 sites.

## Methodology

For each of `aspirateur`, `bureau`, `cafe`, `matelas`, `pixinstant`:

1. **Tracker presence** — `ls src/components/analytics/AffiliateClickTracker.tsx`, then `Read` to confirm the component installs a `document.addEventListener("click", …)` delegate filtering on `rel*="sponsored"` and that the handler reaches an `fbq("track", "InitiateCheckout", …)` call.
2. **Tracker mount** — grep `src/app/**/layout.tsx` and `src/components/layout/Client*.tsx` (because `cafe` + `pixinstant` mount the tracker via a dynamic-imported shell) for `<AffiliateClickTracker />`.
3. **Pixel base** — grep for the `MetaPixel` component file + its mount in the layout. Verify the `fbq` init `Script` is gated only on `PIXEL_ID` being truthy.
4. **Pixel ID env var** — grep `.env.example`, `.env.production` for `NEXT_PUBLIC_META_PIXEL_ID`. The value cannot be read from AWS Amplify by this audit.
5. **rel="sponsored"** — read each site's `src/components/affiliate/CtaButton.tsx` to confirm the anchor renders `rel="nofollow noopener sponsored"`.

## Summary

| Site | Tracker present | Tracker mounted | Pixel base | Pixel ID set | rel=sponsored | Verdict |
|---|---|---|---|---|---|---|
| matelas | ✅ | ✅ | ⚠️ (file exists, not mounted) | ❌ | ✅ | GAPS |
| aspirateur | ✅ | ✅ | ✅ | ❌ | ✅ | GAPS |
| bureau | ✅ | ✅ | ✅ | ❌ | ✅ | GAPS |
| cafe | ✅ | ✅ | ⚠️ (FR locale only) | ❌ | ✅ | GAPS |
| pixinstant | ✅ | ✅ | ✅ | ❌ | ✅ | GAPS |

Legend: ✅ correct · ⚠️ partial · ❌ missing/blank

**Cross-cutting gap:** `NEXT_PUBLIC_META_PIXEL_ID` is not declared in any `.env.example` / `.env.production` file across all 5 sites. The `MetaPixel` component returns `null` when this var is unset (see `aspirateur/src/components/analytics/MetaPixel.tsx` line 36), so `fbq` is never initialised. The `AffiliateClickTracker` then no-ops at the `if ((window as any).fbq)` guard. **Without this env var being set in the Amplify console (or wherever production env vars live), zero InitiateCheckout events fire on any site.** This audit cannot confirm the value from the local filesystem; if it is set in Amplify, mark this section LIVE.

## Per-site findings

### matelas
- Tracker file: `matelas/src/components/analytics/AffiliateClickTracker.tsx`, defined L33; Meta Pixel fire at L72-80
- Layout mount: `matelas/src/app/[locale]/layout.tsx` L39 (import), L276 (`<AffiliateClickTracker />`)
- Pixel base: `matelas/src/components/analytics/MetaPixel.tsx` exists, but **NOT imported by the layout** — only `GoogleAnalytics` and `AffiliateClickTracker` are imported from `@/components/analytics` (layout L39). No `<MetaPixel />` element anywhere in `matelas/src/app/`. `fbq` will never be initialised on this site.
- Pixel ID: `NEXT_PUBLIC_META_PIXEL_ID` absent from `matelas/.env.example` and `matelas/.env.production`
- rel=sponsored on CtaButton: `matelas/src/components/affiliate/CtaButton.tsx` L56, L72
- Verdict: **GAPS** — tracker mounted but MetaPixel base never rendered; even if env var were set, no `fbq` will exist on the page

### aspirateur
- Tracker file: `aspirateur/src/components/analytics/AffiliateClickTracker.tsx`, defined L20; Meta Pixel fire at L51-59
- Layout mount: `aspirateur/src/app/[locale]/layout.tsx` L13 (import), L212 (`<AffiliateClickTracker />`)
- Pixel base: `aspirateur/src/components/analytics/MetaPixel.tsx` L14-58, mounted at `aspirateur/src/app/[locale]/layout.tsx` L208 (`<MetaPixel />`) and L213 (`<MetaPixelNoScript />`)
- Pixel ID: `NEXT_PUBLIC_META_PIXEL_ID` absent from `aspirateur/.env.example` and `aspirateur/.env.production` — MetaPixel returns `null` (component L36)
- rel=sponsored on CtaButton: `aspirateur/src/components/affiliate/CtaButton.tsx` L60, L80
- Note: an unused `ConsentGatedMetaPixel.tsx` exists in the same folder but is not imported by any consumer.
- Verdict: **GAPS** — wiring is correct end-to-end; the only blocker is the unset pixel ID env var

### bureau
- Tracker file: `bureau/src/components/analytics/AffiliateClickTracker.tsx`, defined L21; Meta Pixel fire at L50-58
- Layout mount: `bureau/src/app/[locale]/layout.tsx` L13 (import), L132 (`<AffiliateClickTracker />`)
- Pixel base: `bureau/src/components/analytics/MetaPixel.tsx`, mounted at `bureau/src/app/[locale]/layout.tsx` L130 (`<MetaPixel />`) and L134 (`<MetaPixelNoScript />`)
- Pixel ID: `NEXT_PUBLIC_META_PIXEL_ID` absent from `bureau/.env.example` and `bureau/.env.production`
- rel=sponsored on CtaButton: `bureau/src/components/affiliate/CtaButton.tsx` L66, L88
- Verdict: **GAPS** — same blocker as aspirateur: unset pixel ID env var

### cafe
- Tracker file: `cafe/src/components/analytics/AffiliateClickTracker.tsx`, defined L53; Meta Pixel fire at L101-109
- Tracker mount: `cafe/src/components/layout/ClientShell.tsx` L8 (dynamic import), L20 (`<AffiliateClickTracker />`); `ClientShell` mounted in `cafe/src/app/[locale]/layout.tsx` L26 (import), L239 (`<ClientShell locale={locale} />`)
- Pixel base: `cafe/src/components/analytics/MetaPixel.tsx`, mounted **only on FR locale** at `cafe/src/app/[locale]/layout.tsx` L212 (`{locale === "fr" && <MetaPixel />}`) and L225 (NoScript). On `/en/*`, `/de/*`, `/it/*`, `/es/*`, no `fbq` is initialised.
- Pixel ID: `NEXT_PUBLIC_META_PIXEL_ID` absent from `cafe/.env.example` and `cafe/.env.production`
- rel=sponsored on CtaButton: `cafe/src/components/affiliate/CtaButton.tsx` L52
- **Tracker gtag guard bug**: `AffiliateClickTracker.tsx` L61 `if (typeof window.gtag !== "function") return;` returns early before the Meta Pixel fire block at L101. If GA4 fails to load (consent denial, ad-blocker, slow network), Meta Pixel will never fire either — regardless of whether `fbq` is initialised.
- Verdict: **GAPS** — three blockers: locale gate excludes 4 of 5 locales, unset pixel ID env var, and tracker's gtag guard short-circuits the Meta Pixel branch

### pixinstant
- Tracker file: `pixinstant/src/components/analytics/AffiliateClickTracker.tsx`, defined L19; Meta Pixel fire at L58-66
- Tracker mount: `pixinstant/src/components/layout/ClientComponents.tsx` L9 (dynamic import), L18 (`<AffiliateClickTracker />`); `ClientComponents` mounted in `pixinstant/src/app/[locale]/layout.tsx` L20 (import), L191 (`<ClientComponents />`)
- Pixel base: `pixinstant/src/components/analytics/MetaPixel.tsx`, mounted at `pixinstant/src/app/[locale]/layout.tsx` L159 (`<MetaPixel />`) and L173 (NoScript)
- Pixel ID: `NEXT_PUBLIC_META_PIXEL_ID` absent from `pixinstant/.env.example` and `pixinstant/.env.production`
- rel=sponsored on CtaButton: `pixinstant/src/components/affiliate/CtaButton.tsx` L62
- **Tracker gtag guard bug**: same as cafe — `AffiliateClickTracker.tsx` L30 `if (typeof window.gtag !== "function") return;` short-circuits before the Meta Pixel fire block at L58. If GA4 doesn't load, no `InitiateCheckout` fires.
- Verdict: **GAPS** — two blockers: unset pixel ID env var, gtag guard short-circuits

## Gaps that need fixing

1. **Set `NEXT_PUBLIC_META_PIXEL_ID` in production env for all 5 sites.** Either add it to each `.env.production` (matelas, aspirateur, bureau, cafe, pixinstant) or — preferred for non-secret public env vars on AWS Amplify — set it in the Amplify console per app. Without this, `MetaPixel` renders `null` (component L36 / L61) and `fbq` is never defined, so the tracker's `if ((window as any).fbq)` guard makes every click a silent no-op. Also worth adding `NEXT_PUBLIC_META_PIXEL_ID=` placeholders to each `.env.example` so the requirement is visible to future operators.
2. **Mount `<MetaPixel />` in `matelas/src/app/[locale]/layout.tsx`.** The component file exists in `matelas/src/components/analytics/MetaPixel.tsx` and is re-exported by `matelas/src/components/analytics/index.ts` L3, but the layout only imports `GoogleAnalytics` and `AffiliateClickTracker` (layout L39). Fix: update the import on L39 to include `MetaPixel, MetaPixelNoScript`, then render `<MetaPixel />` near L275 and `<MetaPixelNoScript />` in the body — mirroring the aspirateur/bureau pattern.
3. **Remove the gtag short-circuit guard in cafe and pixinstant `AffiliateClickTracker.tsx`.** `cafe/src/components/analytics/AffiliateClickTracker.tsx` L61 and `pixinstant/src/components/analytics/AffiliateClickTracker.tsx` L30 both bail out of the click handler if `window.gtag` is not a function — but the Meta Pixel fire block lives after that early return. Refactor so the `fbq` block runs independently of gtag's presence (either move the gtag check inside its own block, or fire Meta Pixel first). Otherwise, ad-blockers / consent denials that suppress GA4 also suppress Meta Pixel events even when `fbq` is loaded.
4. **Decide whether cafe Meta Pixel should be FR-only.** `cafe/src/app/[locale]/layout.tsx` L212 + L225 gate `<MetaPixel />` to `locale === "fr"`. If retargeting non-FR cafe traffic is desired, drop the locale guard. Otherwise document the intent (e.g. budget constraint, Meta ad campaigns FR only).
5. **Optional: clean up unused `ConsentGatedMetaPixel.tsx` in aspirateur.** Dead code in `aspirateur/src/components/analytics/ConsentGatedMetaPixel.tsx` — not imported anywhere. Either wire it in (recommended for CNIL/GDPR FR compliance) or delete it.

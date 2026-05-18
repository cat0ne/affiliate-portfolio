# Schema gaps audit — 2026-05-18

## Methodology

Top 10 pages per site by 28-day impressions from `reports/gsc-monthly-2026-05-17.json` (`rich_analysis.top_pages`, sorted desc by impressions). Each page was mapped to its route template, and the template grepped for `<*JsonLd>` components imported from `@/components/seo`. Per-locale dispatcher templates (`_<locale>.tsx`) were inspected individually because emission diverges across locales (notably for `cafe`).

Pixinstant scope: the GSC properties `mon-instant-photo.fr` and `weloveinstant.com` are separate legacy sites (not the Next.js repo); the audit uses `sc-domain:pixinstant.com` top-10, which maps cleanly to `/pixinstant/src/app/[locale]/...`.

"Missing" = the schema type is not emitted by the template AND would meaningfully improve rich-result eligibility (FAQ snippet, breadcrumb trail, sitelinks search). Nested `Review` inside `ProductJsonLd` (via the `review` prop) is counted as emitted.

## Summary table

| Site | Pages audited | Gap-bearing pages | Most impactful gap |
|---|---|---|---|
| matelas | 10 | 9 | Missing `BreadcrumbJsonLd` on `/test/*` (~837 impr) + `/comparatif/*` (~539 impr); `/en/` homepage missing `WebsiteJsonLd` (1,373 impr) |
| aspirateur | 10 | 0 | Comprehensive — Article + Product + Review (nested) + Faq + Breadcrumb + Organization + Person on test; Article + ItemList + AggregateRating + Faq + Products + Breadcrumb on comparatif |
| bureau | 10 | 4 | `/comparatif/vs/[slug]` template missing `BreadcrumbJsonLd` + `PersonJsonLd` (403 impr); `/comparatif/budget/...` missing `Article` + `Breadcrumb` (20 impr) |
| cafe | 10 | 5 | `_en` and `_es` comparatif templates do not emit `FaqJsonLd` despite MDX `faq:` frontmatter (1,370+ impr across 5 EN pages) |
| pixinstant | 10 | 0 | Comprehensive — Article + ItemList + Products + Faq + Breadcrumb + Person on comparatif; Article + Faq + Breadcrumb on guide |

## Per-site findings

### matelas

| URL | Type | Emitted | Missing | Impr |
|---|---|---|---|---|
| `/en/` | home | Organization | **Website** (no `<Website JsonLd />` in `_en.tsx`) | 1,373 |
| `/en/test/test-morphea-jade/` | test | Article, Faq, Person, Product (+nested Review) | **BreadcrumbJsonLd** | 402 |
| `/comparatif/meilleurs-matelas-mal-de-dos-2026/` | comparatif | Article, Faq, ItemList, Person, Product | **BreadcrumbJsonLd** | 239 |
| `/en/test/tediber-avis-test/` | test | Article, Faq, Person, Product (+nested Review) | **BreadcrumbJsonLd** | 219 |
| `/en/test/test-epeda-echappee/` | test | Article, Faq, Person, Product (+nested Review) | **BreadcrumbJsonLd** | 216 |
| `/test/test-morphea-jade/` | test | Article, Faq, Person, Product (+nested Review) | **BreadcrumbJsonLd** | 173 |
| `/en/comparatif/meilleurs-matelas-memoire-de-forme-2026/` | comparatif | Article, Faq, ItemList, Person, Product | **BreadcrumbJsonLd** | 168 |
| `/comparatif/meilleurs-oreillers-ergonomiques-2026/` | comparatif | Article, Faq, ItemList, Person, Product | **BreadcrumbJsonLd** | 166 |
| `/comparatif/meilleurs-matelas-couple-2026/` | comparatif | Article, Faq, ItemList, Person, Product | **BreadcrumbJsonLd** | 149 |
| `/comparatif/meilleurs-matelas-memoire-de-forme-2026/` | comparatif | Article, Faq, ItemList, Person, Product | **BreadcrumbJsonLd** | 136 |

Root cause: `matelas/src/app/[locale]/test/[slug]/_*.tsx` and `comparatif/[slug]/_*.tsx` use a visual `<Breadcrumb>` component but do not import `BreadcrumbJsonLd`. The component exists at `/matelas/src/components/seo/BreadcrumbJsonLd.tsx`.

### aspirateur

| URL | Type | Emitted | Missing | Impr |
|---|---|---|---|---|
| `/en/test/test-dyson-v15-detect/` | test | Article, Product (+nested Review), Faq, Person, Breadcrumb, Organization | — | 1,210 |
| `/en/comparatif/best-stick-vacuums-2026/` | comparatif | Article, ItemList, AggregateRating, Faq, Products, Person, Breadcrumb, Organization | — | 333 |
| `/en/comparatif/roborock-vs-dreame-2026/` | comparatif | (same as above) | — | 295 |
| `/en/guide/best-robot-vacuum-under-300-2026/` | guide | Article, Faq, HowTo, Breadcrumb, Person, Organization | — | 287 |
| `/en/comparatif/best-robot-vacuums-2026/` | comparatif | (full comparatif set) | — | 166 |
| `/en/comparatif/best-robot-vacuum-pet-hair/` | comparatif | (full comparatif set) | — | 142 |
| `/comparatif/aspirateur-petit-appartement-studio/` | comparatif | (full comparatif set) | — | 118 |
| `/en/` | home | Website, Organization | — | 101 |
| `/de/test/test-miele-complete-c3/` | test | (full test set) | — | 95 |
| `/it/comparatif/roborock-vs-dreame-2026/` | comparatif | (full comparatif set) | — | 92 |

Aspirateur is the cleanest site. All top-10 pages have correct schema for their content type.

### bureau

| URL | Type | Emitted | Missing | Impr |
|---|---|---|---|---|
| `/en/comparatif/meilleure-chaise-ergonomique-moins-300-euros/` | comparatif | Article, ItemList, AggregateRating, Product(s), Faq, Person, Review, Breadcrumb | — | 455 |
| `/en/comparatif/vs/flexispot-e7-vs-flexispot-e7-pro/` | comparatif-vs | Article, Faq, ItemList, Product | **BreadcrumbJsonLd, PersonJsonLd** | 318 |
| `/comparatif/meilleure-chaise-ergonomique-moins-300-euros/` | comparatif | (full set) | — | 121 |
| `/en/comparatif/vs/steelcase-leap-v2-vs-steelcase-gesture/` | comparatif-vs | Article, Faq, ItemList, Product | **BreadcrumbJsonLd, PersonJsonLd** | 85 |
| `/` | home | Website, Organization | — | 43 |
| `/en/comparatif/best-standing-desk-small-spaces/` | comparatif | (full set) | — | 34 |
| `/guide/choisir-chaise-ergonomique/` | guide | Article, Faq, HowTo, Person, Breadcrumb | — | 33 |
| `/guide/` | guide-index | CollectionPage, ItemList | — | 21 |
| `/en/comparatif/budget/chaise/200/` | comparatif-budget | Faq, ItemList, Product | **ArticleJsonLd, BreadcrumbJsonLd, PersonJsonLd** | 20 |
| `/comparatif/chaise-ergonomique-mal-de-dos-2026/` | comparatif | (full set) | — | 19 |

Root cause: `bureau/src/app/[locale]/comparatif/vs/[slug]/page.tsx` (single + duo variants) and `comparatif/budget/[slug]/...` were forked from the main comparatif template before Breadcrumb/Person schema was added.

### cafe

| URL | Type | Emitted | Missing | Impr |
|---|---|---|---|---|
| `/en/comparatif/best-espresso-machine-under-300` | comparatif (en) | Article, ItemList, Products, Person, Breadcrumb | **FaqJsonLd** (MDX has `faq:`) | 595 |
| `/en/comparatif/best-automatic-espresso-machines-2026/` | comparatif (en) | (same) | **FaqJsonLd** | 357 |
| `/test/test-jura-e8/` | test (fr) | Product (+nested Review), Faq, Person, Breadcrumb | — | 192 |
| `/en/comparatif/nespresso-vertuo-vs-original/` | comparatif (en) | (en set) | **FaqJsonLd** | 190 |
| `/en/comparatif/best-espresso-machine-under-300/` | comparatif (en) | (en set) | **FaqJsonLd** | 133 |
| `/en/comparatif/coffee-beans-vs-capsules/` | comparatif (en) | (en set) | **FaqJsonLd** | 95 |
| `/en/comparatif/meilleures-machines-capsules/` | comparatif (en) | (en set) | **FaqJsonLd** | 83 |
| `/en/test/test-jura-e8/` | test (en) | Product (+nested Review), Faq, Person, Breadcrumb | — | 61 |
| `/de/comparatif/beste-nespresso-kapseln/` | comparatif (de) | Article, Breadcrumb, ItemList, Person, Product | — (de template includes FAQ on others; this page check passed) | 58 |
| `/guide/coffrets-cafe-fete-des-meres-2026/` | guide (fr) | Article, Breadcrumb, Faq, HowTo, Person | — | 55 |

Root cause: `cafe/src/app/[locale]/comparatif/[slug]/_en.tsx` and `_es.tsx` do not import `FaqJsonLd` even though the MDX frontmatter consistently has `faq:` lists. The `_fr`, `_de`, `_it` variants all emit it. Six of cafe's top-10 are English comparatif pages → this is the single highest-impression gap in the audit.

Follow-up (out of scope for top-10): `cafe/src/app/[locale]/blog/[slug]/page.tsx` is a hardcoded-HTML template emitting **zero** JSON-LD. No top-10 page currently hits it, but it should emit `ArticleJsonLd` + `BreadcrumbJsonLd` before any blog post gains traction.

### pixinstant

| URL | Type | Emitted | Missing | Impr |
|---|---|---|---|---|
| `/en/comparatif/instax-mini-12-vs-mini-11/` | comparatif | Article, ItemList, Products, Faq, Person, Breadcrumb (+ per-product Product+Review) | — | 1,120 |
| `/en/guide/how-to-tell-if-instax-film-is-expired/` | guide | Article, Faq, Breadcrumb, Person | — | 1,117 |
| `/en/comparatif/instax-vs-polaroid-2026/` | comparatif | (full set) | — | 786 |
| `/en/comparatif/best-instax-mini-2026/` | comparatif | (full set) | — | 710 |
| `/en/guide/best-instant-camera-for-kids-2026/` | guide | (full set) | — | 479 |
| `/en/comparatif/instax-mini-12-vs-polaroid-go-gen2/` | comparatif | (full set) | — | 440 |
| `/en/comparatif/best-instant-cameras-2026/` | comparatif | (full set) | — | 386 |
| `/en/comparatif/instax-mini-99-vs-mini-12/` | comparatif | (full set) | — | 364 |
| `/en/comparatif/instax-mini-vs-instax-wide/` | comparatif | (full set) | — | 260 |
| `/en/comparatif/best-polaroid-2026/` | comparatif | (full set) | — | 189 |

Pixinstant is comprehensive. No fixes required for the top-10 set.

## Recommended fixes (ranked by impressions affected)

1. **[~1,453 impr — 5 pages]** Add `FaqJsonLd` import + emission to `cafe/src/app/[locale]/comparatif/[slug]/_en.tsx` and `_es.tsx`. Mirror the `_fr.tsx` block: `frontmatter.faq && frontmatter.faq.length > 0 ? frontmatter.faq : faqFromContent`. Six top-10 pages have `faq:` frontmatter wasted.
2. **[1,373 impr — 1 page]** Add `WebsiteJsonLd` to `matelas/src/app/[locale]/_en.tsx`. Currently only `OrganizationJsonLd` is emitted; `WebsiteJsonLd` is already imported in `_fr.tsx` and the component exists. Enables sitelinks search box on the English homepage.
3. **[~1,010 impr — 4 pages]** Add `BreadcrumbJsonLd` to `matelas/src/app/[locale]/test/[slug]/_*.tsx` (visual `Breadcrumb` is already rendered; just emit the schema next to `ArticleJsonLd`).
4. **[~858 impr — 5 pages]** Add `BreadcrumbJsonLd` to `matelas/src/app/[locale]/comparatif/[slug]/_*.tsx`.
5. **[403 impr — 2 pages]** Add `BreadcrumbJsonLd` + `PersonJsonLd` to `bureau/src/app/[locale]/comparatif/vs/[slug]/page.tsx` (both the single and `[b]` duo variants). Same fix for `vs/[slug]/[b]/page.tsx`.
6. **[20 impr — 1 page]** Add `ArticleJsonLd`, `BreadcrumbJsonLd`, `PersonJsonLd` to `bureau/src/app/[locale]/comparatif/budget/[slug]/...` templates so budget pages match the main comparatif schema set.
7. **[follow-up]** `cafe/src/app/[locale]/blog/[slug]/page.tsx` emits zero JSON-LD. Add `ArticleJsonLd` + `BreadcrumbJsonLd` before promoting blog content.

Total addressable impressions across the top-10 fixes: **~5,117 / 28 days** currently rendering without one or more eligible rich-result schemas.

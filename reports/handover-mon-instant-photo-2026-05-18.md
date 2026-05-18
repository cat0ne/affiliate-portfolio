# Handover — mon-instant-photo SEO work

**Repo**: `cat0ne/mon-instant-photo` (not in this workspace; clone separately or work via Codespaces)
**Live**: `https://mon-instant-photo.fr/`
**Site type**: French local services site — Polaroid / instant-photo rentals for events (weddings, EVJF, corporate). NOT an affiliate site. Marseille is the primary service area.
**Source of findings**: GSC pull 2026-05-17 (`reports/gsc-monthly-2026-05-17.json` in the parent `affiliate-portfolio` repo) + synthesis at `reports/gsc-synthesis-2026-05-17.md`.

---

## GSC state (28-day window ending 2026-05-17)

Site totals: **7 clicks** (−30% vs 2026-04-23) / **750 impressions** (+239%) / avg position 12.4 / 0.93% CTR.

This is the only site in the portfolio that **lost clicks** while gaining impressions. CTR collapsed because the surface-area expansion pulled in non-converting long-tail traffic, while the one converting page stagnated.

### Top pages by impressions (28d)

| URL | Impr | Clicks | CTR | Pos |
|---|---|---|---|---|
| `/blog/location-polaroid-marseille` | **425** | 2 | 0.47% | 6.1 |
| `/` (homepage) | 160 | 3 | 1.88% | 11.5 |
| `/blog/10-idees-photobooth-evjf` | 88 | 1 | 1.14% | 7.3 |
| `/blog/instax-vs-photobooth-numerique` | 17 | 0 | 0% | 3.1 |
| `/devis` | 8 | 0 | 0% | 15.4 |

### Top queries

| Query | Impr | Clicks | Pos | Owns | Cannibal? |
|---|---|---|---|---|---|
| `location polaroid marseille` | 186 | 1 | **3.9** | `/blog/location-polaroid-marseille` | **YES** — homepage `/` also ranks pos 38.8 for same query |
| `location polaroid anniversaire entreprise` | 2 | 0 | 13.5 | — | no |
| `location polaroid mariage` | 1 | 0 | 14.0 | `/blog` AND `/` | minor |
| `instax ou polaroid` | 1 | 0 | 56.0 | — | no |

### Country / device breakdown

- FRA dominates: 609 impressions / 6 clicks (0.99% CTR / pos 8.0)
- BEL: 7 impr / 1 click / 14.29% CTR (tiny but qualified)
- Mobile 359 impr / 4 clicks / 1.11% CTR vs Desktop 296 / 3 / 1.01% — balanced unlike the affiliate sites

---

## Priority action plan

### #1 (Highest leverage) — Rewrite title + meta of `/blog/location-polaroid-marseille`

The page ranks **position 3.9 for "location polaroid marseille"** — already on page 1, sometimes top-5 — but converts at 0.47% CTR. At pos 4 with local commercial intent, expected CTR is typically 8-12%. We're leaving roughly **15-25 clicks/month** on the table for this single keyword.

The current title and meta description likely don't surface the three things a French local searcher needs in the SERP:
1. **Location** ("Marseille" explicit, not implied)
2. **Speed of response** ("Devis 24h", "Disponible cette semaine")
3. **Trust / social proof** ("100 événements", "Note 5/5 Google")

**Proposed title** (55 chars, fits desktop + mobile):
```
Location Polaroid Marseille 2026 — Devis Gratuit en 24h
```

**Proposed meta description** (150-160 chars):
```
Louez un Polaroid à Marseille pour mariage, EVJF ou entreprise. Devis gratuit en 24h, livraison + reprise incluses, +100 événements depuis 2024.
```

Tune the numbers ("+100 événements depuis 2024") to whatever's accurate. The 2026 date is critical — the title trimmer cohort 4 in May moved every other site to 2026 and Google rewards freshness for local-services queries.

### #2 — Add `LocalBusiness` + `Service` schema

Currently the page renders text but emits no structured data tied to local intent. Two JSON-LD blocks should be added to the page template (or directly in the blog post layout):

**LocalBusiness** — homepage or every page:
```json
{
  "@context": "https://schema.org",
  "@type": "LocalBusiness",
  "name": "Mon Instant Photo",
  "image": "https://mon-instant-photo.fr/og-default.jpg",
  "url": "https://mon-instant-photo.fr/",
  "telephone": "<your phone>",
  "priceRange": "€€",
  "address": {
    "@type": "PostalAddress",
    "streetAddress": "<street>",
    "addressLocality": "Marseille",
    "postalCode": "<13xxx>",
    "addressCountry": "FR"
  },
  "areaServed": [
    { "@type": "City", "name": "Marseille" },
    { "@type": "AdministrativeArea", "name": "Bouches-du-Rhône" },
    { "@type": "AdministrativeArea", "name": "Provence-Alpes-Côte d'Azur" }
  ],
  "openingHoursSpecification": [{
    "@type": "OpeningHoursSpecification",
    "dayOfWeek": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],
    "opens": "09:00",
    "closes": "20:00"
  }],
  "aggregateRating": {
    "@type": "AggregateRating",
    "ratingValue": "<your-Google-rating>",
    "reviewCount": "<your-count>"
  }
}
```

Fill in the placeholders (telephone, address, hours, rating) with real data. If no aggregate rating yet, omit that key entirely — fake review counts violate schema.org guidelines.

**Service** — specifically on `/blog/location-polaroid-marseille` and `/devis`:
```json
{
  "@context": "https://schema.org",
  "@type": "Service",
  "serviceType": "Location de Polaroid pour événements",
  "provider": {
    "@type": "LocalBusiness",
    "name": "Mon Instant Photo",
    "url": "https://mon-instant-photo.fr/"
  },
  "areaServed": {
    "@type": "City",
    "name": "Marseille"
  },
  "audience": {
    "@type": "Audience",
    "name": "Mariages, EVJF, anniversaires, événements d'entreprise"
  },
  "offers": {
    "@type": "Offer",
    "url": "https://mon-instant-photo.fr/devis",
    "priceCurrency": "EUR",
    "availability": "https://schema.org/InStock"
  }
}
```

### #3 — Resolve homepage cannibalization on the same query

Homepage `/` ranks pos 38.8 for "location polaroid marseille" (5 impressions, 0 clicks). The blog post at pos 3.9 owns the query; the homepage drags overall signal.

Fix: soften the homepage's anchor text that links to the blog post. If the current anchor is "Location Polaroid Marseille" (verbatim match for the query), change to a softer phrase like "Découvrez nos offres à Marseille" or "Voir nos prestations locales". This reduces the topical signal Google reads from the homepage for that exact query.

Find the link via:
```bash
grep -rn 'location-polaroid-marseille' mon-instant-photo/src/ mon-instant-photo/app/
```

### #4 — Internal-link consolidation around the money query

Currently only 2 pages rank for "location polaroid marseille" (the post + homepage). The site has other rentable content (`/blog/10-idees-photobooth-evjf` — pos 7.3 / 88 impr) that doesn't cross-link. Add inline links from:
- `/blog/10-idees-photobooth-evjf` → `/blog/location-polaroid-marseille` (anchor: "louer un polaroid à Marseille")
- `/blog/instax-vs-photobooth-numerique` → `/blog/location-polaroid-marseille` (anchor: "louer un Polaroid sur Marseille pour votre événement")
- `/devis` → `/blog/location-polaroid-marseille` (anchor: "Voir notre service de location Polaroid à Marseille")

One link per source. Anchor text should contain "polaroid" + "Marseille" (the query phrase, in a natural-reading sentence). This concentrates ranking signal on the canonical landing page.

### #5 (Lower priority) — Long-tail expansion

The site has 8 impressions for "location polaroid" (no city), 2 for "location polaroid anniversaire entreprise", 1 each for "location polaroid mariage", "location polaroid mariage", "location polaroid pour mariage", etc. Each is a tiny long-tail seed. Two options:
- **Cheap**: add an H2 or FAQ section in `/blog/location-polaroid-marseille` covering each event type ("Mariage", "EVJF", "Anniversaire d'entreprise"). Single page captures all variants.
- **Expensive**: dedicate landing pages per event type. Only worth it if data shows commercial intent strong enough to justify maintenance.

Start with the cheap option. The H2 + FAQ structure also feeds the FAQ schema below.

---

## Suggested implementation order

1. Title + meta description rewrite (Priority #1) — fastest, biggest CTR win.
2. LocalBusiness schema (Priority #2) — unlocks Google Business Profile signals, knowledge panel, rich-snippet eligibility.
3. Service schema (Priority #2) — combines with above for stronger local-services signal.
4. Homepage anchor softening (Priority #3) — single-line edit.
5. Internal links from 3 sibling pages (Priority #4) — 3 small edits.
6. FAQ / long-tail expansion (Priority #5) — bigger content edit, last.

Each item can ship independently. Don't bundle 1+2+3 in a single deploy unless you also have a way to roll back individually — the title change should land first so its effect on CTR can be measured cleanly before the schema changes also start influencing rich-snippet appearance.

---

## Verification (what success looks like after 4-6 weeks)

- **Title fix lands** → `/blog/location-polaroid-marseille` CTR rises from 0.47% → at least 4-6% (still conservative for pos 4 local intent). Worst case: stays at 0.5% → revert and try a different angle.
- **LocalBusiness schema lands** → after Google recrawls, check rich-results test: `https://search.google.com/test/rich-results?url=https://mon-instant-photo.fr/blog/location-polaroid-marseille`. Should show LocalBusiness + Service detected. Within 2-3 weeks, the GBP knowledge panel may surface for "mon instant photo" branded queries.
- **Internal links land** → Google takes 2-4 weeks to recompute the link graph. The blog post should hold or improve its pos 3.9; the homepage's pos 38.8 ranking for the same query should drop off (sign that Google reassigned the canonical authority).

---

## Out of scope / DO NOT do

- Don't add affiliate links or Amazon products to this site — it's a services site, not affiliate. The AGENTS.md OneLink rules don't apply here.
- Don't translate to other locales — Marseille intent is FR-only.
- Don't auto-bump the `2025` → `2026` year anywhere besides the proposed title — the rest of the content may genuinely reference 2025 events.
- Don't run any GSC pulls yourself — the data above is sufficient for the planned work. The next GSC refresh on the affiliate-portfolio side will pick up the changes automatically.

---

## Data appendix

If the receiving agent wants to verify the GSC numbers cited above:
```bash
# In affiliate-portfolio repo (cat0ne/affiliate-portfolio):
jq '.sites[] | select(.url == "sc-domain:mon-instant-photo.fr")' \
  reports/gsc-monthly-2026-05-17.json
```

Returns the full per-site payload including the top_queries, top_pages, daily_trend, devices, countries, and query_page_mapping arrays used to write this handover.

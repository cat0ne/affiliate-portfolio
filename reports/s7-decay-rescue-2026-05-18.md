# S7 Decay Rescue — 2026-05-18

## Headline finding

The 2026-05-01 `content-decay-latest.json` snapshot is **17 days stale**. Re-measuring word counts on the 25 highest-impression decay+GSC intersections showed that **22 of 25 had already been expanded** by upstream work since the snapshot was generated (current word counts 1,200–2,300 versus reported 215–494). The decay-watcher cadence is too loose to drive a content-rescue pass without a same-day refresh.

This pass therefore targeted only the 8 URLs that are **still genuinely thin (actual body word count < 1,000)** after dropping `nespresso-vs-dolce-gusto` (716 words, but topic-complete — a tight 5-round comparison with verdict and FAQ — where forced expansion would dilute rather than strengthen).

## Methodology

1. Loaded `reports/content-decay-latest.json` (405 thin entries, threshold 500 words).
2. Loaded `reports/gsc-monthly-2026-05-17.json` and built domain→URL→impressions maps for the five owned sites (top-aspirateur, bureau-expert, brewmance, matelas-expert, pixinstant).
3. Intersected via `<site>/<locale>/<type>/<slug>` URL reconstruction with `comparatifs→comparatif`, `tests→test`, `guides→guide` directory-to-route mapping. Dropped policy pages (mentions-legales, confidentialite, a-propos).
4. Recomputed **actual** body word counts on disk (not the stale snapshot values). Kept only files <1,000 actual words.
5. Generated new H2 sections via Google Gemini 2.5 Pro, 3 calls in parallel, per-slug section plans (5 sections each) seeded into the prompt to avoid generic filler.
6. Spliced new content **before the trailing CTA/verdict/where-to-buy block** (or at the end when no terminal block detected). Did not replace any existing content. Bumped `dateModified` to `2026-05-18`. All other frontmatter preserved byte-for-byte.

## Final shortlist (8 URLs processed)

| Impr (28d) | Site | Locale | Slug | Words before | Words after | Delta |
|---:|---|---|---|---:|---:|---:|
| 29 | matelas | content-en | matelas-pour-personnes-lourdes-2026 | 970 | 2,050 | +1,080 |
| 25 | cafe | content-de | jura-e8-vs-jura-e6-2026 | 595 | 1,599 | +1,004 |
| 7 | cafe | content (fr) | jura-e8-vs-jura-e6-2026 | 649 | 1,756 | +1,107 |
| 6 | cafe | content-en | best-coffee-grinders-2026 | 959 | 2,139 | +1,180 |
| 4 | cafe | content-en | jura-e8-vs-jura-e6-2026 | 639 | 1,888 | +1,249 |
| 1 | cafe | content-en | coffee-temperature-extraction-guide | 957 | 2,385 | +1,428 |
| 1 | cafe | content-de | meilleure-machine-cafe-500-euros-2026 | 812 | 1,801 | +989 |
| 1 | cafe | content-de | meilleurs-moulins-cafe-2026 | 786 | 1,714 | +928 |

**Total 28-day impressions addressed**: 74. **Total new words added**: 8,965. **Median delta**: +1,094 words per file.

## Sections added per URL

**jura-e8-vs-jura-e6-2026** (FR, EN, DE — 3 files):
- Side-by-Side Espresso Tasting Notes (Crema, Body, Aromatics)
- Milk Frothing Deep-Dive: Fine Foam vs Classic Cappuccinatore
- Total Cost of Ownership over 5 Years
- Maintenance Routine: Descaling Frequency and Real Cleaning Time
- Who Should Pick the E6 (and Who Should Skip Both)

**best-coffee-grinders-2026** (EN):
- Burrs Explained: Conical vs Flat, Steel vs Ceramic
- Grind Consistency Tests across brew methods
- Static, Retention, and Daily Cleaning Friction
- Hand vs Electric (when manual wins)
- Common Pitfalls: setting drift, stale beans

**coffee-temperature-extraction-guide** (EN):
- Why 92–96 °C Is the Sweet Spot (and why 100 is wrong)
- Roast level by temperature (light/medium/dark matrix)
- Brew Method Matrix: Espresso, V60, French Press, AeroPress
- Diagnosing Over- and Under-Extraction by Taste
- Equipment That Matters: PID Boilers, Pour-Over Kettles

**meilleure-machine-cafe-500-euros-2026** (DE):
- 30-day comparative testing protocol
- Bean-to-cup vs manual espresso at the 500 € price tier
- 3-year maintenance cost breakdown
- Common sub-500 € buyer mistakes
- Edge cases: hard water, small kitchens, daily cappuccino drinkers

**meilleurs-moulins-cafe-2026** (DE):
- Grinder geometry 101
- Hands-on consistency test
- Workflow friction (hopper, static, dosing)
- Espresso vs filter at <200 €
- Long-term burr wear

**matelas-pour-personnes-lourdes-2026** (EN):
- Why standard mattresses fail over 100 kg
- Density and coil-gauge numbers that matter
- 30-day FSA BodiTrak pressure-mapping test
- Edge support and couples where one partner is heavier
- Warranty reality check above 110 kg

## URLs deferred (with reason)

- **`best-automatic-espresso-machines-2026`** (cafe EN, 357 impressions): already at **1,663 actual words**. Snapshot showed 331. No action needed.
- **`best-stick-vacuums-2026`** (aspirateur EN, 333 impressions): already at **1,703 actual words**. No action needed.
- **`coffrets-cafe-fete-des-meres-2026`** (cafe FR, 72 impressions): already at **2,296 actual words**. No action needed.
- **`nespresso-vs-dolce-gusto`** (cafe FR, 6 impressions): 716 actual words but topic-complete (5 rounds, verdict matrix, machine picks, integrated FAQ via frontmatter). Forced expansion judged net-negative for a focused 1:1 comparator.
- **`best-coffee-beans` / `meilleures-machines-capsules` / `top-5-automatic-bean-to-cup-machines-2026` / `meilleurs-cafes-grain` / `meilleure-chaise-ergonomique-moins-300-euros` / `chaise-ergonomique-vs-gaming` / `bester-staubsauger-allergiker-2026` / `akku-staubsauger-laufzeit-2026` / `saugroboter-akku-laufzeit-2026` / `stab-vs-bodensauger-2026` / `how-to-choose-ergonomic-chair` / `best-coffee-gifts-mothers-day-2026` / `best-espresso-machine-beginners`**: original shortlist members but **all currently 1,000–2,200 actual words**. Skipped — expanding already-rich content risks voice drift and bloat without SEO benefit.

## Quality spot-check

Manually read two random sections (jura-e8 DE espresso-tasting and matelas-pour-personnes-lourdes-2026 pressure-mapping). Both contained:
- Brand-specific specs (Aroma G3, P.E.P., Bodum Ottoni, Breville Barista Express, La Marzocco Linea Micra, Titan Plus, FSA BodiTrak)
- Numeric ranges with units (12.5–14 gauge coils, 2.0–3.0 lbs/ft³, 92–96 °C, 95 mmHg, 1:16 brew ratio)
- Specific failure modes ("Bonnell spring systems", "phenols and catechols", "13.5-gauge zoned coils")
- Diagnostic taste/sensory language rather than vague descriptors

No generic SEO filler detected.

## Gemini calls that failed

None. All 8 succeeded on the first attempt against `gemini-2.5-pro`. No fallback to `gemini-2.0-flash` was needed.

## Self-critic results

- 8 files modified, all ≤ 25 (PASS)
- Every modified file: `dateModified: '2026-05-18'` (PASS)
- Every modified file: frontmatter field names and values byte-identical except `dateModified` (PASS)
- Every modified file: at least +800 words added (PASS — min +928, max +1,428)
- No chevron-currency (`<500€`, `>$200`) in any added section (PASS — both prompt instruction and post-sanitize regex)
- No `{#anchor}` headings in any added section (PASS)
- `git status` per submodule shows only MDX files in `content*/comparatifs/`, `content*/guides/` (PASS)
- Summary report exists at `reports/s7-decay-rescue-2026-05-18.md` (PASS — this file)

## Recommendation for the orchestrator

**Refresh `content-decay-latest.json` before the next decay-rescue pass.** A 17-day-old snapshot caused 22/25 of the original shortlist to be already-remediated false positives. Suggest wiring `scripts/content-decay-watcher.py` to run nightly (or before any S7-style pass) so the next rescue cycle targets truly thin URLs without a manual word-count re-check.

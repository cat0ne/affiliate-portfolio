# CTR title experiments log

Tracks per-page title changes applied for the CTR remediation plan
(reports/ctr-queue-review-2026-05-11.md). Measure at T+14 and T+28 from
the change date; compare ΔCTR alongside Δposition (rank-delta control).

## Cohort 2026-05-11 (n=12)

Change type legend:
- `CTR-variant` — title rewrite from agent_ctr_optimizer.py
- `trim-rule` — title trim from agent_title_trimmer.py rule-based pipeline
- `trim-gemini` — title trim from agent_title_trimmer.py Gemini self-critic escalation

Baseline metrics: pre-change 14-day GSC clicks / impressions / CTR / position.
Filled from reports/gsc-full-audit-2026-05-10.json snapshot.

| Site | Page | Change type | Pre impr/7d | Pre pos | Old title (len) | New title (len) |
|---|---|---|---|---|---|---|
| matelas | `/en/avis/tediber/` | CTR-variant | ~64 (extrapolated) | 4.8 | `Tediber Review: Our Opinion on the French Brand` (47) | `Tediber Mattress Review 2026: Tested & Rated by Experts` (55) |
| pixinstant | `/en/comparatif/instax-mini-12-vs-mini-11/` | trim-rule (pipe_drop) | 374 | 9.5 | `Instax Mini 12 vs 11 (2026): 7 Real Differences \| Which to Buy?` (63) | `Instax Mini 12 vs 11 (2026): 7 Real Differences` (47) |
| matelas | `/en/test/test-emma-original/` | trim-gemini | 229 | 8.2 | `Emma Original Review 2026: 100-Night Test \| 3 Flaws Sellers Hide` (64) | `Emma Original Review 2026: 3 Flaws Sellers Hide` (47) |
| pixinstant | `/en/comparatif/instax-vs-polaroid-2026/` | trim-gemini | 168 | 6.9 | `Instax vs Polaroid 2026: Full Comparison \| Film Price, Format & Quality` (71) | `Instax vs Polaroid 2026: Full Comparison & Film Price` (53) |
| matelas | `/en/test/test-morphea-jade/` | trim-gemini | 142 | 4.4 | `Morphea Jade Test 2026: 100 Nights \| French Craftsmanship Worth €899?` (69) | `Morphea Jade Test 2026: French Craftsmanship Worth €899?` (56) |
| aspirateur | `/en/comparatif/best-stick-vacuums-2026/` | trim-rule (pipe_drop) | 131 | 9.1 | `Best Stick Vacuums 2026: 12 Tested, 3 to Avoid \| Buyer's Guide` (62) | `Best Stick Vacuums 2026: 12 Tested, 3 to Avoid` (46) |
| pixinstant | `/en/guide/instax-mini-12-film-guide/` | trim-gemini | 117 | 8.5 | `Instax Mini 12 Film Guide 2026: Price, Where to Buy & Money-Saving Tips` (71) | `Instax Mini 12 Film 2026: Price, Where to Buy, Money-Saving` (59) |
| pixinstant | `/en/test/instax-mini-12-review/` | trim-gemini | 116 | 10.3 | `Instax Mini 12 Review 2026: 2-Week Test \| True Cost Per Photo Revealed` (70) | `Instax Mini 12 Review 2026: True Cost Per Photo Revealed` (56) |
| bureau | `/en/comparatif/vs/flexispot-e7-vs-flexispot-e7-pro/` | trim-gemini | 85 | 8.3 | `FlexiSpot E7 vs FlexiSpot E7 Pro: Which One Should You Choose in 2026?` (70) | `FlexiSpot E7 vs. E7 Pro: Which to Choose in 2026?` (49) |
| pixinstant | `/en/guide/polaroid-film-complete-guide-2026/` | trim-gemini | 85 | 8.3 | `Polaroid Film Guide 2026: i-Type vs 600 vs SX-70 \| Prices & Where to Buy` (72) | `Polaroid Film Guide 2026: Compare Types & Where to Buy` (54) |
| matelas | `/en/test/test-epeda-echappee/` | trim-rule (boilerplate) | 84 | 4.2 | `Épéda L'Échappée Test 2026: 100 Nights \| What Reviews Don't Say` (63) | `Épéda L'Échappée Test 2026: 100 Nights` (38) |
| matelas | `/en/comparatif/meilleur-matelas-tediber/` | trim-rule (pipe_drop) | 81 | 6.2 | `Best Tediber Mattress 2026: Review & Complete Guide \| Matelas Expert` (68) | `Best Tediber Mattress 2026: Review & Complete Guide` (51) |

## Cohort composition

By change type:
- 1 CTR-variant
- 4 trim-rule (pipe_drop or boilerplate — clean removal of branding/boilerplate suffix)
- 7 trim-gemini (Gemini escalation triggered by hook loss or year loss in best rule output)

By position band:
- P1 (pos 1-5): 2 entries (Morphea, Épéda) — title/meta-only band
- P2 (pos 6-10): 9 entries — title + content band
- P3 (pos 11+): 1 entry (Mini 12 Review pos 10.3, borderline) — would normally defer to rank work

By site:
- pixinstant: 5
- matelas: 5
- aspirateur: 1
- bureau: 1

## Measurement plan

Re-run `python3 scripts/gsc_full_audit.py` on:
- **2026-05-25** (T+14): early signal on CTR uplift from clean title-truncation fix
- **2026-06-08** (T+28): full window after Google reindex stabilization

For each row above, log:
- Δclicks / Δimpressions / ΔCTR / Δposition vs the 14-day baseline pre-change
- Rank-controlled ΔCTR: subtract the CTR change that would have happened from rank movement alone (use Backlinko Q4 2025 curve in `agent_ctr_optimizer.py:EXPECTED_CTR_BY_POSITION`)

**Kill criterion**: if median rank-controlled ΔCTR at T+28 is not ≥+15%, this cohort failed and we revert.

## Cohort 2026-05-11 (cohort 2, n=9) — extension

Lower-impression range (37-63 impr/7d) but still trims with hook/year preserved.

| Site | Page | Change type | Pre impr/7d | Pre pos | Old title (len) | New title (len) |
|---|---|---|---|---|---|---|
| aspirateur | `/en/guide/best-robot-vacuum-under-300-2026/` | trim-rule (pipe_drop) | 63 | 7.7 | `Best Robot Vacuum Under $300 2026: 5 Tested & Ranked \| #1 for Small Homes` (73) | `Best Robot Vacuum Under $300 2026: 5 Tested & Ranked` (52) |
| pixinstant | `/en/comparatif/polaroid-now-gen2-vs-instax-mini-12/` | trim-rule (pipe_drop) | 59 | 8.4 | `Polaroid Now Gen 2 vs Instax Mini 12: 6 Differences Tested \| Buyer's Guide` (74) | `Polaroid Now Gen 2 vs Instax Mini 12: 6 Differences Tested` (58) |
| matelas | `/en/comparatif/meilleurs-matelas-moins-de-500-euros/` | trim-rule (pipe_drop) | 53 | 8.1 | `Best Mattresses Under €500 2026: 7 Tested \| Quality on a Budget` (63) | `Best Mattresses Under €500 2026: 7 Tested` (41) |
| pixinstant | `/en/test/polaroid-now-gen2-review/` | trim-gemini | 49 | 9.6 | `Polaroid Now Gen 2 Review 2026: Large Format Tested \| Worth €130?` (65) | `Polaroid Now Gen 2 Review 2026: Worth €130?` (43) |
| pixinstant | `/en/comparatif/best-instant-camera-under-100-2026/` | trim-rule (pipe_drop) | 41 | 8.3 | `Best Instant Camera Under £100 2026: 5 Tested \| #1 for Beginners` (64) | `Best Instant Camera Under £100 2026: 5 Tested` (45) |
| pixinstant | `/comparatif/meilleur-instax-mini-2026/` (FR) | trim-rule (pipe_drop) | 39 | 9.5 | `Meilleur Instax Mini 2026 : Mini 12 vs 11 vs 99 Testés \| Notre Gagnant` (70) | `Meilleur Instax Mini 2026 : Mini 12 vs 11 vs 99 Testés` (54) |
| matelas | `/en/comparatif/meilleur-matelas-latex-naturel/` | trim-rule (pipe_drop) | 38 | 7.7 | `Best Natural Latex Mattress 2026: 7 Tested \| Organic & Sustainable` (66) | `Best Natural Latex Mattress 2026: 7 Tested` (42) |
| matelas | `/en/guide/tailles-matelas-dimensions/` | trim-gemini | 37 | 8.1 | `Mattress Sizes & Dimensions Guide 2026: EU, UK & US Comparison Chart` (68) | `Mattress Sizes Guide 2026: EU, UK & US Comparison Chart` (55) |
| aspirateur | `/es/comparatif/mejores-aspiradoras-sin-cable-2026/` | trim-rule (pipe_drop) | 37 | 4.5 | `Mejores Aspiradoras sin Cable 2026: 5 Testadas \| Ganadora Sorpresa` (66) | `Mejores Aspiradoras sin Cable 2026: 5 Testadas` (46) |

Cohort 2 composition: 7 rule-based trims + 2 Gemini self-critic escalations (Polaroid €130 hook recovery; Mattress Sizes Comparison Chart hook recovery).

By position band:
- P1 (pos ≤ 5): 1 (Mejores Aspiradoras ES pos 4.5)
- P2 (pos 6-10): 8

By site: matelas 3, pixinstant 4, aspirateur 2.

## Combined cohort totals (cohort 1 + cohort 2)

- **21 changes** across 5 sites (matelas 8, pixinstant 9, aspirateur 3, bureau 1)
- Total pre-change weekly impressions ≈ 2,400
- 1 CTR-variant rewrite, 11 trim-rule, 9 trim-gemini

## Trimmer agent self-critic enhancements (2026-05-11)

The cohort 2 review surfaced gaps in the original critic. Added in `scripts/agent_title_trimmer.py`:
- **Signal-retention floor**: escalate to Gemini if trim < 60% of original length when original > 50 chars. Recovers value props the rule pipeline drops too aggressively.
- More hook intrigue regex coverage (currency, "compare types", "where to buy", "money-saving").

Test suite: `scripts/test_title_trimmer.py` — 16/16 pass.

## Cohort 2026-05-11 (cohort 3, n=21) — locale extension

Extends cohorts 1+2 (EN-only) to the FR/DE/ES/IT translations of the same product pages. Same change-date 2026-05-11.

| Site | Page | Change type | Locale | Old len | New len |
|---|---|---|---|---|---|
| matelas | `/test/test-morphea-jade/` | trim-gemini | fr | 68 | 48 |
| matelas | `/comparatif/meilleurs-matelas-moins-de-500-euros/` | trim-rule | fr | 63 | 46 |
| matelas | `/comparatif/meilleur-matelas-tediber/` | trim-rule | fr | 69 | 52 |
| matelas | `/de/test/test-emma-original/` | trim-gemini | de | 71 | 60 |
| matelas | `/de/test/test-morphea-jade/` | trim-gemini | de | 62 | 57 |
| matelas | `/de/comparatif/meilleurs-matelas-moins-de-500-euros/` | trim-rule | de | 70 | 53 |
| matelas | `/de/comparatif/meilleur-matelas-tediber/` | trim-rule | de | 67 | 50 |
| matelas | `/es/test/test-emma-original/` | trim-gemini | es | 67 | 45 |
| matelas | `/es/comparatif/meilleurs-matelas-moins-de-500-euros/` | trim-rule | es | 65 | 48 |
| matelas | `/es/comparatif/meilleur-matelas-tediber/` | trim-rule | es | 68 | 51 |
| matelas | `/it/test/test-emma-original/` | trim-gemini | it | 68 | 44 |
| matelas | `/it/comparatif/meilleurs-matelas-moins-de-500-euros/` | trim-rule | it | 65 | 48 |
| matelas | `/it/comparatif/meilleur-matelas-tediber/` | trim-rule | it | 76 | 59 |
| bureau | `/de/comparatif/vs/flexispot-e7-vs-flexispot-e7-pro/` | trim-gemini | de | 69 | 57 |
| pixinstant | `/comparatif/instax-mini-12-vs-mini-11/` | trim-rule | fr | 66 | 53 |
| pixinstant | `/comparatif/polaroid-now-gen2-vs-instax-mini-12/` | trim-gemini | fr | 73 | 55 |
| pixinstant | `/it/comparatif/meilleur-instax-mini-2026/` | trim-rule | it | 61 | 48 |
| pixinstant | `/it/comparatif/polaroid-now-gen2-vs-instax-mini-12/` | trim-gemini | it | 67 | 54 |
| pixinstant | `/de/comparatif/instax-mini-12-vs-mini-11/` | trim-rule | de | 73 | 60 |
| pixinstant | `/es/comparatif/instax-mini-12-vs-mini-11/` | trim-rule | es | 62 | 49 |
| pixinstant | `/es/comparatif/polaroid-now-gen2-vs-instax-mini-12/` | trim-gemini | es | 69 | 51 |

**Excluded from cohort 3** (self-critic rejection): pixinstant `/de/comparatif/polaroid-now-gen2-vs-instax-mini-12/` — rule trim landed at 36 chars dropping the "Welche Sofortbildkamera?" CTA that's the page's main differentiator. Kept in queue for manual review.

Cohort 3 composition: 13 rule-based + 8 Gemini self-critic escalations.
Locale mix: 3 FR + 6 DE + 5 ES + 5 IT + 2 FR-pixinstant.

**Measurement note**: cohort 3 pages have lower individual impression volumes than cohort 1+2 (these are non-EN locales of the same products; EN dominates impressions on these sites). Cohort 3 is best treated as a **cohort-level signal** at T+28 rather than per-URL.

## Combined cohort totals (cohorts 1+2+3)

- **42 changes** across 4 sites (matelas 21, pixinstant 16, aspirateur 3, bureau 2)
- 22 trim-rule + 17 trim-gemini + 1 CTR-variant + 2 excluded by self-critic
- All EN versions of these 4 product pages now ≤60 chars; FR/DE/ES/IT versions also ≤60 chars (with the 1 documented exclusion)

## Exclusions

- Brewmance: canonical recovery window through 2026-06-07 — re-evaluate after.
- WeLoveInstant: out of scope for the wider CTR plan.

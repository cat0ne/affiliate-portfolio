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

## Exclusions

- Brewmance: canonical recovery window through 2026-06-07 — re-evaluate after.
- WeLoveInstant: out of scope for the wider CTR plan.

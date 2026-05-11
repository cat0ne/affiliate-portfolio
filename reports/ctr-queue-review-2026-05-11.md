# CTR queue review — 2026-05-11

Cross-references `reports/agent_queues/ctr_proposed/2026-05-11.json` (22 proposals)
against `reports/agent_queues/title_trim_proposed/2026-05-11.json` (1055 proposals).

Headline finding: only **1 of 22** CTR-optimizer variants is an improvement over the current title. The rest are either:
- **Better served by a length trim** (14 entries — current title is good, just truncated in SERP)
- **No improvement available** (7 entries — current title is fine; agent's variant is worse)

This validates the user's instinct that the CTR optimizer was the wrong tool for most of the work; length is the real problem.

## Apply (1 proposal)

| Site | URL | Current | Proposed | Why |
|---|---|---|---|---|
| matelas | `/en/avis/tediber/` | `Tediber Review: Our Opinion on the French Brand` | `Tediber Mattress Review 2026: Tested & Rated by Experts` | Adds year + specificity; current title is generic and dateless. 47→53 chars. |

To apply: edit the title in the MDX directly or `python3 scripts/agent_ctr_optimizer.py --site matelas --apply` (will emit all matelas proposals — review first).

## Defer to title trimmer (14 proposals)

These current titles are good content, just over 60 chars. The CTR optimizer's "Which X Is Worth It in 2026? Honest Review" variants are generic with lowercase entity names — clearly worse than what's there. Use the length-trimmer queue instead.

| Site | Pos | Impr | Current title (length) | Trimmer proposal |
|---|---|---|---|---|
| bureau | 7.0 | 309 | `Ergonomic Chair Under €300 [2026]: Top 5 Tested \| Bureau Expert` (63) | `Ergonomic Chair Under €300 [2026]: Top 5 Tested` (47) |
| matelas | 4.2 | 586 | `Morphea Jade Test 2026: 100 Nights \| French Craftsmanship Worth €899?` (69) | `Morphea Jade Test 2026: 100 Nights` (34) |
| matelas | 3.9 | 236 | `Épéda L'Échappée Test 2026: 100 Nights \| What Reviews Don't Say` (63) | `Épéda L'Échappée Test 2026: 100 Nights` (38) |
| matelas | 7.7 | 510 | `Emma Original Review 2026: 100-Night Test \| 3 Flaws Sellers Hide` (64) | `Emma Original Review 2026: 100-Night Test` (41) |
| matelas | 7.1 | 336 | `Best Memory Foam Mattresses 2026: 7 Tested \| Our #1 for Hot Sleepers` (68) | `Best Memory Foam Mattresses 2026: 7 Tested` (42) |
| ... | ... | ... | (9 more in `ctr_proposed/2026-05-11.json` cross-referenced with `title_trim_proposed/2026-05-11.json`) | |

Inspect via:
```bash
python3 -c "import json; q=json.load(open('reports/agent_queues/ctr_proposed/2026-05-11.json')); t={x['mdx_path']:x for x in json.load(open('reports/agent_queues/title_trim_proposed/2026-05-11.json'))['proposals'] if x['proposed_title']}; [print(p['site'], p['current_title'], '->', t[p['mdx_path']]['proposed_title']) for s in q['sites'].values() for p in s if p.get('mdx_path') in t]"
```

## Reject (7 proposals)

The agent produced a variant but it's worse than the current title. No action needed.

| Site | Page | Why reject |
|---|---|---|
| aspirateur | `/en/test/test-dreame-l10s-ultra/` | Variant: lowercase brand `dreame l10s ultra`, drops the strong "€300 Cheaper Than Roborock" hook |
| matelas | `/en/` homepage | Queue shows `current_title: "en"` but that's a queue artifact — the homepage has no MDX file, so `agent_ctr_optimizer.py` fell back to the URL slug. The actual page metadata in `src/app/[locale]/_en.tsx:21` is `"Best Mattresses 2026: 7 Tested & Compared \| Back Pain Relief \| " + site.name` — already strong. Agent should skip pages with no MDX backing; minor follow-up. |
| pixinstant | `/en/guide/how-to-tell-if-instax-film-is-expired/` | Variant garbles "How To Tell If Instax" into "Ultimate How To Tell If Instax Buyer's Guide". Current is fine. |
| aspirateur | `/en/test/test-dyson-v15-detect/` | Current "Dyson V15 Detect Review 2026: Laser Tested 25h — Worth €649?" is strong; no improvement from either queue. |
| matelas | `/comparatif/meilleur-oreiller-memoire-de-forme/` | Current FR title is fine. |
| pixinstant | `/en/comparatif/instax-mini-99-vs-mini-12/` | Current is fine; variant generates nonsense "7 Best Instax Mini 99 Mini 12". |
| pixinstant | `/en/guide/best-instant-camera-for-kids-2026/` | Current strong. |

## Action items

1. **Apply the 1 Tediber EN avis title** manually or via `--site matelas --apply` (review first).
2. **(Skip)** — the `/en/` "title=en" was a queue artifact, not a page bug. Homepage metadata is correct in `_en.tsx`. Minor follow-up: have `agent_ctr_optimizer.py` skip URLs with no MDX backing rather than falling back to slug-as-title.
3. **Review the trimmer queue** for the 14 entries above (and 1041 others).
4. **Skip rerunning the CTR optimizer until the trimmer has run its course** — they overlap.

## Self-critic on this review

- Bucket A was initially 4 entries; manual inspection downgraded 3 to "reject" (lowercase brands, garbled phrasing, worse than current). The triage rule (`uplift >= 1.5 and entity_match`) is too permissive — entity-match-by-token doesn't catch lowercase brand drift.
- The `/en/` homepage bug surfaced as a side effect. Worth a separate ticket.
- Did not use Apify — the data needed was titles + positions, both in GSC export. No ASIN lookups required.

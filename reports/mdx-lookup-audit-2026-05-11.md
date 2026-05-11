# MDX lookup audit — 2026-05-11

After fixing `agent_ctr_optimizer.py:_find_mdx_for_url` to handle both parallel (`content-en/`) and nested (`content/en/`) layouts, audited every other script in `scripts/` for the same pattern.

## Summary table

| Script | Function:line | Bug present | Write impact | Fixed |
|---|---|---|---|---|
| `agent_ctr_optimizer.py` | `_find_mdx_for_url` | N | high (queue-only currently) | ✓ commit `9d5fc8b` |
| `agent_title_trimmer.py` | `scan_site` | N (correct — enumerates `content*`) | medium | n/a |
| `agent_url_health.py` | `_site_slugs` | **Y** | **high** | ✓ commit (this PR) |
| `agent_cro_optimizer.py` | `rewrite_asin_in_files` | N (false positive from audit — `content/**/*.mdx` does catch nested) | high | verified, no change |
| `agent_seo_auditor.py` | `find_mdx_files` | Partial (auto-fix path) | high | deferred |
| `agent_writer.py` | `resolve_mdx_path` | Partial (fallback `rglob("*.mdx")`) | high | **fixed** (this commit) |
| `agent_translator.py` | `resolve_mdx_path` | Partial (fallback `rglob("*.mdx")`) | high | **fixed** (this commit) |
| `agent_reviewer.py` | `resolve_mdx_path` | Partial (fallback `rglob("*.mdx")`) | low (read-only analysis) | **fixed** (this commit) |
| `agent_syndication.py` | `_mdx_inventory` | Partial (post-filters correctly) | low (queues events; doesn't write) | deferred |

## Concrete impact of the url_health fix

`agent_url_health.py:_site_slugs` builds the slug → public-path inventory used by the 404 reaper. The old `repo.rglob("content/**/*.mdx")` pattern silently missed every file under `content-en/`, `content-de/`, `content-es/`, `content-it/` (parallel layout on matelas/bureau/cafe/aspirateur). Measured:

- matelas: pre-fix 134 slugs (FR only), post-fix 667 slugs (5 locales × ~130 each)
- bureau: pre-fix 99 → post-fix 587 (6 locales including `content-uk/`)
- cafe: pre-fix 79 → post-fix 488
- aspirateur: pre-fix 56 → post-fix 326

That's **548–620 MDX files silently invisible per site** when matching 404s against the inventory. Behavior on 404 of `/en/test/foo-renamed/`:
- Pre-fix: empty EN slug pool → no redirect proposed → 404 persists → ranking lost
- Post-fix: matches against full EN inventory → fuzzy-matches `/en/test/foo/` if similar → 301 redirect proposed

Bonus: old code recursed into `node_modules`, which is why matelas's rglob returned 898 (with junk) vs the corrected 667.

## Deferred — high-risk, defer-with-explicit-reason

~~`agent_writer.py` and `agent_translator.py` use a fallback `rglob("*.mdx")`…~~ **Fixed in follow-up commit.** Both now:

- Drop the cross-locale `repo.rglob("*.mdx")` terminal fallback entirely.
- For default-locale resolution on `content/`, exclude `content/<en|de|es|it|uk|ja>/` subtrees (closes nested-layout cross-locale leak on aspirateur/pixinstant).
- For non-default locale, search only `content-<loc>/**` AND `content/<loc>/**`.
- For monorepo sites, scope rglob to `content/*/<loc>/**` so an EN resolve cannot return a JA file.
- Caller sites verified: both `process_event` and `process_events_parallel` in writer, plus `process_event`/CLI test in translator, all pass `locale_hint` from the event payload. No caller changes needed.

Coverage: `scripts/test_writer_translator_paths.py` — 18 tests including synthetic-fixture tests that prove the fallback is dead for writer, translator, and reviewer.

## Follow-up: `agent_reviewer.py` fix

Same shape as writer/translator: dropped the cross-locale `repo.rglob("*.mdx")` terminal fallback, added parallel + nested layout search, exclude `content/<other-locale>/` subtrees on default-locale resolution, and scope monorepo rglob to `content/*/<loc>/**`. Reviewer is read-only so production harm was low (wrong-locale feedback is ignored or causes a confusing review note), but consistency across the three agents is worth the small change. Caller verified: single call site at `process_event` already passes `locale` from the event payload.

## `agent_cro_optimizer.py` — false positive from audit

The audit agent claimed `cro_optimizer.rewrite_asin_in_files` misses nested layouts. Verified empirically: `pixinstant/content/**/*.mdx` returns 355 files including 73 under `/en/`. The `**` recursion does descend into `content/en/`, so no bug exists. No change needed.

## What's correct already

`agent_title_trimmer.py:scan_site` enumerates `content*` top-level dirs and processes each independently with explicit locale detection. Reference implementation — same pattern was used for the url_health fix.

## Test coverage

- `scripts/test_url_health_slugs.py` — 4 tests, all pass. Verifies multi-locale inventory + no node_modules pollution.
- `scripts/test_mdx_lookup.py` — 5 tests for the ctr_optimizer fix.
- `scripts/test_ctr_guardrails.py`, `scripts/test_title_trimmer.py` — broader coverage.

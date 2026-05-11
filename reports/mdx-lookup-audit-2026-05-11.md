# MDX lookup audit — 2026-05-11

After fixing `agent_ctr_optimizer.py:_find_mdx_for_url` to handle both parallel (`content-en/`) and nested (`content/en/`) layouts, audited every other script in `scripts/` for the same pattern.

## Summary table

| Script | Function:line | Bug present | Write impact | Fixed |
|---|---|---|---|---|
| `agent_ctr_optimizer.py` | `_find_mdx_for_url` | N | high (queue-only currently) | ✓ commit `9d5fc8b` |
| `agent_title_trimmer.py` | `scan_site` | N (correct — enumerates `content*`) | medium | n/a |
| `agent_url_health.py` | `_site_slugs` | **Y** | **high** | ✓ commit (this PR) |
| `agent_cro_optimizer.py` | `rewrite_asin_in_files` | N (false positive from audit — `content/**/*.mdx` does catch nested) | high | verified, no change |
| `agent_seo_auditor.py` | `find_mdx_files` | Partial (auto-fix path) | high | **fixed** (this commit) |
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

## Follow-up: `agent_seo_auditor.py` fix

Different shape from the writer/translator/reviewer trio because `find_mdx_files` returns a **list** of files (not a single resolved path). The real wrong-locale write was in the auto-fixer's `add_canonical` branch, which built `https://www.<domain>/{slug}` with no locale prefix — so a file at `content-en/tests/foo.mdx` got the FR canonical URL written into it.

Changes:

- `find_mdx_files` now returns `list[tuple[Path, locale, content_type]]`, enumerating top-level `content*` dirs explicitly (matches `agent_title_trimmer.scan_site` and `agent_url_health._site_slugs`). The dead fallback at line 211 (re-globbing `content/**/*.mdx` inside `if not mdx_files`, after the same glob already ran in the loop above) is dissolved.
- Monorepo layout (`src/app/**/content/...`) preserved as a fallback when no top-level `content*` dir exists (covers airpurify/safehive/pawhive).
- `audit_page` now accepts `locale` + `content_type` and tags every emitted issue with them.
- `apply_fix(add_canonical)` reads the locale/content_type tag and constructs `https://www.<domain>{/<locale>}{/<type>}/<slug>`. Default-locale files get the unprefixed URL; localized files get `/en/...`, `/de/...`, etc.
- Both internal callers (`run_site_audit`, CLI specific-checks branch) updated to consume the tuple shape.

Caller-contract change: legacy `list[Path]` → `list[tuple[Path, str, str]]`. No external imports of `find_mdx_files` exist (`grep -rn "find_mdx_files\|from agent_seo_auditor"` returns only the two internal callers), so no compatibility wrapper was added — documented in this report instead.

Coverage: `scripts/test_seo_auditor_scan.py` — 6 tests including the synthetic duplicate-slug case from the audit (matelas-like layout with `content/tests/test-foo.mdx` + `content-en/tests/test-foo.mdx`). The smoking-gun test runs `apply_fix(add_canonical)` on the EN file and asserts the written canonical contains `/en/test/test-foo`; the FR file's canonical is asserted to NOT contain `/en/`. Production sanity verified on real sites: matelas yields 667 files across 5 locales correctly tagged, pixinstant yields 340 across 5 locales correctly tagged (no FR-mistag of `content/en/` files), airpurify yields 55 EN files via the monorepo fallback.

## `agent_cro_optimizer.py` — false positive from audit

The audit agent claimed `cro_optimizer.rewrite_asin_in_files` misses nested layouts. Verified empirically: `pixinstant/content/**/*.mdx` returns 355 files including 73 under `/en/`. The `**` recursion does descend into `content/en/`, so no bug exists. No change needed.

## What's correct already

`agent_title_trimmer.py:scan_site` enumerates `content*` top-level dirs and processes each independently with explicit locale detection. Reference implementation — same pattern was used for the url_health fix.

## Test coverage

- `scripts/test_url_health_slugs.py` — 4 tests, all pass. Verifies multi-locale inventory + no node_modules pollution.
- `scripts/test_mdx_lookup.py` — 5 tests for the ctr_optimizer fix.
- `scripts/test_writer_translator_paths.py` — 18 tests, all pass. Covers writer/translator/reviewer.
- `scripts/test_seo_auditor_scan.py` — 6 tests, all pass. Covers parallel + nested layout enumeration, locale tagging, and the auto-fix locale-correct canonical write.
- `scripts/test_ctr_guardrails.py`, `scripts/test_title_trimmer.py` — broader coverage.

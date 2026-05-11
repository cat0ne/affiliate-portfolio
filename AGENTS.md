# AGENTS.md — Affiliation Sites

This file provides project-wide context for AI coding agents.

## Amazon Associates OneLink Tags

All affiliate sites share the same Amazon Associates OneLink account (Store ID: `zoomzen21-21`).
Use the correct **tracking ID** for each locale when generating or rewriting Amazon links.

| Marketplace   | Locale | Tracking ID     | Domain         | Notes |
|---------------|--------|-----------------|----------------|-------|
| 🇩🇪 Germany   | `de`   | `zoomzen-21`    | `amazon.de`    | OneLink associated |
| 🇪🇸 Spain     | `es`   | `zoomzen08-21`  | `amazon.es`    | OneLink associated |
| 🇫🇷 France    | `fr`   | `zoomzen05-21`  | `amazon.fr`    | **Primary FR tag** — OneLink associated |
| 🇮🇹 Italy     | `it`   | `zoomzen01-21`  | `amazon.it`    | OneLink associated |
| 🇬🇧 UK        | `en-gb`| `zoomzen07-21`  | `amazon.co.uk` | OneLink associated |
| 🇺🇸 USA       | `en`   | `zoomzus-20`    | `amazon.com`   | OneLink associated |

### Store History
- **Original store**: `zoomzen21-21` — created first, generated revenue but no OneLink/i18n
- **Current primary**: `zoomzen05-21` — same account, OneLink-enabled, used for all sites
- All tracking IDs belong to the same Amazon Associates account (Guillaume Hochard)

**Rules:**
- Never hardcode affiliate tags in content — always use the config/rewriter.
- FR is the default marketplace; all other locales must be localized via `localizeAmazonUrl()` or equivalent.
- OneLink is active on Aspirateur and Cafe; PixInstant uses locale-aware rewriting.

## Sites

| Site           | URL production                | Repo GitHub                                      |
|----------------|-------------------------------|--------------------------------------------------|
| SafeHive       | https://safehivehq.com        | https://github.com/cat0ne/affiliate-suite        |
| AirPurify      | https://airpurify.com         | (same repo)                                      |
| PawHive        | https://pawhivehq.com         | (same repo)                                      |
| Matelas Expert | https://www.matelas-expert.fr | https://github.com/cat0ne/matelas-literie        |
| Top-Aspirateur | https://www.top-aspirateur.fr | https://github.com/cat0ne/meilleur-aspirateur    |
| Brewmance      | https://www.brewmance.fr      | https://github.com/cat0ne/affiliation-cafe       |
| PixInstant     | https://www.pixinstant.com    | https://github.com/cat0ne/affiliation-pixinstant |
| Bureau Expert  | https://www.bureau-expert.fr  | https://github.com/cat0ne/bureau-expert          |

> **Note:** SafeHive, AirPurify and PawHive share the same monorepo (`affiliate-suite`). The 5 legacy sites (Matelas, Aspirateur, Café, PixInstant, Bureau) are separate repos.
>
> **Legacy directory mapping:**
> | Site         | Directory     |
> |--------------|---------------|
> | Matelas      | `matelas/`    |
> | Aspirateur   | `aspirateur/` |
> | Cafe         | `cafe/`       |
> | PixInstant   | `pixinstant/` |
> | Bureau       | `bureau/`     |

## Stack (all sites)

- Next.js 16 App Router + React 19 + TypeScript 5
- Tailwind CSS v4
- MDX file-based content
- next-intl i18n (FR/EN/DE/IT/ES)
- AWS Amplify deployment

## Cross-Site Propagation Rule

**When a fix or design change is made on one affiliation site, check whether it should also apply to the other 4.**

The 5 sites share most architectural patterns (next-intl, MDX content, comparison tables, ProductScoreCard, affiliate-link rewriting, language switcher, etc.). Bugs and visual issues are almost always systemic, not site-specific.

Before declaring a single-site change done:

1. `grep -rn "<changed-symbol-or-class>" {aspirateur,bureau,cafe,matelas,pixinstant}/src` to find equivalent code paths.
2. If 2+ sites match, propagate the fix in the same PR series (parallel agents are fine).
3. If the change is genuinely site-specific (niche-specific copy, niche-specific component), say so explicitly in the commit/PR body so future maintainers know not to copy it blindly.

This applies to: bug fixes, visual/design tweaks, accessibility improvements, i18n fixes, SEO fixes, performance optimizations, and security/safety hardening. It does NOT apply to: niche-specific content, per-site product data, per-site theme colors.

## Portfolio automation (MVP pipeline)

Daily GitHub Actions (`automation.yml`) sets **`AFFILIATION_SITES_ROOT`** to the workspace root (the
`affiliate-portfolio` clone that contains `matelas/`, `aspirateur/`, `affiliate-suite/`, etc.). All
agents that used a hardcoded Mac path now call **`scripts/affiliate_paths.portfolio_root()`**, so the
same scripts run locally and in CI.

**Machine DB + Hermes in CI:** the workflow symlinks `~/affiliate-machine.db` → `data/affiliate-machine.db`
and `~/hermes-events` → `.hermes-events/` so existing agents keep using home-relative paths while state
lives in the workspace for that job. **`pipeline_import_gsc_daily.py`** fills **`gsc_page_daily`** (GSC
page × date) so CWV, CTR, and canary use real click/impression totals without relying on legacy
`page_metrics` alone.

**Hermes bus (shared claim API):** all consumers use **`scripts/hermes_bus.py`** — **`claim_inbox_json()`**
for inbox → processing, then **`complete_claimed_event()`** / **`fail_claimed_event()`** (or **`retry_or_fail_claimed_event()`**). Prefer **`HERMES_EVENTS_ROOT`** (queue base: `inbox/`, `processing/`, …). Back-compat: **`HERMES_EVENTS_DIR`** = path to `inbox/`. **`HERMES_CLAIM_STALE_SECONDS`** (default `86400`) removes stale `.claim` files under `state/` so crashed workers cannot block forever. Emitters resolve the inbox the same way (**`ensure_hermes_dirs()`**); use **`write_inbox_event_json()`** for atomic writes. **`scripts/test_hermes_bus_smoke.py`** exercises claim → complete on a temp dir (also runs in **`automation.yml`** after `pip install`). **`scripts/run-weekly-hermes.sh`** / **`run-weekly-report.sh`** set **`HERMES_EVENTS_DIR`** from **`HERMES_EVENTS_ROOT/inbox`** when the inbox variable is unset (else default **`$HOME/hermes-events/inbox`** with the CI symlink).

**Multi-repo publishing:** site content still lives in separate GitHub repos; the publisher opens PRs
per `SITE_REPOS` / event payload. Ensuring `AFFILIATION_SITES_ROOT` is the portfolio root keeps paths
consistent across all consumers.

## Learned User Preferences

- Prefer wiring Hermes **consumers** (publisher, translator, CRO optimizer) so events produce repo changes and PRs, instead of adding agents that only emit events.
- Treat **end-to-end pipeline behavior** as the success criterion: reliable daily data and detections should flow through to applied site fixes, content updates, and measurable impact—not orphan queues.

## Learned Workspace Facts

- Strategist and headline-style outputs must follow the **actual calendar year** in copy (e.g. 2026; advance appropriately in Q4); stale default years leak into user-visible titles and reports.
- Without **real affiliate revenue or commission signals** in prioritization logic, strategist-style queues skew toward high-impression, low-commercial-intent queries; revenue-aware inputs materially change what gets worked first.


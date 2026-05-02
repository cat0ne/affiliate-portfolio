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

| Site         | Directory   | Domain              | Niche               |
|--------------|-------------|---------------------|---------------------|
| Matelas      | `matelas/`  | `matelas-expert.fr` | Mattress reviews    |
| Aspirateur   | `aspirateur/`| `top-aspirateur.fr` | Vacuum reviews      |
| Cafe         | `cafe/`     | `brewmance.fr`      | Coffee machines     |
| PixInstant   | `pixinstant/`| `pixinstant.com`   | Instant cameras     |
| Bureau       | `bureau/`   | `bureau-expert.fr`  | Office furniture    |

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

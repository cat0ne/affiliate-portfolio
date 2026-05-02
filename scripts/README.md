# Scripts

This directory contains operational and automation scripts for the affiliation-sites monorepo.

## Amazon Price Update System

The price update system keeps `public/amazon-prices.json` files across all 5 sites in sync with live Amazon pricing data.

### How it works

1. A scheduled GitHub Actions workflow (`.github/workflows/update-prices.yml`) runs every **Monday at 06:00 UTC**.
2. The workflow checks out the repository, installs Python dependencies, and executes `scripts/update-amazon-prices.py`.
3. The script queries the Amazon Product Advertising API (PA-API) for every tracked ASIN and writes updated price data to each site's `public/amazon-prices.json`.
4. If any prices changed, the workflow commits the updated files directly to `main` with the message `chore: auto-update Amazon prices [skip ci]`.
5. Because each site deploys via AWS Amplify (connected to GitHub), the push triggers a fresh build and the new prices go live automatically.
6. If no prices changed, the workflow exits cleanly without creating an empty commit.

You can also trigger the workflow manually from the **Actions** tab in GitHub (see `workflow_dispatch`).

### Required secrets / environment variables

| Variable / Secret   | Description                                                    | Required |
|---------------------|----------------------------------------------------------------|----------|
| `PA_API_ACCESS_KEY` | Amazon Product Advertising API access key                      | Yes      |
| `PA_API_SECRET_KEY` | Amazon PA-API secret key                                       | Yes      |
| `PA_API_PARTNER_TAG`| Default partner tag (tracking ID). The script may override this with per-site tags if configured. | Optional |
| `PRICE_UPDATE_PAT`  | GitHub Personal Access Token with `contents:write` permission so the workflow can push commits to `main`. | Yes      |

### Running manually

```bash
# 1. Export credentials
export PA_API_ACCESS_KEY="your-access-key"
export PA_API_SECRET_KEY="your-secret-key"
export PA_API_PARTNER_TAG="your-partner-tag"

# 2. Install dependencies
pip install -r scripts/requirements.txt

# 3. Run the script
python scripts/update-amazon-prices.py
```

### Adding new products to tracking

1. Locate the site's tracking configuration (usually a JSON or YAML file inside the site directory that lists tracked ASINs).
2. Add the new Amazon ASIN and any locale-specific overrides.
3. Run the script manually (see above) to verify the new product is fetched and written correctly.
4. Commit both the configuration change and the updated `public/amazon-prices.json`.

> **Note:** Keep `scripts/requirements.txt` up to date with any Python packages the price update script (or other scripts) depend on.

## CRO / link smoke test

`cro-link-tester.ts` is a Playwright-based smoke test that catches client-side
breakage on the 5 production affiliation sites — specifically the class of bugs
where homepage HTML *looks* correct (article URLs return 200, anchors are
rendered) but clicks on those anchors do not actually navigate (overlays,
hydration crashes, swallowed click handlers, etc.).

For each site it:

1. Loads the homepage and waits for network idle.
2. Picks the first N article cards in `<main>` (default 5, configurable).
3. For each card: verifies it is the topmost element under its centre point,
   actually clicks it, and waits for navigation. If no navigation happens within
   ~10s, the card is flagged as a CRO bug and a full-page screenshot is captured
   to `reports/cro-tester/<site>/`.
4. On the destination page: confirms HTTP 200, an `<h1>` or `<article>` is
   rendered, and every Amazon CTA `<a>` has a `tag=` matching one of the known
   per-site Associates IDs (FR `zoomzen05-21`, EN/US `zoomzus-20`, DE
   `zoomzen-21`, IT `zoomzen01-21`, ES `zoomzen08-21`, UK `zoomzen07-21`).
5. Hits `/en/` per site to confirm locale switching works.

### Running

```bash
# One-time: install deps + Chromium browser (cached afterwards).
cd scripts && npm install && npx playwright install chromium

# Run the tester (headless, all sites).
./scripts/run-cro-tester.sh

# Single site, with visible browser for debugging.
./scripts/run-cro-tester.sh --site bureau --headed

# Test more cards per site.
./scripts/run-cro-tester.sh --max-articles 8
```

### Output

- `reports/cro-tester-YYYY-MM-DD.json` — structured report (machine-readable).
- `reports/cro-tester-latest.md` — Markdown summary with per-site pass/fail.
- `reports/cro-tester/<site>/<timestamp>-<failure>.png` — screenshots on failure.

Exit code is 0 on all-pass, 1 on any failure — suitable as a pre-deploy gate.

### Constraints

- Read-only on the sites: nothing in `bureau/`, `aspirateur/`, etc. is touched.
- Total run time targets ~3 minutes for all 5 sites with default settings.
- If a site is temporarily down, it is logged and the run continues.

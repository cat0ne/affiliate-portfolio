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

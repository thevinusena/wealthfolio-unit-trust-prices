# Wealthfolio Sri Lankan Unit Trust Price Feed

A small, serverless GitHub project that collects unit-trust price history from [CeylonFunds](https://ceylonfunds.lk/), stores the data as CSV files, and publishes those files with GitHub Pages for use in [Wealthfolio](https://wealthfolio.app/).

The feed is configured for these funds:

| Wealthfolio symbol | Fund | CeylonFunds page |
|---|---|---|
| `FCMMF` | First Capital Money Market Fund | [Open page](https://ceylonfunds.lk/funds/first-capital-money-market-fund) |
| `FCFIF` | First Capital Fixed Income Fund | [Open page](https://ceylonfunds.lk/funds/first-capital-fixed-income-fund) |
| `CCGF` | Ceybank Century Growth Fund | [Open page](https://ceylonfunds.lk/funds/ceybank-century-growth) |

## How it works

1. GitHub Actions runs the Python collector once a day at **13:30 UTC (19:00 Sri Lanka time)**. You can also start it manually from the Actions tab.
2. The collector fetches each configured CeylonFunds fund page and extracts the dated price points from the page's Next.js Flight payload.
3. It uses `buying_price` as `close`. CeylonFunds describes the price used for its price-return calculations as the bid price, i.e. the price an investor receives on redemption. This is the appropriate side for Wealthfolio portfolio valuation.
4. It merges incoming history with saved CSV history. Previously saved dates are retained if the website later returns a shorter history.
5. The collector writes the CSVs and a human-readable index into `docs/`.
6. GitHub Actions commits changed files under `docs/` and deploys that directory to GitHub Pages.

**Important:** the workflow time is the collection time, not the unit-price publication date. Always use the `date` column as the actual price date. If CeylonFunds has not yet published a new price, the latest available row may be older than the workflow run.

## Data source and limitations

- Primary source: [CeylonFunds](https://ceylonfunds.lk/), which says its primary unit-price source is the daily table published by the [Unit Trust Association of Sri Lanka (UTASL)](https://www.utasl.lk/unit-prices/).
- CeylonFunds says its public price history begins on **1 October 2025**.
- CeylonFunds describes its data as reproduced from published sources and says it does not independently audit the source data. Verify material prices against the relevant fund manager or UTASL before relying on them.
- The collector records the page's actual `price_date`; it does not use the fetch date, estimate missing prices, interpolate, or forward-fill.
- If a page cannot be fetched or parsed, returns no price points, contains invalid values, or appears to return a shortened history, the previous valid CSV for that fund is retained. Other funds continue to be processed. The workflow deploys last-known-good data where available and then reports an overall failure so the problem is visible in Actions.
- A successful fetch does not guarantee that a new price was published that day. Check the publication date and freshness shown on `index.html`.
- The source page format can change. If that happens, parsing should fail safely rather than invent prices.

### Respectful use and redistribution

This project makes requests only to the public fund pages configured in `funds.json`, with a descriptive User-Agent and a two-second pause between funds. It normally makes one fetch per fund per scheduled run and retries transient failures up to three attempts.

Before publishing or sharing this repository publicly, review CeylonFunds' [data-source explanation](https://ceylonfunds.lk/data-sources), [methodology](https://ceylonfunds.lk/methodology), site terms (if provided), and `robots.txt`. These pages explain sourcing and methodology but do not, by themselves, establish permission to republish the site's collected data. If reuse or redistribution rights are unclear, ask CeylonFunds for permission and follow any applicable terms. The repository should not bypass access controls or anti-bot protections.

## Repository layout

```text
wealthfolio-unit-trust-prices/
├── docs/
│   └── latest/
├── scripts/
│   └── update_prices.py
├── tests/
│   ├── fixtures/
│   │   └── nextjs_flight_sample.html
│   └── test_update_prices.py
├── .github/
│   └── workflows/
│       └── update-prices.yml
├── funds.json
├── requirements.txt
├── README.md
└── .gitignore
```

The collector creates the CSV files and `docs/index.html` on its first successful run.

## Create the GitHub repository

1. Sign in to GitHub and create a new repository named `wealthfolio-unit-trust-prices` (or choose another name and use that name consistently in the Pages URLs below).
2. For GitHub Free hosting, make the repository **public**. The published CSVs and the repository contents will therefore be publicly accessible. Do not put passwords, API keys, personal financial statements, or private account information in this repository.
3. Do not initialise the remote repository with a second README, `.gitignore`, or licence if you already have these files in this local project folder.
4. Upload the complete contents of this project folder to the repository's default branch, which should be `main`. Keep the `.github/workflows/update-prices.yml` folder path exactly as shown.
5. In the repository, open **Settings → Pages**. Under **Build and deployment → Source**, choose **GitHub Actions**. Do not choose the branch `/docs` option; this project uses the Pages Actions deployment in its workflow.
6. Open **Settings → Actions → General**. Confirm GitHub Actions are allowed. Under Workflow permissions, allow the workflow to have the permissions declared in its YAML (`contents: write`). If the repository or organisation policy prevents write access, the workflow will not be able to commit updated CSVs.
7. Open the **Actions** tab, select **Update Unit Trust Prices**, and choose **Run workflow** on `main` for the first run.
8. Open the run and inspect the **Collect and validate unit trust prices** step. Confirm each fund reports `SUCCESS` and a non-zero number of records. If a fund fails, read its error before relying on the published file.
9. When deployment succeeds, open the Pages URL shown in the workflow's `Deploy to GitHub Pages` step. The landing page should list all three funds and their latest verified price dates.

GitHub Pages' official custom-workflow process uses `configure-pages`, `upload-pages-artifact`, and `deploy-pages`. This repository's workflow uses that approach to publish the `docs/` folder.

## Run locally (optional)

Requirements: Python 3.13 or later and internet access.

From the project root:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/update_prices.py
```

The offline test suite checks Next.js payload parsing, bid-price selection, date handling, fund-name matching, history merging and CSV formatting. The script creates or updates the output files under `docs/`. It exits with a non-zero code if one or more funds could not be updated. Do not treat a failed run as a successful full refresh; inspect the log and each fund's latest publication date.

## Output files

After a successful run, the `docs/` directory contains:

```text
docs/
├── index.html
├── FCMMF.csv
├── FCFIF.csv
├── CCGF.csv
├── latest.csv
└── latest/
    ├── FCMMF.csv
    ├── FCFIF.csv
    └── CCGF.csv
```

Every CSV has exactly these columns:

```csv
symbol,date,close,currency
```

- `docs/FCMMF.csv`, `docs/FCFIF.csv`, and `docs/CCGF.csv` contain full available history, sorted by ascending date.
- `docs/latest/FCMMF.csv`, `docs/latest/FCFIF.csv`, and `docs/latest/CCGF.csv` contain one latest verified row per fund.
- `docs/latest.csv` is a combined convenience file with one latest row per fund.
- `docs/index.html` shows the latest saved price date, price, freshness, fetch status, and links to the CSVs.

## Add another fund

Add one object to `funds.json`; no scraper code change should be needed if the fund page follows the same data structure.

Example:

```json
{
  "symbol": "NEWFUND",
  "slug": "the-fund-page-slug",
  "name": "The Exact Fund Name"
}
```

Use the exact slug from the CeylonFunds URL and the exact fund name displayed on its page. The collector checks that the fetched page contains the configured fund name. After adding the entry, run the workflow manually and check that a new `docs/NEWFUND.csv` and `docs/latest/NEWFUND.csv` are generated.

## Configure Wealthfolio

First, make sure each Wealthfolio security has the correct symbol (`FCMMF`, `FCFIF`, or `CCGF`) and is denominated in LKR. This feed supplies redemption/bid prices for valuation; it does not create deposits, buys, sells, dividends, interest entries, or portfolio balances.

In Wealthfolio, open **Settings → Market data → Add custom provider**. Choose CSV for each source and configure the provider as follows. Replace `<username>` and `<repo>` with your GitHub username and repository name.

### Latest source

Use this URL template:

```text
https://<username>.github.io/<repo>/latest/{SYMBOL}.csv
```

Configure:

- Format: `CSV`
- Price path: `close`
- Date path: `date`
- Currency path: `currency` (or leave empty if your Wealthfolio version uses the security's LKR currency)
- Date format: `YYYY-MM-DD`, if the form requires a format

The `{SYMBOL}` placeholder resolves to the Wealthfolio security symbol, so it must match the `symbol` in `funds.json`.

### Historical source

Use this URL template:

```text
https://<username>.github.io/<repo>/{SYMBOL}.csv
```

Configure:

- Format: `CSV`
- Price path: `close`
- Date path: `date`
- Currency path: `currency` (or leave empty if your Wealthfolio version uses the security's LKR currency)
- Date format: `YYYY-MM-DD`, if the form requires a format

The Historical source is optional in Wealthfolio. Use its **Test** function and verify that it returns multiple dated rows in ascending date order. Test Latest separately and verify that it returns exactly one row with the expected fund symbol, redemption price, publication date, and LKR currency. Do not assume the provider works until both tests pass in your installed Wealthfolio version.

After creating the provider, open each security's **Edit → Market data** settings, select this custom provider as its preferred provider, and save. Then run a market-data sync and compare the values and dates with CeylonFunds or the fund manager.

## Troubleshooting

### A fund reports `No price points found`

CeylonFunds may have changed its Next.js page structure or the initial HTML may no longer contain the expected price data. The script intentionally refuses to write a new file in that case. Inspect the page response and update the parser only after confirming the new data structure.

### A fund reports that incoming history is shorter than the saved history

The collector treats this as a possible incomplete response and keeps the existing CSV. Check whether CeylonFunds has shortened its public archive or whether the fetch was partial. Do not delete saved history merely to make the run pass.

### Latest publication date looks old

This can happen when the fund manager/UTASL has not yet published a new price. The schedule runs at 19:00 Sri Lanka time, but the actual source publication date may lag. Check the source page before changing or manually adding any price.

### GitHub Pages does not publish

Check that **Settings → Pages → Source** is set to **GitHub Actions**, that the workflow has `pages: write` and `id-token: write` permissions, and that the `github-pages` deployment environment is not blocked by a protection rule. Open the Actions run and inspect the configure, upload, and deploy steps.

### Wealthfolio cannot read the CSV

Open the published CSV URL directly in a browser. Confirm the response is a CSV file, the file has the exact four-column header, and the URL contains the right repository name and `{SYMBOL}` placeholder. Then use Wealthfolio's Test button and inspect the parsed preview.

## Important

This is a personal data-collection and portfolio-valuation tool, not investment advice. It does not verify fund manager statements independently and must not be used as the sole basis for buying or redeeming units. Check the publication date, source and actual redemption amount before acting.

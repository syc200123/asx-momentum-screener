# ASX Momentum Screener

Automated daily momentum screening of all ASX-listed stocks. Scans ~2,000 stocks overnight via GitHub Actions, publishes results to GitHub Pages. Open the page and the top-scoring stocks are ready.

**Live site:** `https://<your-username>.github.io/asx-momentum-screener/`

## Architecture

```
GitHub Actions (weekdays 6:00 AM AEST)
  └─→ Python scans all ASX stocks (5yr history via Yahoo Finance)
      └─→ Computes momentum scores, streaks, win rates
          └─→ Commits JSON to docs/data/
              └─→ GitHub Pages serves React frontend + data
                  └─→ Open page → results are already loaded
```

## Setup (5 minutes)

### 1. Create the repository

```bash
git clone <this-repo> asx-momentum-screener
cd asx-momentum-screener
git remote set-url origin https://github.com/<your-username>/asx-momentum-screener.git
git push -u origin main
```

### 2. Enable GitHub Pages

1. Go to **Settings → Pages**
2. Source: **Deploy from a branch**
3. Branch: **main**, folder: **/docs**
4. Save

### 3. Run the first scan

Either wait for the next scheduled run (weekdays 6:00 AM AEST), or trigger manually:

1. Go to **Actions → ASX Momentum Scan**
2. Click **Run workflow**

The scan takes roughly 15–30 minutes. Once complete, it commits the JSON data and your site is live.

### 4. Open the site

Navigate to `https://<your-username>.github.io/asx-momentum-screener/`

Results load automatically. No clicks needed.

## Screening Logic

### Momentum Score (0–8)

For each of 8 periods (1wk, 1mo, 3mo, 6mo, 1yr, 2yr, 3yr, 5yr), the stock earns a point if its **geometric daily average return ≥ 0.2%** (i.e., compounding at 0.2%/day over the trading days in that period).

### Streak Analysis

The frontend lets you dynamically choose:

- **Granularity**: Weekly (W), Monthly (M), or Quarterly (Q)
- **Lookback window**: e.g., 52 weeks, 36 months, 20 quarters

For each combination, the app shows:
- **Current streak**: consecutive positive periods ending now (+12 = 12 straight positive weeks)
- **Best streak**: longest positive run in the lookback window
- **Worst streak**: longest negative run
- **Win rate**: percentage of positive periods
- **Avg win / Avg loss**: mean return for positive vs negative periods

Click any row to expand and see a visual heatmap of individual period returns.

### Flags

| Flag | Code | Meaning |
|------|------|---------|
| ↑ Uptrend | CU | Positive returns across 1wk, 3mo, 6mo, 1yr |
| ↑↑ Extended | EU | Uptrend + positive at 2yr and 3yr |
| ⚡ Accel | AC | Each shorter period has higher daily avg than the next longer |
| 🔥 Hot | HS | 8+ consecutive positive weeks |
| ❄ Cold | CS | 6+ consecutive negative weeks |
| ✓ Consistent | CW | 70%+ weekly win rate over 52 weeks |
| ★ New | NU | Entered uptrend since previous scan |
| ↓ Lost | LU | Broke uptrend since previous scan |

### Size Optimisation

Full granular data (every weekly/monthly/quarterly return) is included only for stocks scoring 3+ or flagged as uptrend. All others get summary statistics only. This keeps the JSON under ~5MB for fast page loads.

## File Structure

```
asx-momentum-screener/
├── .github/workflows/scan.yml    # GitHub Actions workflow
├── scripts/scan.py               # Python scanner
├── docs/                         # GitHub Pages root
│   ├── index.html                # Self-contained React frontend
│   └── data/
│       ├── asx_momentum_data.json  # Latest scan results
│       └── snapshots/              # Dated snapshots for comparison
└── README.md
```

## Running Locally

```bash
# Install dependencies
pip install yfinance requests pandas numpy

# Run scan (outputs to docs/data/)
python scripts/scan.py

# Serve locally
cd docs && python -m http.server 8000
# Open http://localhost:8000
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_PRICE` | 0.10 | Minimum stock price filter |
| `WORKERS` | 20 | Parallel fetch threads |
| `OUTPUT_DIR` | docs/data | Output directory for JSON |

## Costs

GitHub Actions free tier provides 2,000 minutes/month. Each scan uses roughly 20–30 minutes, so 5 scans/week ≈ 100–150 min/month — well within the free tier.

---

*General information only. Not personal financial advice.*

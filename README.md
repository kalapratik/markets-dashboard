# Markets

EOD desk for US equities, sectors, global ETFs, rates and credit, FX, commodities, and crypto. Prior-session closes, not a live feed. Static site on GitHub Pages. No API keys, no server, no paid data.

The page reads `site/data/markets.json`. A scheduled workflow rewrites that file after the US cash close and commits it. The ticker list lives in one file: [`universe.json`](universe.json).

Published site: <https://kalapratik.github.io/markets-dashboard/>

## One-time setup

Do these two clicks or the workflows cannot publish or commit.

1. **Pages.** Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**. Save. The deploy workflow publishes the `site/` directory. After the first successful run the site is at <https://kalapratik.github.io/markets-dashboard/>. If the first run fails because Pages was still off, use **Actions → Deploy GitHub Pages → Run workflow** once the source is set.

2. **Actions write.** Repo **Settings → Actions → General → Workflow permissions → Read and write permissions**. Save. The refresh job commits `site/data/markets.json` as `github-actions[bot]`. The workflow file already requests `contents: write`; this setting is what lets that request through. Leave the default branch unprotected, or allow that bot to push to `main`. A protected `main` with no bypass will reject the data commit.

Actions themselves are on by default. The snapshot already in the repo is enough for the first deploy. You do not have to wait for the cron.

To refresh immediately: **Actions → Refresh market data → Run workflow** (branch `main`).

## Schedule

`Refresh market data` runs weekdays at 22:30 UTC (5:30pm ET in winter, 6:30pm ET in summer), plus any manual run. It fetches, commits when the JSON changed, then deploys that commit to Pages. A push from the workflow token does not start the `push` trigger on `Deploy GitHub Pages`, so the refresh workflow calls the deploy itself. A normal push to `main` still starts `Deploy GitHub Pages` on its own.

If Yahoo or Treasury is down and fewer than 60% of the names succeed, the script exits without touching the file, so the site keeps the last good snapshot. Re-run the workflow later. Names that failed on a successful run show in the page footer.

## Add a ticker

Edit [`universe.json`](universe.json). Each group has an `instruments` list. Order in the file is the row order. `column` is `1` (left) or `2` (right).

US-listed price, ETF, future, FX pair, or crypto print on Yahoo:

```json
{ "id": "XBI", "name": "Biotech", "symbol": "XBI" }
```

`id` is what the desk shows. `symbol` is the Yahoo ticker (`^VIX`, `EURUSD=X`, `GC=F`, `BTC-USD`, `DX-Y.NYB`). Optional `decimals` (default 2). Optional `coingecko` id (`bitcoin`, `ethereum`, `solana`) is used only if Yahoo fails for that row.

A Treasury yield (changes print in basis points, not percent):

```json
{ "id": "US2Y", "name": "US 2Y", "kind": "yield", "source": "treasury", "field": "2 Yr", "fred": "DGS2", "session": "yield" }
```

`field` is the column header on the Treasury daily par curve (`2 Yr`, `10 Yr`, `30 Yr`, …). `fred` is the fallback series. Put `"session": "yield"` on yields that sit in a group whose other rows are cash equities, so an open cash session does not treat them as a partial stock print.

USD/CNH is the one pair Yahoo does not publish as a daily history (the chart is a single live quote). It is set to `source: "currency-api"` (keyless daily USD file, weekday prints). If that file is down, `fallbackSymbol` `CNH=F` is the CME offshore RMB future. Any other Yahoo name can take a `fallbackSymbol` the same way.

Commit to `main` (or run the refresh workflow). The next fetch picks the new name up. Locally:

```bash
python3 scripts/fetch_markets.py
python3 -m http.server -d site 8000
```

Open `http://127.0.0.1:8000`. The fetch script is stdlib only.

## What the columns are

Aligned to the anchor session (`SPY`). Anything dated after that session is ignored, so a next-day futures or crypto bar does not sit next to the prior equity close. If you run the script while US cash is still open (before 16:15 America/New_York, weekdays), today's partial US bar is dropped and the as-of date stays on the prior session.

| Column | Prices | Yields |
| --- | --- | --- |
| LAST | Adjusted close | Par yield, percent |
| 1D | 1 session | change, bp |
| 1W | 5 sessions | change, bp |
| 1M | 21 sessions | change, bp |
| YTD | vs last print of the prior calendar year | change, bp |
| 52W | where the last close sits in the trailing 365-day close range | same |
| 3M | last 63 closes | same |

The strip is computed in the browser from that file: tape (SPY, QQQ, IWM, VIX, DXY, BTC, 2Y, 10Y), 2s10s in bp, RSP minus SPY on the day (breadth), and the three largest and three smallest 1D price moves. VIX is tagged `LOW` under 14, `ELEVATED` at 20, `STRESS` at 30. A yield or future whose own print date is behind the equity session is marked on the row.

ETF returns use Yahoo adjusted closes. Futures are the front-month Yahoo series (`GC=F`, `SI=F`, `CL=F`, `BZ=F`, `NG=F`, `HG=F`) and can gap on the roll. FX and crypto are the latest daily bar on the as-of date, not an official fix. Treasury is the par curve and can update after the equity close; FRED is only the fallback and is often a day behind.

## Sources

- Yahoo Finance chart, no key: `query1.finance.yahoo.com/v8/finance/chart`
- US Treasury daily par yield curve CSV, no key
- currency-api daily USD file, no key, for USD/CNH spot only (Yahoo has no history on that pair)
- FRED graph CSV (`DGS2`, `DGS10`, `DGS30`), no key, fallback only
- CoinGecko market chart, no key, crypto fallback only
- CME `CNH=F` via Yahoo, only if the USD/CNH spot file fails

Stooq was checked and rejected: it returns a bot wall instead of a CSV.

Type is JetBrains Mono, SIL Open Font License, in `site/fonts/`.

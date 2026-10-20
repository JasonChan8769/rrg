# RRG – Relative Rotation Graph

A phone-friendly Relative Rotation Graph for sector, industry, country and factor ETFs. It has tails, play, step forward and back, and a date slider. It runs entirely on free GitHub hosting, with no app store and no login.

**Open the app:** https://jasonchan8769.github.io/rrg/

## Put it on your phone's home screen
- **iPhone (Safari):** open the link, tap the Share button, then **Add to Home Screen**.
- **Android (Chrome):** open the link, tap the ⋮ menu, then **Add to Home screen** (or **Install app**).

It then opens full screen like an app. It also works offline, using the last prices it loaded.

## Where the prices come from
- Yahoo Finance daily closes (split-adjusted, not dividend-adjusted), with Stooq as a backup for US tickers.
- A scheduled GitHub job (`.github/workflows/update.yml`) refreshes them about every 30 minutes while US markets are open (Mon–Fri, 9:30–16:00 New York), and runs a full refresh after the close.
- GitHub's timer can run a few minutes late at busy times.

## Add a ticker
About 115 ETFs are built in (see `scripts/universe.json`). To add others, edit **`tickers.txt`**: one ticker per line, in Yahoo Finance format (`NVDA`, `0700.HK`, `BTC-USD`). Save it, wait a few minutes, then press Refresh in the app. You can edit the file from your phone's browser on github.com.

## Run an update by hand
Go to the **Actions** tab, open **Update prices and publish**, then press **Run workflow**.

## How the RRG is calculated
RS = price ÷ benchmark. RS-Ratio = 100 × SMA(RS, 10) ÷ SMA(RS, 30). RS-Momentum = 100 × RS-Ratio ÷ SMA(RS-Ratio, 9). This matches StockCharts' RRG values when given the same prices.

Not investment advice.

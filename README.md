# 🎲 Gambles: Kalshi paper trading

A localhost dashboard for **simulating** bets on live [Kalshi](https://kalshi.com) prediction markets.
Think stock-market simulator, but with real Kalshi odds. No real money is involved: the app only
reads public market data and never places orders.

## Features

- **Available Bets**: every 2026 midterm race on Kalshi (Senate, House, Governor, plus House and Senate
  control), with live bid/ask, implied probability and volume. An **All Kalshi** view pages through
  every open event, and you can look up any market by ticker.
- **Paper betting**: buy YES or NO by dollars or by contracts. Orders fill at the live ask, and the
  preview shows cost, Kalshi's fee, payout if the bet wins, and the loss if it doesn't.
- **Active Bets**: positions valued at the bid/ask mid-price, with unrealized P/L, the payout if each
  bet wins, and one-click early **Sell** at the live bid.
- **Price charts everywhere**: click any active or past bet, or the 📈 button on any market, to see its
  YES and NO price lines over 1D / 1W / 1M / All, from Kalshi's price history. Charts mark your entry
  and exit prices and when you placed and closed the bet. Kalshi's API doesn't expose market colors,
  so Democrat markets draw YES in blue and NO in red, and Republican markets the reverse. Other markets
  get a color pair seeded from their ticker, so each market keeps the same colors.
- **History & Breakdown**: wins, losses and early sales, win rate, gross winnings and losses, biggest
  win and loss, and P/L split by race type, party and category.
- **Automatic settlement**: a background loop refreshes prices every 60 seconds. When Kalshi finalizes
  a market, open bets settle at $1 or $0 and cash is credited.
- **Equity chart**: portfolio value sampled over time.
- **Multiple portfolios**: create as many as you like, each with its own bankroll.

### Built-in scenarios: 2026 midterms

On first run the app scans Kalshi for every 2026 race and builds **16 portfolios**. Each one buys YES
on one party's side of every race, $100 per race:

| | All races | Only races whose side *currently* has ≥5% odds |
|---|---|---|
| **Bought now** (live ask) | `all-dem`, `all-gop` | `all-dem-5pct`, `all-gop-5pct` |
| **1 week ago** | `all-dem-7d`, `all-gop-7d` | `all-dem-7d-5pct`, `all-gop-7d-5pct` |
| **2 weeks ago** | `all-dem-14d`, `all-gop-14d` | `all-dem-14d-5pct`, `all-gop-14d-5pct` |
| **1 month ago** | `all-dem-30d`, `all-gop-30d` | `all-dem-30d-5pct`, `all-gop-30d-5pct` |

**Backdated** portfolios fill at the ask Kalshi actually showed at that moment, from hourly price
history, with Kalshi's fee at that price. Each bet is stamped with the past date. A race whose market
wasn't listed yet, or had no offers then, is skipped and the reason is recorded. Each backdated
portfolio's equity curve is backfilled from the same history, so its chart starts on the purchase date.
All 16 share one history fetch, which takes about a minute.

The **Dem vs GOP** tab shows every variant in one grid. Click a row to compare that Dem/GOP pair side by
side, race by race, with a P/L-over-time chart. You can re-run the "bought now" scenarios at current
prices, with a custom stake, from the **Portfolios** tab. Any scenario can be rebuilt with
`POST /api/scenarios/{slug}/reset`.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
./run.sh            # or: uv run python -m app.main
```

Open **http://127.0.0.1:8000**. The first launch spends about a minute scanning Kalshi's election
series, then the scenarios appear.

### Optional: Kalshi API key

Not required, because Kalshi's market data is public. A key only gives you a higher rate limit.
To use one, copy `.env.example` to `.env` and set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`.
Requests are then signed with RSA-PSS per Kalshi's API docs. The app never calls any order endpoint.

## How the simulation works

| | |
|---|---|
| **Buy** | Fills at the current **ask** for the chosen side (YES ask or NO ask) |
| **Sell early** | Fills at the current **bid** for that side |
| **Fees** | Kalshi's taker fee `ceil_to_cent(0.07 × contracts × P × (1 − P))` on entry and early exit, none at settlement. Disable with `GAMBLES_SIMULATE_FEES=0` |
| **Valuation** | Open positions are marked at the bid/ask mid. Once a market has a result, they're worth $1 or $0 |
| **Settlement** | When Kalshi reports `result = yes/no`, winning contracts pay $1.00. Voided markets refund at cost |

A fresh scenario starts slightly negative. That's the bid/ask spread plus fees: the cost of crossing
the spread, just as with a real order.

Simplifications: orders always fill in full at the top of the book (order-book depth is ignored), and
the simulator never moves the market.

## Project layout

```
app/
  main.py        FastAPI app, REST API, background refresh/settlement loop
  kalshi.py      Read-only Kalshi v2 client (rate limiting, retries, optional request signing)
  catalog.py     Market normalization and 2026 midterm race discovery
  sim.py         Paper-trading engine: fills, fees, settlement, P/L, summaries
  scenarios.py   All-Dem / All-GOP scenario grid (timing x odds filter) and seeding
  backtest.py    Backdated fills from Kalshi hourly history, equity-curve backfill
  db.py          SQLite schema and helpers (data/gambles.db)
  static/        Dashboard (plain HTML/CSS/JS, no build step)
tests/           pytest suite (engine, catalog parsing, scenarios, settlement loop)
```

## Development

```bash
uv run pytest
```

All state lives in `data/gambles.db`. Delete it to start over.

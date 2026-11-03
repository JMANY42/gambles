"""Backdated ("what if I'd bought N days ago") support.

Kalshi's hourly candlesticks record the YES bid/ask at the end of every hour, so a
backdated buy fills at the actual ask Kalshi showed at that time. The same history
is used to backfill each backdated portfolio's equity curve.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from . import catalog, sim
from .kalshi import KalshiClient

log = logging.getLogger(__name__)

HOUR = 3600
DAY = 86_400
MAX_QUOTE_STALENESS = 12 * HOUR   # a historical quote older than this is not a fill
SNAPSHOT_STEP = 6 * HOUR          # backfilled equity-curve resolution

History = dict[str, list[dict[str, Any]]]  # ticker -> points from catalog.candles_to_points


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def fetch_history(client: KalshiClient, tickers: list[str], days: int) -> History:
    """Hourly YES history for each ticker over the last `days` days (+ a little slack)."""
    end = int(time.time())
    start = end - days * DAY - MAX_QUOTE_STALENESS
    out: History = {}
    for i, ticker in enumerate(tickers, 1):
        try:
            series = await catalog.series_for_market(client, ticker)
            if not series:
                continue
            candles = await client.candlesticks(series, ticker, start, end, 60)
            out[ticker] = catalog.candles_to_points(candles)
        except Exception as exc:  # one bad market shouldn't sink the backtest
            log.warning("No price history for %s: %s", ticker, exc)
        if i % 50 == 0:
            log.info("Backtest history: %d/%d markets", i, len(tickers))
    return out


def _at_or_before(points: list[dict[str, Any]], ts: float) -> list[dict[str, Any]]:
    return [p for p in points if p["t"] <= ts]


def quote_at(points: list[dict[str, Any]], ts: float) -> float | None:
    """The YES ask in effect at `ts`, or None if the market wasn't listed / had no offers."""
    for p in reversed(_at_or_before(points, ts)):
        if ts - p["t"] > MAX_QUOTE_STALENESS:
            return None
        ask = p.get("yes_ask")
        if ask is not None and 0 < ask < 1:
            return ask
    return None


def yes_mark_at(points: list[dict[str, Any]], ts: float) -> float | None:
    """Fair YES value at `ts` (bid/ask mid, else last trade), matching sim.mark_price."""
    prior = _at_or_before(points, ts)
    if not prior:
        return None
    p = prior[-1]
    bid, ask = p.get("yes_bid"), p.get("yes_ask")
    if bid and ask and 0 < ask < 1:
        return round((bid + ask) / 2, 4)
    return p["yes"]


def backfill_snapshots(conn: sqlite3.Connection, portfolio_id: int, history: History,
                       start_ts: float, end_ts: float) -> int:
    """Write equity snapshots from start_ts to end_ts for a freshly backdated portfolio
    (all bets placed at start_ts, none closed since)."""
    pf = sim.get_portfolio(conn, portfolio_id)
    bets = conn.execute("SELECT market_ticker, side, contracts, entry_price FROM bets "
                        "WHERE portfolio_id = ? AND status = 'open'", (portfolio_id,)).fetchall()
    n = 0
    t = start_ts
    while t < end_ts:
        value = 0.0
        for b in bets:
            y = yes_mark_at(history.get(b["market_ticker"], []), t)
            px = b["entry_price"] if y is None else (y if b["side"] == "yes" else 1 - y)
            value += b["contracts"] * px
        value = sim.cents(value)
        conn.execute(
            "INSERT INTO snapshots(portfolio_id, ts, cash, positions_value, equity) VALUES (?,?,?,?,?)",
            (portfolio_id, iso(t), pf["cash"], value, sim.cents(pf["cash"] + value)),
        )
        n += 1
        t += SNAPSHOT_STEP
    return n

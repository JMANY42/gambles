"""Paper-trading engine.

Model (mirrors how a real Kalshi order would fill, simplified):
  * Buying fills at the current ask for the chosen side (YES ask / NO ask).
  * Selling early fills at the current bid for that side.
  * Every contract settles at $1.00 if its side wins, $0.00 otherwise.
  * Kalshi's taker trading fee: ceil_to_cent(0.07 * contracts * P * (1 - P)),
    charged on entry and on early exit; none at settlement.
  * Open positions are marked at the bid/ask mid (a fair-value estimate);
    if the market isn't trading, at the last traded price.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .config import settings

FEE_RATE = 0.07


class BetError(ValueError):
    """Raised when a simulated order can't be filled."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cents(x: float) -> float:
    return round(x + 0.0, 2)


def kalshi_fee(contracts: int, price: float, rate: float = FEE_RATE) -> float:
    if not settings.simulate_fees or contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = rate * contracts * price * (1 - price)
    return math.ceil(round(raw * 100, 6)) / 100


def side_quote(market: dict[str, Any], side: str) -> tuple[float | None, float | None]:
    """(bid, ask) for the given side, None when there is no quote."""
    bid = market.get(f"{side}_bid")
    ask = market.get(f"{side}_ask")
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and 0 < ask < 1 else None
    return bid, ask


def mark_price(market: dict[str, Any] | None, side: str) -> float | None:
    """Fair value per contract for an open position: the bid/ask mid while the
    market trades (falls back to the last trade), $1/$0 once it has a result."""
    if not market:
        return None
    result = market.get("result")
    if result in ("yes", "no"):
        return 1.0 if result == side else 0.0
    bid, ask = side_quote(market, side)
    if market.get("status") == "active" and bid is not None and ask is not None:
        return round((bid + ask) / 2, 4)
    last = market.get("last_price")
    if last is None:
        return bid
    return last if side == "yes" else round(1 - last, 4)


def is_tradeable(market: dict[str, Any]) -> bool:
    return market.get("status") == "active" and not market.get("result")


def contracts_for_budget(budget: float, price: float) -> int:
    """Max whole contracts such that cost + entry fee <= budget."""
    n = int(budget // price)
    while n > 0 and n * price + kalshi_fee(n, price) > budget + 1e-9:
        n -= 1
    return n


# --- portfolios ---------------------------------------------------------------

def create_portfolio(conn: sqlite3.Connection, slug: str, name: str, description: str = "",
                     starting_balance: float | None = None) -> int:
    bal = cents(starting_balance if starting_balance is not None else settings.starting_balance)
    cur = conn.execute(
        "INSERT INTO portfolios(slug, name, description, starting_balance, cash) VALUES (?,?,?,?,?)",
        (slug, name, description, bal, bal),
    )
    return int(cur.lastrowid)


def get_portfolio(conn: sqlite3.Connection, portfolio_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    if not row:
        raise BetError(f"Portfolio {portfolio_id} not found")
    return dict(row)


# --- orders -------------------------------------------------------------------

def place_bet(conn: sqlite3.Connection, portfolio_id: int, market: dict[str, Any], side: str,
              contracts: int | None = None, dollars: float | None = None,
              note: str | None = None) -> dict[str, Any]:
    side = side.lower()
    if side not in ("yes", "no"):
        raise BetError("side must be 'yes' or 'no'")
    if not is_tradeable(market):
        raise BetError(f"{market['ticker']} is not open for trading (status={market.get('status')})")
    _, ask = side_quote(market, side)
    if ask is None:
        raise BetError(f"No {side.upper()} offers available on {market['ticker']}")

    if contracts is None:
        if not dollars or dollars <= 0:
            raise BetError("Provide a positive number of contracts or a dollar amount")
        contracts = contracts_for_budget(dollars, ask)
    contracts = int(contracts)
    if contracts <= 0:
        raise BetError(f"Amount too small to buy one contract at ${ask:.4f}")

    cost = cents(contracts * ask)
    fee = kalshi_fee(contracts, ask)
    pf = get_portfolio(conn, portfolio_id)
    if cost + fee > pf["cash"] + 1e-9:
        raise BetError(f"Insufficient cash: need ${cost + fee:,.2f}, have ${pf['cash']:,.2f}")

    conn.execute("UPDATE portfolios SET cash = ROUND(cash - ?, 2) WHERE id = ?", (cost + fee, portfolio_id))
    cur = conn.execute(
        "INSERT INTO bets(portfolio_id, market_ticker, side, contracts, entry_price, cost, entry_fee, note, placed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (portfolio_id, market["ticker"], side, contracts, ask, cost, fee, note, now_iso()),
    )
    return dict(conn.execute("SELECT * FROM bets WHERE id = ?", (cur.lastrowid,)).fetchone())


def sell_bet(conn: sqlite3.Connection, bet_id: int, market: dict[str, Any]) -> dict[str, Any]:
    bet = conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not bet:
        raise BetError(f"Bet {bet_id} not found")
    if bet["status"] != "open":
        raise BetError(f"Bet {bet_id} is already {bet['status']}")
    if not is_tradeable(market):
        raise BetError("Market is not trading; wait for settlement")
    bid, _ = side_quote(market, bet["side"])
    if bid is None:
        raise BetError(f"No {bet['side'].upper()} bids to sell into right now")
    proceeds = cents(bet["contracts"] * bid)
    fee = kalshi_fee(bet["contracts"], bid)
    pnl = cents(proceeds - fee - bet["cost"] - bet["entry_fee"])
    conn.execute(
        "UPDATE bets SET status='sold', exit_price=?, exit_fee=?, proceeds=?, pnl=?, closed_at=? WHERE id=?",
        (bid, fee, proceeds, pnl, now_iso(), bet_id),
    )
    conn.execute("UPDATE portfolios SET cash = ROUND(cash + ?, 2) WHERE id = ?",
                 (proceeds - fee, bet["portfolio_id"]))
    return dict(conn.execute("SELECT * FROM bets WHERE id = ?", (bet_id,)).fetchone())


def settle_market(conn: sqlite3.Connection, market: dict[str, Any]) -> int:
    """Settle all open bets on a market that has a final result. Returns count settled."""
    result = market.get("result")
    if not result:
        return 0
    open_bets = conn.execute(
        "SELECT * FROM bets WHERE market_ticker = ? AND status = 'open'", (market["ticker"],)
    ).fetchall()
    for bet in open_bets:
        if result in ("yes", "no"):
            won = bet["side"] == result
            proceeds = float(bet["contracts"]) if won else 0.0
            status, exit_price = ("won", 1.0) if won else ("lost", 0.0)
        else:  # voided / cancelled market: refund at entry
            proceeds, status, exit_price = bet["cost"] + bet["entry_fee"], "void", bet["entry_price"]
        pnl = cents(proceeds - bet["cost"] - bet["entry_fee"])
        conn.execute(
            "UPDATE bets SET status=?, exit_price=?, exit_fee=0, proceeds=?, pnl=?, closed_at=? WHERE id=?",
            (status, exit_price, cents(proceeds), pnl, now_iso(), bet["id"]),
        )
        conn.execute("UPDATE portfolios SET cash = ROUND(cash + ?, 2) WHERE id = ?",
                     (proceeds, bet["portfolio_id"]))
    return len(open_bets)


# --- reporting ----------------------------------------------------------------

_BET_SELECT = """
SELECT b.*, m.event_title, m.title AS market_title, m.yes_sub_title, m.chamber, m.state,
       m.district, m.party, m.status AS market_status, m.result AS market_result,
       m.yes_bid, m.yes_ask, m.no_bid, m.no_ask, m.last_price, m.close_time, m.category
FROM bets b LEFT JOIN markets m ON m.ticker = b.market_ticker
"""


def _enrich_open(bet: dict[str, Any]) -> dict[str, Any]:
    market = {k: bet.get(k) for k in ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "result")}
    market["status"] = bet.get("market_status")
    mp = mark_price(market, bet["side"])
    bet["mark_price"] = mp
    bet["market_value"] = cents(bet["contracts"] * mp) if mp is not None else None
    basis = bet["cost"] + bet["entry_fee"]
    bet["unrealized_pnl"] = cents(bet["market_value"] - basis) if mp is not None else None
    bet["max_payout"] = float(bet["contracts"])
    bet["max_profit"] = cents(bet["contracts"] - basis)
    bet["implied_prob"] = bet["entry_price"]
    return bet


def list_bets(conn: sqlite3.Connection, portfolio_id: int, status: str = "open") -> list[dict[str, Any]]:
    if status == "open":
        where, order = "b.status = 'open'", "b.placed_at DESC, b.id DESC"
    else:
        where, order = "b.status != 'open'", "b.closed_at DESC, b.id DESC"
    out = [dict(r) for r in conn.execute(
        f"{_BET_SELECT} WHERE b.portfolio_id = ? AND {where} ORDER BY {order}", (portfolio_id,))]
    return [_enrich_open(b) for b in out] if status == "open" else out


def _group(bets: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for b in bets:
        k = b.get(key) or "Other"
        g = groups.setdefault(k, {"group": k, "bets": 0, "won": 0, "lost": 0, "sold": 0,
                                  "staked": 0.0, "pnl": 0.0, "open": 0, "unrealized": 0.0})
        g["bets"] += 1
        g["staked"] += b["cost"] + b["entry_fee"]
        if b["status"] == "open":
            g["open"] += 1
            g["unrealized"] += b.get("unrealized_pnl") or 0
        else:
            g[b["status"]] = g.get(b["status"], 0) + 1
            g["pnl"] += b.get("pnl") or 0
    for g in groups.values():
        g["staked"], g["pnl"], g["unrealized"] = cents(g["staked"]), cents(g["pnl"]), cents(g["unrealized"])
    return sorted(groups.values(), key=lambda g: -g["staked"])


def portfolio_summary(conn: sqlite3.Connection, portfolio_id: int) -> dict[str, Any]:
    pf = get_portfolio(conn, portfolio_id)
    open_bets = list_bets(conn, portfolio_id, "open")
    closed = list_bets(conn, portfolio_id, "closed")

    positions_value = cents(sum(b["market_value"] or 0 for b in open_bets))
    open_basis = cents(sum(b["cost"] + b["entry_fee"] for b in open_bets))
    unrealized = cents(positions_value - open_basis)
    realized = cents(sum(b["pnl"] or 0 for b in closed))
    fees = cents(sum(b["entry_fee"] + (b["exit_fee"] or 0) for b in open_bets + closed))
    equity = cents(pf["cash"] + positions_value)
    won = [b for b in closed if b["status"] == "won"]
    lost = [b for b in closed if b["status"] == "lost"]
    sold = [b for b in closed if b["status"] == "sold"]
    decided = len(won) + len(lost)
    profitable_sales = [b for b in sold if (b["pnl"] or 0) > 0]

    everything = open_bets + closed
    return {
        "portfolio": pf,
        "cash": pf["cash"],
        "starting_balance": pf["starting_balance"],
        "positions_value": positions_value,
        "open_cost_basis": open_basis,
        "equity": equity,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized,
        "total_pnl": cents(equity - pf["starting_balance"]),
        "total_return_pct": round((equity / pf["starting_balance"] - 1) * 100, 3) if pf["starting_balance"] else 0,
        "fees_paid": fees,
        "open_count": len(open_bets),
        "max_payout_if_all_win": cents(sum(b["contracts"] for b in open_bets)),
        "counts": {"won": len(won), "lost": len(lost), "sold": len(sold),
                   "void": sum(1 for b in closed if b["status"] == "void")},
        "win_rate_pct": round(len(won) / decided * 100, 1) if decided else None,
        "sold_profitably": len(profitable_sales),
        "gross_winnings": cents(sum(b["pnl"] for b in closed if (b["pnl"] or 0) > 0)),
        "gross_losses": cents(sum(b["pnl"] for b in closed if (b["pnl"] or 0) < 0)),
        "biggest_win": max(closed, key=lambda b: b["pnl"] or 0, default=None),
        "biggest_loss": min(closed, key=lambda b: b["pnl"] or 0, default=None),
        "by_chamber": _group(everything, "chamber"),
        "by_party": _group(everything, "party"),
        "by_category": _group(everything, "category"),
    }


def record_snapshot(conn: sqlite3.Connection, portfolio_id: int) -> dict[str, Any]:
    s = portfolio_summary(conn, portfolio_id)
    conn.execute(
        "INSERT INTO snapshots(portfolio_id, ts, cash, positions_value, equity) VALUES (?,?,?,?,?)",
        (portfolio_id, now_iso(), s["cash"], s["positions_value"], s["equity"]),
    )
    return s

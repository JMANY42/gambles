"""Built-in scenarios: bet YES on every Democrat / every Republican in the 2026 midterms.

Each party has a grid of variants:
  * timing: bought now, 1 week ago, 2 weeks ago, or 1 month ago (backdated fills at
    Kalshi's historical ask, see backtest.py)
  * filter: every race, or only races whose YES side *currently* has >= 5% odds
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from . import backtest, db, sim
from .config import settings
from .kalshi import KalshiClient

log = logging.getLogger(__name__)

MIN_PROB = 0.05
PARTIES = {"dem": ("D", "Democrats", "Democratic"), "gop": ("R", "Republicans", "Republican")}
TIMINGS = {0: "now", 7: "1 week ago", 14: "2 weeks ago", 30: "1 month ago"}


@dataclass(frozen=True)
class Scenario:
    slug: str
    party: str           # D / R
    party_plural: str    # Democrats
    party_adj: str       # Democratic
    days_ago: int
    min_prob: float | None

    @property
    def name(self) -> str:
        if self.days_ago == 0 and self.min_prob is None:
            return f"All {self.party_plural} — 2026 Midterms"  # the original two scenarios
        name = f"All {self.party_plural} — bought {TIMINGS[self.days_ago]}"
        return name + (f", ≥{self.min_prob:.0%} odds only" if self.min_prob else "")

    def description(self, stake: float) -> str:
        when = "at the live ask" if not self.days_ago else \
            f"at the ask Kalshi showed {TIMINGS[self.days_ago]} (backdated)"
        filt = (f" Skips races where the {self.party_adj} side currently has under "
                f"{self.min_prob:.0%} odds.") if self.min_prob else ""
        return (f"Buys YES on the {self.party_adj} side of every 2026 Senate, House, Governor and "
                f"chamber-control market on Kalshi, ${stake:.0f} per race {when}.{filt}")


def variant_suffix(days_ago: int, filtered: bool) -> str:
    return (f"-{days_ago}d" if days_ago else "") + ("-5pct" if filtered else "")


def _build() -> dict[str, Scenario]:
    out = {}
    for key, (party, plural, adj) in PARTIES.items():
        for days in TIMINGS:
            for filtered in (False, True):
                slug = f"all-{key}{variant_suffix(days, filtered)}"
                out[slug] = Scenario(slug, party, plural, adj, days, MIN_PROB if filtered else None)
    return out


SCENARIOS = _build()


def current_yes_prob(m: dict[str, Any]) -> float | None:
    """The market's current chance of paying out on YES (mid, else last trade)."""
    return sim.mark_price(m, "yes")


def _pick_party_markets(conn, party: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One market per race for the given party. Returns (picks, skipped_races)."""
    markets = db.rows(conn, "SELECT * FROM markets WHERE is_midterm = 1 ORDER BY chamber, state, district")
    by_event: dict[str, list[dict[str, Any]]] = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)

    picks, skipped = [], []
    for event_ticker, ms in by_event.items():
        cands = [m for m in ms if m["party"] == party]
        # Prefer the canonical "-D"/"-R" party market over candidate-specific ones.
        cands.sort(key=lambda m: (not m["ticker"].endswith(f"-{party}"), m["ticker"]))
        first = ms[0]
        label = first["event_title"] or event_ticker
        if not cands:
            skipped.append({"event_ticker": event_ticker, "race": label, "reason": f"no {party} market"})
            continue
        m = cands[0]
        if not sim.is_tradeable(m):
            skipped.append({"event_ticker": event_ticker, "race": label, "reason": f"market {m['status']}"})
            continue
        if sim.side_quote(m, "yes")[1] is None:
            skipped.append({"event_ticker": event_ticker, "race": label, "reason": "no YES offers"})
            continue
        picks.append(m)
    return picks, skipped


def existing_slugs() -> set[str]:
    with db.tx() as conn:
        return {r["slug"] for r in conn.execute("SELECT slug FROM portfolios")}


def history_tickers() -> list[str]:
    """Markets a backdated scenario might buy (both parties), for one shared history fetch."""
    with db.tx() as conn:
        tickers = set()
        for party in ("D", "R"):
            picks, _ = _pick_party_markets(conn, party)
            tickers.update(m["ticker"] for m in picks)
    return sorted(tickers)


def seed_scenario(slug: str, stake: float | None = None, reset: bool = False,
                  history: backtest.History | None = None, now_ts: float | None = None) -> dict[str, Any]:
    sc = SCENARIOS[slug]
    stake = stake or settings.scenario_stake
    now_ts = now_ts or time.time()
    if sc.days_ago and history is None:
        raise sim.BetError(f"{slug} is backdated and needs price history")
    buy_ts = now_ts - sc.days_ago * backtest.DAY

    with db.tx() as conn:
        existing = conn.execute("SELECT id FROM portfolios WHERE slug = ?", (slug,)).fetchone()
        if existing and not reset:
            return {"slug": slug, "status": "exists", "portfolio_id": existing["id"]}
        if existing:
            conn.execute("DELETE FROM portfolios WHERE id = ?", (existing["id"],))

        picks, skipped = _pick_party_markets(conn, sc.party)
        if not picks:
            raise sim.BetError("No midterm markets cached yet; refresh the catalog first.")
        if sc.min_prob:
            kept = []
            for m in picks:
                p = current_yes_prob(m)
                if p is None or p < sc.min_prob:
                    skipped.append({"event_ticker": m["event_ticker"], "race": m["event_title"],
                                    "reason": f"current odds {(p or 0):.1%} < {sc.min_prob:.0%}"})
                else:
                    kept.append(m)
            picks = kept

        # Same bankroll rule for every variant so their returns are comparable.
        bankroll = max(settings.starting_balance, len(picks) * stake * 1.1)
        pid = sim.create_portfolio(conn, slug, sc.name, sc.description(stake),
                                   starting_balance=round(bankroll, -2))
        placed = 0
        for m in picks:
            market, placed_at = m, None
            if sc.days_ago:
                ask = backtest.quote_at(history.get(m["ticker"], []), buy_ts)
                if ask is None:
                    skipped.append({"event_ticker": m["event_ticker"], "race": m["event_title"],
                                    "reason": f"no YES offers {TIMINGS[sc.days_ago]} (not listed yet or no quotes)"})
                    continue
                market = {**m, "yes_ask": ask, "status": "active", "result": None}
                placed_at = backtest.iso(buy_ts)
            try:
                sim.place_bet(conn, pid, market, "yes", dollars=stake, note=f"scenario:{slug}",
                              placed_at=placed_at)
                placed += 1
            except sim.BetError as exc:
                skipped.append({"event_ticker": m["event_ticker"], "race": m["event_title"], "reason": str(exc)})
        if sc.days_ago:
            backtest.backfill_snapshots(conn, pid, history, buy_ts, now_ts)
        sim.record_snapshot(conn, pid)

    result = {"slug": slug, "status": "seeded", "portfolio_id": pid, "bets_placed": placed,
              "skipped": skipped, "stake": stake, "days_ago": sc.days_ago, "min_prob": sc.min_prob,
              "placed_at": backtest.iso(buy_ts)}
    db.set_meta(f"scenario:{slug}", result)
    log.info("Seeded %s: %d bets, %d skipped", slug, placed, len(skipped))
    return result


async def seed_all(client: KalshiClient, reset: bool = False,
                   slugs: list[str] | None = None, stake: float | None = None) -> list[dict[str, Any]]:
    """Create missing scenario portfolios (or re-create `slugs` when reset=True).
    Backdated ones share a single hourly-history fetch."""
    have = existing_slugs()
    todo = slugs or [s for s in SCENARIOS if reset or s not in have]
    if not todo:
        return []
    history: backtest.History | None = None
    longest = max(SCENARIOS[s].days_ago for s in todo)
    if longest:
        tickers = history_tickers()
        log.info("Fetching %d days of hourly history for %d markets…", longest, len(tickers))
        history = await backtest.fetch_history(client, tickers, longest)
    now_ts = time.time()  # one clock for the whole batch so variants line up
    return [await asyncio.to_thread(seed_scenario, s, stake, reset, history, now_ts) for s in todo]

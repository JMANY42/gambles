"""Built-in scenarios: bet YES on every Democrat / every Republican in the 2026 midterms."""
from __future__ import annotations

import logging
from typing import Any

from . import db, sim
from .config import settings

log = logging.getLogger(__name__)

SCENARIOS = {
    "all-dem": {
        "party": "D",
        "name": "All Democrats — 2026 Midterms",
        "description": "Buys YES on the Democratic side of every 2026 Senate, House, Governor and "
                       "chamber-control market on Kalshi, ${stake:.0f} per race at the live ask.",
    },
    "all-gop": {
        "party": "R",
        "name": "All Republicans — 2026 Midterms",
        "description": "Buys YES on the Republican side of every 2026 Senate, House, Governor and "
                       "chamber-control market on Kalshi, ${stake:.0f} per race at the live ask.",
    },
}


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


def seed_scenario(slug: str, stake: float | None = None, reset: bool = False) -> dict[str, Any]:
    spec = SCENARIOS[slug]
    stake = stake or settings.scenario_stake
    with db.tx() as conn:
        existing = conn.execute("SELECT id FROM portfolios WHERE slug = ?", (slug,)).fetchone()
        if existing and not reset:
            return {"slug": slug, "status": "exists", "portfolio_id": existing["id"]}
        if existing:
            conn.execute("DELETE FROM portfolios WHERE id = ?", (existing["id"],))

        picks, skipped = _pick_party_markets(conn, spec["party"])
        if not picks:
            raise sim.BetError("No midterm markets cached yet; refresh the catalog first.")
        # Bankroll: the configured default, or enough to cover every race, whichever is larger.
        bankroll = max(settings.starting_balance, len(picks) * stake * 1.1)
        pid = sim.create_portfolio(conn, slug, spec["name"], spec["description"].format(stake=stake),
                                   starting_balance=round(bankroll, -2))
        placed = 0
        for m in picks:
            try:
                sim.place_bet(conn, pid, m, "yes", dollars=stake, note=f"scenario:{slug}")
                placed += 1
            except sim.BetError as exc:
                skipped.append({"event_ticker": m["event_ticker"], "race": m["event_title"], "reason": str(exc)})
        sim.record_snapshot(conn, pid)

    result = {"slug": slug, "status": "seeded", "portfolio_id": pid, "bets_placed": placed,
              "skipped": skipped, "stake": stake}
    db.set_meta(f"scenario:{slug}", result)
    log.info("Seeded %s: %d bets, %d skipped", slug, placed, len(skipped))
    return result


def seed_all(reset: bool = False) -> list[dict[str, Any]]:
    return [seed_scenario(slug, reset=reset) for slug in SCENARIOS]

"""Backdated scenarios: historical fills, the >=5% filter, and equity backfill."""
import asyncio
import time

import pytest

from app import backtest, catalog, db, scenarios

DAY, HOUR = backtest.DAY, backtest.HOUR
# Hour-aligned "now" near the real clock: the live snapshot written at the end of
# seeding uses the real time, so backfilled snapshots must precede it.
NOW = int(time.time()) // HOUR * HOUR


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "bt.db")
    db.init_db()
    yield
    db.set_db_path(None)


def pt(t, bid, ask, yes=None):
    return {"t": t, "yes": yes if yes is not None else bid, "yes_bid": bid, "yes_ask": ask, "volume": 1}


def market(ticker, party, bid, ask, event):
    return catalog.normalize_market(
        {"ticker": ticker, "event_ticker": event, "status": "active", "result": "",
         "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
         "no_bid_dollars": str(round(1 - ask, 4)), "no_ask_dollars": str(round(1 - bid, 4)),
         "last_price_dollars": str(bid)},
        {"title": event, "series_ticker": event.split("-")[0]},
        {"chamber": "Senate", "state": event[6:8], "district": None},
    )


# --- quote lookup ---

def test_quote_at_uses_latest_ask_at_or_before_time():
    pts = [pt(NOW - 10 * HOUR, 0.30, 0.32), pt(NOW - 5 * HOUR, 0.40, 0.42), pt(NOW + HOUR, 0.9, 0.95)]
    assert backtest.quote_at(pts, NOW) == 0.42          # never peeks into the future
    assert backtest.quote_at(pts, NOW - 6 * HOUR) == 0.32


def test_quote_at_rejects_unlisted_stale_or_empty_books():
    assert backtest.quote_at([pt(NOW + HOUR, 0.4, 0.42)], NOW) is None           # not listed yet
    assert backtest.quote_at([pt(NOW - 2 * DAY, 0.4, 0.42)], NOW) is None        # too stale
    assert backtest.quote_at([pt(NOW - HOUR, 0.0, 1.0)], NOW) is None            # no offers
    # falls back past an empty-book hour to the last real offer
    assert backtest.quote_at([pt(NOW - 3 * HOUR, 0.4, 0.42), pt(NOW - HOUR, 0, 0)], NOW) == 0.42


def test_yes_mark_at_prefers_mid():
    assert backtest.yes_mark_at([pt(NOW - HOUR, 0.40, 0.44)], NOW) == 0.42
    assert backtest.yes_mark_at([pt(NOW - HOUR, 0, 0, yes=0.5)], NOW) == 0.5
    assert backtest.yes_mark_at([pt(NOW + HOUR, 0.4, 0.44)], NOW) is None


# --- scenario grid ---

def test_scenario_grid_slugs_and_names():
    assert len(scenarios.SCENARIOS) == 16
    sc = scenarios.SCENARIOS["all-gop-14d-5pct"]
    assert (sc.party, sc.days_ago, sc.min_prob) == ("R", 14, 0.05)
    assert sc.name == "All Republicans — bought 2 weeks ago, ≥5% odds only"
    assert scenarios.SCENARIOS["all-dem"].name == "All Democrats — 2026 Midterms"
    assert scenarios.variant_suffix(30, False) == "-30d"


def _seed_markets():
    # D currently 60%, R currently 3% (below the 5% cutoff) in PA; D 2% / R 97% in TN.
    catalog.upsert_markets([
        market("SENATEPA-26-D", "D", 0.59, 0.61, "SENATEPA-26"),
        market("SENATEPA-26-R", "R", 0.02, 0.04, "SENATEPA-26"),
        market("SENATETN-26-D", "D", 0.01, 0.03, "SENATETN-26"),
        market("SENATETN-26-R", "R", 0.96, 0.98, "SENATETN-26"),
    ])
    buy = NOW - 7 * DAY
    return {
        # A week ago PA-D was 40/42 and PA-R 55/57; prices then drift to today's.
        "SENATEPA-26-D": [pt(buy - HOUR, 0.40, 0.42), pt(NOW - DAY, 0.55, 0.57)],
        "SENATEPA-26-R": [pt(buy - HOUR, 0.55, 0.57), pt(NOW - DAY, 0.03, 0.05)],
        "SENATETN-26-D": [pt(buy - HOUR, 0.05, 0.06)],
        # TN-R listed only after the buy time -> must be skipped, not filled at a future price
        "SENATETN-26-R": [pt(buy + DAY, 0.9, 0.92)],
    }


def bets_for(slug):
    with db.tx() as conn:
        return db.rows(conn, "SELECT b.* FROM bets b JOIN portfolios p ON p.id = b.portfolio_id "
                             "WHERE p.slug = ? ORDER BY b.market_ticker", (slug,))


def test_backdated_scenario_fills_at_historical_ask_and_backdates():
    history = _seed_markets()
    res = scenarios.seed_scenario("all-dem-7d", history=history, now_ts=NOW)
    assert res["bets_placed"] == 2
    pa, tn = bets_for("all-dem-7d")
    assert pa["entry_price"] == 0.42 and tn["entry_price"] == 0.06
    assert pa["placed_at"] == backtest.iso(NOW - 7 * DAY)

    gop = scenarios.seed_scenario("all-gop-7d", history=history, now_ts=NOW)
    assert gop["bets_placed"] == 1                     # TN-R wasn't listed a week ago
    assert any("not listed yet" in s["reason"] for s in gop["skipped"])


def test_min_prob_filter_uses_current_odds():
    history = _seed_markets()
    dem = scenarios.seed_scenario("all-dem-7d-5pct", history=history, now_ts=NOW)
    # TN-D is 2% now -> excluded even though it was 6% a week ago.
    assert [b["market_ticker"] for b in bets_for("all-dem-7d-5pct")] == ["SENATEPA-26-D"]
    assert any("< 5%" in s["reason"] for s in dem["skipped"])
    # The live "bought now" filtered variant needs no history.
    scenarios.seed_scenario("all-gop-5pct", now_ts=NOW)
    assert [b["market_ticker"] for b in bets_for("all-gop-5pct")] == ["SENATETN-26-R"]


def test_backdated_seed_requires_history():
    _seed_markets()
    with pytest.raises(Exception, match="needs price history"):
        scenarios.seed_scenario("all-dem-30d", now_ts=NOW)


def test_backfill_snapshots_trace_equity_from_buy_time():
    history = _seed_markets()
    res = scenarios.seed_scenario("all-dem-7d", history=history, now_ts=NOW)
    with db.tx() as conn:
        snaps = db.rows(conn, "SELECT * FROM snapshots WHERE portfolio_id = ? ORDER BY ts", (res["portfolio_id"],))
    assert snaps[0]["ts"] == backtest.iso(NOW - 7 * DAY)
    assert len(snaps) == 7 * 24 // 6 + 1               # every 6h, plus the live one
    # First point marks PA-D at its then-mid (0.41) — equity starts just under bankroll.
    first = snaps[0]
    assert first["equity"] == pytest.approx(first["cash"] + first["positions_value"], abs=0.01)


def test_seed_all_fetches_history_once_for_all_backdated(monkeypatch):
    history = _seed_markets()
    calls = []

    async def fake_fetch(client, tickers, days):
        calls.append(days)
        return history

    monkeypatch.setattr(backtest, "fetch_history", fake_fetch)
    out = asyncio.run(scenarios.seed_all(client=None))
    assert calls == [30]                               # one fetch, sized for the longest window
    assert {r["slug"] for r in out} == set(scenarios.SCENARIOS)
    assert scenarios.existing_slugs() == set(scenarios.SCENARIOS)

"""Integration test: background refresh pulls quotes, settles finished markets, snapshots."""
import asyncio

import pytest

from app import catalog, db, main, sim


class FakeClient:
    def __init__(self, markets):
        self.markets = markets
        self.authenticated = False

    async def markets_by_tickers(self, tickers):
        return [m for m in self.markets if m["ticker"] in tickers]


def raw(ticker, bid, ask, status="active", result=""):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "status": status, "result": result,
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}",
            "no_bid_dollars": f"{1 - ask:.4f}", "no_ask_dollars": f"{1 - bid:.4f}",
            "last_price_dollars": f"{bid:.4f}"}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.set_db_path(tmp_path / "loop.db")
    db.init_db()
    yield
    db.set_db_path(None)


def test_refresh_settles_and_snapshots():
    race = {"chamber": "Senate", "state": "PA", "district": None}
    d = catalog.normalize_market(raw("SENATEPA-26-D", 0.60, 0.62), {"title": "PA"}, race)
    r = catalog.normalize_market(raw("SENATEPA-26-R", 0.38, 0.40), {"title": "PA"}, race)
    catalog.upsert_markets([d, r])
    with db.tx() as conn:
        pid = sim.create_portfolio(conn, "x", "X", starting_balance=1000)
        bd = sim.place_bet(conn, pid, d, "yes", contracts=100)
        br = sim.place_bet(conn, pid, r, "yes", contracts=100)

    # Election called: D wins.
    main.state.client = FakeClient([
        raw("SENATEPA-26-D", 0, 1, status="finalized", result="yes"),
        raw("SENATEPA-26-R", 0, 1, status="finalized", result="no"),
    ])
    main.state.last_snapshot = 0
    out = asyncio.run(main.refresh_prices_and_settle())
    assert out["settled"] == 2

    with db.tx() as conn:
        s = sim.portfolio_summary(conn, pid)
        snaps = db.rows(conn, "SELECT * FROM snapshots WHERE portfolio_id = ?", (pid,))
    assert s["counts"] == {"won": 1, "lost": 1, "sold": 0, "void": 0}
    expected = 1000 - (bd["cost"] + bd["entry_fee"]) - (br["cost"] + br["entry_fee"]) + 100
    assert s["cash"] == pytest.approx(expected, abs=0.01)
    assert s["open_count"] == 0 and s["equity"] == s["cash"]
    assert len(snaps) == 1

    # A second refresh doesn't double-pay.
    assert asyncio.run(main.refresh_prices_and_settle())["settled"] == 0


def test_catalog_refresh_endpoint_spawns_task(monkeypatch):
    """Regression: the endpoint must run on the event loop (it calls asyncio.create_task)."""
    from fastapi.testclient import TestClient

    calls = []

    async def fake_refresh(seed=True):
        calls.append(seed)
        return {}

    async def idle_loop():
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "run_catalog_refresh", fake_refresh)
    monkeypatch.setattr(main, "background_loop", idle_loop)
    with TestClient(main.app) as client:
        resp = client.post("/api/refresh/catalog")
        assert resp.status_code == 200 and resp.json()["started"] is True
    assert calls == [True]

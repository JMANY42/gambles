"""FastAPI app: JSON API + static dashboard + background price/settlement loop."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, db, scenarios, sim
from .config import settings
from .kalshi import KalshiClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gambles")
STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self) -> None:
        self.client: KalshiClient | None = None
        self.catalog_task: asyncio.Task | None = None
        self.loop_task: asyncio.Task | None = None
        self.last_price_refresh: str | None = None
        self.last_snapshot = 0.0
        self.last_error: str | None = None
        self.settled_count = 0


state = State()


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _catalog_stale() -> bool:
    meta = db.get_meta("catalog")
    if not meta:
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(meta["refreshed_at"].replace("Z", "+00:00"))
    return age.total_seconds() > settings.catalog_refresh_hours * 3600


async def run_catalog_refresh(seed: bool = True) -> dict[str, Any]:
    assert state.client
    summary = await catalog.discover_midterms(state.client)
    if seed:
        await asyncio.to_thread(scenarios.seed_all)
    return summary


def start_catalog_refresh(seed: bool = True) -> bool:
    if state.catalog_task and not state.catalog_task.done():
        return False

    async def runner() -> None:
        try:
            await run_catalog_refresh(seed)
        except Exception as exc:
            state.last_error = f"catalog: {exc}"
            log.exception("Catalog refresh failed")

    state.catalog_task = asyncio.create_task(runner())
    return True


async def refresh_prices_and_settle() -> dict[str, Any]:
    assert state.client
    with db.tx() as conn:
        bet_tickers = [r["market_ticker"] for r in conn.execute(
            "SELECT DISTINCT market_ticker FROM bets WHERE status = 'open'")]
        midterm = [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM markets WHERE is_midterm = 1 AND result IS NULL")]
    tickers = sorted(set(bet_tickers) | set(midterm))
    n = await catalog.refresh_quotes(state.client, tickers)

    settled = 0
    with db.tx() as conn:
        resolved = db.rows(conn, "SELECT m.* FROM markets m WHERE m.result IS NOT NULL AND m.result != '' "
                                 "AND EXISTS (SELECT 1 FROM bets b WHERE b.market_ticker = m.ticker AND b.status='open')")
        for m in resolved:
            settled += sim.settle_market(conn, m)
    state.settled_count += settled
    state.last_price_refresh = _utcnow()

    if settled or time.monotonic() - state.last_snapshot >= settings.snapshot_minutes * 60:
        with db.tx() as conn:
            for p in conn.execute("SELECT id FROM portfolios").fetchall():
                sim.record_snapshot(conn, p["id"])
        state.last_snapshot = time.monotonic()
    return {"quotes": n, "settled": settled, "at": state.last_price_refresh}


async def background_loop() -> None:
    while True:
        try:
            if _catalog_stale():
                start_catalog_refresh()
            if not (state.catalog_task and not state.catalog_task.done()):
                await refresh_prices_and_settle()
                if (state.last_error or "").startswith("prices"):
                    state.last_error = None
        except Exception as exc:
            state.last_error = f"prices: {exc}"
            log.exception("Price refresh failed")
        await asyncio.sleep(settings.price_refresh_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    state.client = KalshiClient()
    state.loop_task = asyncio.create_task(background_loop())
    yield
    for t in (state.loop_task, state.catalog_task):
        if t:
            t.cancel()
    await state.client.close()


app = FastAPI(title="Gambles — Kalshi paper trading", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# --- status / refresh ---------------------------------------------------------

@app.get("/api/status")
def status() -> dict[str, Any]:
    return {
        "authenticated": bool(state.client and state.client.authenticated),
        "catalog": db.get_meta("catalog"),
        "catalog_refreshing": bool(state.catalog_task and not state.catalog_task.done()),
        "last_price_refresh": state.last_price_refresh,
        "price_refresh_seconds": settings.price_refresh_seconds,
        "simulate_fees": settings.simulate_fees,
        "settled_total": state.settled_count,
        "last_error": state.last_error,
        "server_time": _utcnow(),
    }


@app.post("/api/refresh/catalog")
async def refresh_catalog() -> dict[str, Any]:  # async: must run on the event loop to spawn a task
    return {"started": start_catalog_refresh()}


@app.post("/api/refresh/prices")
async def refresh_prices() -> dict[str, Any]:
    return await refresh_prices_and_settle()


# --- markets ------------------------------------------------------------------

@app.get("/api/midterms")
def midterms() -> dict[str, Any]:
    with db.tx() as conn:
        ms = db.rows(conn, "SELECT * FROM markets WHERE is_midterm = 1 ORDER BY chamber, state, "
                           "CAST(district AS INTEGER), district, party")
    races: dict[str, dict[str, Any]] = {}
    for m in ms:
        r = races.setdefault(m["event_ticker"], {
            "event_ticker": m["event_ticker"], "title": m["event_title"], "chamber": m["chamber"],
            "state": m["state"], "district": m["district"], "markets": []})
        r["markets"].append(m)
    return {"races": list(races.values()), "catalog": db.get_meta("catalog")}


@app.get("/api/kalshi/events")
async def kalshi_events(cursor: str | None = None, limit: int = Query(100, le=200)) -> dict[str, Any]:
    """Live passthrough browse of all open Kalshi events (one page at a time)."""
    assert state.client
    data = await state.client.events_page(cursor=cursor, limit=limit)
    events = []
    for ev in data.get("events") or []:
        markets = [catalog.normalize_market(m, ev) for m in ev.get("markets") or []]
        events.append({"event_ticker": ev["event_ticker"], "title": ev.get("title"),
                       "sub_title": ev.get("sub_title"), "category": ev.get("category"),
                       "series_ticker": ev.get("series_ticker"),
                       "markets": [m for m in markets if m["status"] == "active"]})
    return {"events": [e for e in events if e["markets"]], "cursor": data.get("cursor") or None}


@app.get("/api/kalshi/markets/{ticker}")
async def kalshi_market(ticker: str) -> dict[str, Any]:
    assert state.client
    try:
        m = await catalog.ensure_market(state.client, ticker.upper())
    except Exception as exc:
        raise HTTPException(404, f"Market lookup failed: {exc}")
    if not m:
        raise HTTPException(404, "Market not found")
    return m


@app.get("/api/quote")
async def quote(ticker: str, side: str, dollars: float | None = None,
                contracts: int | None = None) -> dict[str, Any]:
    assert state.client
    m = await catalog.ensure_market(state.client, ticker)
    if not m:
        raise HTTPException(404, "Market not found")
    bid, ask = sim.side_quote(m, side)
    if ask is None:
        return {"market": m, "error": f"No {side.upper()} offers"}
    n = contracts if contracts else sim.contracts_for_budget(dollars or 0, ask)
    cost, fee = sim.cents(n * ask), sim.kalshi_fee(n, ask)
    return {"market": m, "side": side, "price": ask, "bid": bid, "contracts": n, "cost": cost,
            "fee": fee, "total": sim.cents(cost + fee), "max_payout": float(n),
            "max_profit": sim.cents(n - cost - fee), "implied_prob": ask}


# (span seconds or None for "since listing", candle period in minutes)
HISTORY_RANGES = {"1d": (86_400, 1), "1w": (7 * 86_400, 60), "1m": (30 * 86_400, 60), "all": (None, 1440)}
_history_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
HISTORY_TTL = 60.0


@app.get("/api/markets/{ticker}/history")
async def market_history(ticker: str,
                         range: str = Query("1w", pattern="^(1d|1w|1m|all)$")) -> dict[str, Any]:
    """YES-price history for a market. Clients convert to the NO side as 1 - yes."""
    assert state.client
    ticker = ticker.upper()
    key = (ticker, range)
    hit = _history_cache.get(key)
    if hit and time.monotonic() - hit[0] < HISTORY_TTL:
        return hit[1]
    try:
        series = await catalog.series_for_market(state.client, ticker)
    except Exception as exc:
        raise HTTPException(502, f"Could not resolve market series: {exc}")
    if not series:
        raise HTTPException(404, "Market not found")
    span, period = HISTORY_RANGES[range]
    end = int(time.time())
    start = end - span if span else end - 3 * 365 * 86_400  # Kalshi clips to the listing date
    try:
        candles = await state.client.candlesticks(series, ticker, start, end, period)
    except Exception as exc:
        raise HTTPException(502, f"Kalshi price history unavailable: {exc}")
    out = {"ticker": ticker, "range": range, "period_minutes": period,
           "points": catalog.candles_to_points(candles)}
    _history_cache[key] = (time.monotonic(), out)
    return out


# --- portfolios & bets --------------------------------------------------------

class PortfolioIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    starting_balance: float = Field(gt=0, le=10_000_000)


class BetIn(BaseModel):
    ticker: str
    side: str = Field(pattern="^(yes|no)$")
    contracts: int | None = Field(default=None, gt=0)
    dollars: float | None = Field(default=None, gt=0)


@app.get("/api/portfolios")
def portfolios() -> list[dict[str, Any]]:
    with db.tx() as conn:
        out = []
        for p in db.rows(conn, "SELECT * FROM portfolios ORDER BY id"):
            s = sim.portfolio_summary(conn, p["id"])
            out.append({**p, "equity": s["equity"], "total_pnl": s["total_pnl"],
                        "total_return_pct": s["total_return_pct"], "open_count": s["open_count"]})
    return out


@app.post("/api/portfolios")
def create_portfolio(body: PortfolioIn) -> dict[str, Any]:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-") + f"-{int(time.time())}"
    with db.tx() as conn:
        pid = sim.create_portfolio(conn, slug, body.name, body.description, body.starting_balance)
        sim.record_snapshot(conn, pid)
        return sim.get_portfolio(conn, pid)


@app.delete("/api/portfolios/{pid}")
def delete_portfolio(pid: int) -> dict[str, Any]:
    with db.tx() as conn:
        conn.execute("DELETE FROM portfolios WHERE id = ?", (pid,))
    return {"deleted": pid}


@app.get("/api/portfolios/{pid}/summary")
def summary(pid: int) -> dict[str, Any]:
    with db.tx() as conn:
        try:
            return sim.portfolio_summary(conn, pid)
        except sim.BetError as exc:
            raise HTTPException(404, str(exc))


@app.get("/api/portfolios/{pid}/bets")
def bets(pid: int, status: str = Query("open", pattern="^(open|closed)$")) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return sim.list_bets(conn, pid, status)


@app.get("/api/portfolios/{pid}/snapshots")
def snapshots(pid: int) -> list[dict[str, Any]]:
    with db.tx() as conn:
        return db.rows(conn, "SELECT ts, cash, positions_value, equity FROM snapshots "
                             "WHERE portfolio_id = ? ORDER BY ts", (pid,))


@app.post("/api/portfolios/{pid}/bets")
async def place(pid: int, body: BetIn) -> dict[str, Any]:
    assert state.client
    m = await catalog.ensure_market(state.client, body.ticker.upper())
    if not m:
        raise HTTPException(404, "Market not found")
    try:
        with db.tx() as conn:
            return sim.place_bet(conn, pid, m, body.side, body.contracts, body.dollars, note="manual")
    except sim.BetError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/bets/{bet_id}/sell")
async def sell(bet_id: int) -> dict[str, Any]:
    assert state.client
    with db.tx() as conn:
        bet = conn.execute("SELECT market_ticker FROM bets WHERE id = ?", (bet_id,)).fetchone()
    if not bet:
        raise HTTPException(404, "Bet not found")
    m = await catalog.ensure_market(state.client, bet["market_ticker"])
    try:
        with db.tx() as conn:
            return sim.sell_bet(conn, bet_id, m)
    except sim.BetError as exc:
        raise HTTPException(400, str(exc))


# --- scenarios ----------------------------------------------------------------

@app.post("/api/scenarios/{slug}/reset")
def reset_scenario(slug: str, stake: float | None = None) -> dict[str, Any]:
    if slug not in scenarios.SCENARIOS:
        raise HTTPException(404, "Unknown scenario")
    try:
        return scenarios.seed_scenario(slug, stake=stake, reset=True)
    except sim.BetError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/compare")
def compare() -> dict[str, Any]:
    with db.tx() as conn:
        pfs = db.rows(conn, "SELECT * FROM portfolios WHERE slug IN ('all-dem', 'all-gop') ORDER BY slug")
        out: dict[str, Any] = {"scenarios": [], "races": []}
        race_rows: dict[str, dict[str, Any]] = {}
        for p in pfs:
            s = sim.portfolio_summary(conn, p["id"])
            s.pop("biggest_win"), s.pop("biggest_loss")
            s["meta"] = db.get_meta(f"scenario:{p['slug']}")
            out["scenarios"].append(s)
            for b in sim.list_bets(conn, p["id"], "open") + sim.list_bets(conn, p["id"], "closed"):
                key = b["market_ticker"].rsplit("-", 1)[0]
                r = race_rows.setdefault(key, {"race": b["event_title"], "chamber": b["chamber"],
                                               "state": b["state"], "district": b["district"]})
                pnl = b["pnl"] if b["status"] != "open" else b.get("unrealized_pnl")
                r[p["slug"]] = {"status": b["status"], "entry_price": b["entry_price"],
                                "mark": b.get("mark_price", b.get("exit_price")),
                                "pnl": pnl, "contracts": b["contracts"]}
        out["races"] = sorted(race_rows.values(), key=lambda r: (r["chamber"] or "", r["state"] or "",
                                                                  str(r["district"] or "")))
    return out


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()

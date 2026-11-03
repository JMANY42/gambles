"""Market catalog: normalizing Kalshi market payloads and discovering the
2026 midterm races (Senate, House, Governor, and chamber control)."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from . import db
from .kalshi import KalshiClient

log = logging.getLogger(__name__)

# Series ticker patterns for race-level markets. Kalshi has used several naming
# generations (SENATEPA, SENATEPARTY-PA, KXSENATEPA, ...), so we match them all
# and then keep only the 2026 events.
_RACE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Control", re.compile(r"^CONTROL(?P<chamber>[HS])$")),
    ("Senate", re.compile(r"^(?:KX)?SENATE(?:PARTY)?-?(?P<state>[A-Z]{2})(?P<special>S)?$")),
    ("Governor", re.compile(r"^(?:KX)?GOVPARTY-?(?P<state>[A-Z]{2})$")),
    ("House", re.compile(r"^HOUSE(?:PARTY-?)?(?P<state>[A-Z]{2})(?P<district>\d{1,2})(?P<special>S)?$")),
]

_2026_EVENT = re.compile(r"-(26|2026)$")
_PARTY_WORDS = {"democratic": "D", "democrat": "D", "republican": "R", "independent": "I"}


def classify_series(series_ticker: str) -> dict[str, Any] | None:
    """Return race metadata for a race-level series ticker, or None."""
    for chamber, pat in _RACE_PATTERNS:
        m = pat.match(series_ticker)
        if not m:
            continue
        g = m.groupdict()
        if chamber == "Control":
            return {"chamber": "Control", "state": None,
                    "district": "House" if g["chamber"] == "H" else "Senate"}
        district = None
        if chamber == "House":
            district = str(int(g["district"])) + (" (special)" if g.get("special") else "")
        elif g.get("special"):
            district = "special"
        return {"chamber": chamber, "state": g["state"], "district": district}
    return None


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def market_party(market: dict[str, Any]) -> str | None:
    """Infer which party a market's YES side represents."""
    suffix = market.get("ticker", "").rsplit("-", 1)[-1].upper()
    if suffix in ("D", "R", "I"):
        return suffix
    strike = market.get("custom_strike") or {}
    party = str(strike.get("Party", "")).lower()
    for word, code in _PARTY_WORDS.items():
        if word in party:
            return code
    sub = str(market.get("yes_sub_title", "")).lower()
    for word, code in _PARTY_WORDS.items():
        if sub.startswith(word):
            return code
    return None


def normalize_market(market: dict[str, Any], event: dict[str, Any] | None = None,
                     race: dict[str, Any] | None = None) -> dict[str, Any]:
    event = event or {}
    return {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker") or event.get("event_ticker"),
        "series_ticker": event.get("series_ticker"),
        "event_title": event.get("title"),
        "title": market.get("title"),
        "yes_sub_title": market.get("yes_sub_title") or market.get("subtitle"),
        "category": event.get("category"),
        "chamber": (race or {}).get("chamber"),
        "state": (race or {}).get("state"),
        "district": (race or {}).get("district"),
        # Race markets that aren't D/R are independents / third-party candidates.
        "party": (market_party(market) or "I") if race else None,
        "is_midterm": 1 if race else 0,
        "status": market.get("status"),
        "result": market.get("result") or None,
        "yes_bid": _f(market.get("yes_bid_dollars")),
        "yes_ask": _f(market.get("yes_ask_dollars")),
        "no_bid": _f(market.get("no_bid_dollars")),
        "no_ask": _f(market.get("no_ask_dollars")),
        "last_price": _f(market.get("last_price_dollars")),
        "volume": _f(market.get("volume_fp")),
        "open_interest": _f(market.get("open_interest_fp")),
        "close_time": market.get("close_time"),
    }


_UPSERT_COLS = [
    "ticker", "event_ticker", "series_ticker", "event_title", "title", "yes_sub_title",
    "category", "chamber", "state", "district", "party", "is_midterm", "status", "result",
    "yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume", "open_interest", "close_time",
]
# Columns that a price-only refresh may update (metadata is kept from discovery).
_QUOTE_COLS = ["status", "result", "yes_bid", "yes_ask", "no_bid", "no_ask",
               "last_price", "volume", "open_interest", "close_time"]


def upsert_markets(markets: list[dict[str, Any]], quotes_only: bool = False) -> None:
    if not markets:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.tx() as conn:
        for m in markets:
            if quotes_only:
                sets = ", ".join(f"{c} = :{c}" for c in _QUOTE_COLS)
                conn.execute(f"UPDATE markets SET {sets}, updated_at = :now WHERE ticker = :ticker",
                             {**{c: m.get(c) for c in _QUOTE_COLS}, "ticker": m["ticker"], "now": now})
                continue
            placeholders = ", ".join(f":{c}" for c in _UPSERT_COLS)
            # Never downgrade a midterm market's metadata when it's re-seen generically.
            updates = ", ".join(
                f"{c} = COALESCE(excluded.{c}, markets.{c})" if c not in _QUOTE_COLS else f"{c} = excluded.{c}"
                for c in _UPSERT_COLS if c not in ("ticker", "is_midterm")
            )
            conn.execute(
                f"INSERT INTO markets ({', '.join(_UPSERT_COLS)}, updated_at) VALUES ({placeholders}, :now) "
                f"ON CONFLICT(ticker) DO UPDATE SET {updates}, "
                f"is_midterm = MAX(markets.is_midterm, excluded.is_midterm), updated_at = :now",
                {**{c: m.get(c) for c in _UPSERT_COLS}, "now": now},
            )


async def discover_midterms(client: KalshiClient) -> dict[str, Any]:
    """Walk Kalshi's election series and cache every 2026 race market."""
    all_series = await client.series(category="Elections")
    candidates = [(s, classify_series(s["ticker"])) for s in all_series]
    candidates = [(s, r) for s, r in candidates if r]
    log.info("Midterm discovery: %d candidate race series", len(candidates))

    found: list[dict[str, Any]] = []
    seen_races: dict[tuple, str] = {}
    events_kept = 0
    for series, race in candidates:
        try:
            events = await client.events_for_series(series["ticker"])
        except Exception as exc:
            log.warning("Skipping series %s: %s", series["ticker"], exc)
            continue
        for ev in events:
            if not _2026_EVENT.search(ev.get("event_ticker", "")):
                continue
            if not ev.get("markets"):
                log.info("Skipping %s: event has no markets", ev["event_ticker"])
                continue
            # Several legacy series can describe the same race; keep the first.
            race_key = (race["chamber"], race["state"], race["district"])
            if race_key in seen_races:  # same event via another series, or a duplicate race
                continue
            seen_races[race_key] = ev["event_ticker"]
            events_kept += 1
            for m in ev.get("markets") or []:
                found.append(normalize_market(m, ev, race))

    upsert_markets(found)
    summary = {
        "series_scanned": len(candidates),
        "events": events_kept,
        "markets": len(found),
        "refreshed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    db.set_meta("catalog", summary)
    log.info("Midterm discovery complete: %s", summary)
    return summary


async def refresh_quotes(client: KalshiClient, tickers: list[str]) -> int:
    if not tickers:
        return 0
    raw = await client.markets_by_tickers(tickers)
    upsert_markets([normalize_market(m) for m in raw], quotes_only=True)
    return len(raw)


async def ensure_market(client: KalshiClient, ticker: str) -> dict[str, Any] | None:
    """Fetch a market (and its event, for titles) into the cache; return the row."""
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM markets WHERE ticker = ?", (ticker,)).fetchone()
    if row is None:
        m = await client.market(ticker)
        if not m:
            return None
        ev = {}
        try:
            ev = (await client.event(m["event_ticker"])).get("event") or {}
        except Exception:
            pass
        upsert_markets([normalize_market(m, ev)])
    else:
        await refresh_quotes(client, [ticker])
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM markets WHERE ticker = ?", (ticker,)).fetchone()
    return dict(row) if row else None

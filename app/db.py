"""SQLite persistence. One connection per call; WAL mode for concurrent reads."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolios (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    slug             TEXT UNIQUE NOT NULL,
    name             TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    starting_balance REAL NOT NULL,
    cash             REAL NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Cached Kalshi markets (latest known quote + metadata).
CREATE TABLE IF NOT EXISTS markets (
    ticker        TEXT PRIMARY KEY,
    event_ticker  TEXT NOT NULL,
    series_ticker TEXT,
    event_title   TEXT,
    title         TEXT,
    yes_sub_title TEXT,
    category      TEXT,
    chamber       TEXT,     -- Senate / House / Governor / Control (midterms only)
    state         TEXT,
    district      TEXT,
    party         TEXT,     -- D / R / I / NULL
    is_midterm    INTEGER NOT NULL DEFAULT 0,
    status        TEXT,
    result        TEXT,
    yes_bid       REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    last_price    REAL,
    volume        REAL,
    open_interest REAL,
    close_time    TEXT,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS ix_markets_event ON markets(event_ticker);
CREATE INDEX IF NOT EXISTS ix_markets_midterm ON markets(is_midterm);

CREATE TABLE IF NOT EXISTS bets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    market_ticker TEXT NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('yes','no')),
    contracts     INTEGER NOT NULL CHECK (contracts > 0),
    entry_price   REAL NOT NULL,         -- $ per contract paid
    cost          REAL NOT NULL,         -- contracts * entry_price
    entry_fee     REAL NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','won','lost','sold','void')),
    exit_price    REAL,                  -- 1/0 at settlement, bid if sold
    exit_fee      REAL NOT NULL DEFAULT 0,
    proceeds      REAL,                  -- cash returned (before exit fee)
    pnl           REAL,                  -- proceeds - cost - fees
    note          TEXT,
    placed_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    closed_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_bets_portfolio ON bets(portfolio_id, status);
CREATE INDEX IF NOT EXISTS ix_bets_market ON bets(market_ticker);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id  INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    ts            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    cash          REAL NOT NULL,
    positions_value REAL NOT NULL,
    equity        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_portfolio ON snapshots(portfolio_id, ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


_db_path: Path | None = None


def set_db_path(path: Path | None) -> None:
    """Point the app at a different database (used by tests)."""
    global _db_path
    _db_path = path


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect(_db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with tx() as conn:
        conn.executescript(SCHEMA)


def rows(conn: sqlite3.Connection, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_meta(key: str, default: Any = None) -> Any:
    with tx() as conn:
        r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_meta(key: str, value: Any) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


_load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    api_base: str = os.environ.get(
        "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
    )
    # Optional. Public market data needs no auth; a key only raises rate limits.
    api_key_id: str | None = os.environ.get("KALSHI_API_KEY_ID") or None
    private_key_path: str | None = os.environ.get("KALSHI_PRIVATE_KEY_PATH") or None

    db_path: Path = Path(os.environ.get("GAMBLES_DB", ROOT / "data" / "gambles.db"))
    host: str = os.environ.get("GAMBLES_HOST", "127.0.0.1")
    port: int = int(os.environ.get("GAMBLES_PORT", "8000"))

    # Background refresh cadence
    price_refresh_seconds: int = int(os.environ.get("GAMBLES_PRICE_REFRESH", "60"))
    catalog_refresh_hours: float = float(os.environ.get("GAMBLES_CATALOG_REFRESH_HOURS", "6"))
    snapshot_minutes: float = float(os.environ.get("GAMBLES_SNAPSHOT_MINUTES", "5"))

    # Simulation defaults
    starting_balance: float = float(os.environ.get("GAMBLES_STARTING_BALANCE", "25000"))
    scenario_stake: float = float(os.environ.get("GAMBLES_SCENARIO_STAKE", "100"))
    simulate_fees: bool = os.environ.get("GAMBLES_SIMULATE_FEES", "1") not in ("0", "false", "no")

    # Client-side throttle (requests/sec). Kalshi's basic tier allows ~20 reads/sec.
    max_rps: float = float(os.environ.get("KALSHI_MAX_RPS", "8"))


settings = Settings()

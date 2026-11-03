"""Thin read-only client for the Kalshi Trade API v2.

Market data endpoints are public. If KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH
are configured, requests are signed (RSA-PSS / SHA256) per Kalshi's docs, which
grants the account's (higher) rate limits. This app never calls order endpoints.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger(__name__)


class KalshiClient:
    def __init__(self, base_url: str | None = None, max_rps: float | None = None):
        self.base_url = (base_url or settings.api_base).rstrip("/")
        self._path_prefix = urlparse(self.base_url).path
        self._min_interval = 1.0 / (max_rps or settings.max_rps)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._client = httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "gambles-sim/0.1"})
        self._key_id = settings.api_key_id
        self._private_key = None
        if self._key_id and settings.private_key_path:
            try:
                from cryptography.hazmat.primitives import serialization

                with open(settings.private_key_path, "rb") as fh:
                    self._private_key = serialization.load_pem_private_key(fh.read(), password=None)
                log.info("Kalshi API key loaded; requests will be signed.")
            except Exception as exc:  # pragma: no cover - depends on user files
                log.warning("Could not load Kalshi private key (%s); using public access.", exc)
                self._private_key = None

    @property
    def authenticated(self) -> bool:
        return self._private_key is not None

    async def close(self) -> None:
        await self._client.aclose()

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self._private_key:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts = str(int(time.time() * 1000))
        message = f"{ts}{method.upper()}{self._path_prefix}{path}".encode()
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._key_id or "",
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        for attempt in range(5):
            await self._throttle()
            try:
                resp = await self._client.get(
                    self.base_url + path, params=params, headers=self._auth_headers("GET", path)
                )
            except httpx.TransportError as exc:
                log.warning("Kalshi transport error on %s: %s", path, exc)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"Kalshi request failed after retries: {path}")

    async def paginate(self, path: str, key: str, params: dict[str, Any] | None = None,
                       max_pages: int = 50) -> AsyncIterator[dict[str, Any]]:
        params = dict(params or {})
        for _ in range(max_pages):
            data = await self.get(path, params)
            for item in data.get(key) or []:
                yield item
            cursor = data.get("cursor")
            if not cursor or not data.get(key):
                return
            params["cursor"] = cursor

    # --- convenience wrappers -------------------------------------------------

    async def series(self, category: str | None = None) -> list[dict[str, Any]]:
        data = await self.get("/series", {"category": category})
        return data.get("series") or []

    async def events_for_series(self, series_ticker: str) -> list[dict[str, Any]]:
        return [e async for e in self.paginate(
            "/events", "events",
            {"series_ticker": series_ticker, "with_nested_markets": "true", "limit": 200},
            max_pages=5,
        )]

    async def events_page(self, cursor: str | None = None, limit: int = 100,
                          status: str = "open") -> dict[str, Any]:
        return await self.get("/events", {
            "status": status, "with_nested_markets": "true", "limit": limit, "cursor": cursor,
        })

    async def event(self, event_ticker: str) -> dict[str, Any]:
        return await self.get(f"/events/{event_ticker}", {"with_nested_markets": "true"})

    async def markets_by_tickers(self, tickers: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i in range(0, len(tickers), 100):
            chunk = tickers[i : i + 100]
            data = await self.get("/markets", {"tickers": ",".join(chunk), "limit": 1000})
            out.extend(data.get("markets") or [])
        return out

    async def market(self, ticker: str) -> dict[str, Any]:
        try:
            return (await self.get(f"/markets/{ticker}")).get("market") or {}
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404):
                return {}
            raise

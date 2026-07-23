"""Price feeds.

- PaperFeed: geometric-Brownian simulator per token, so the whole app runs
  with zero keys and zero network access.
- DexscreenerFeed: free public API, polls pair prices for Base / Robinhood
  Chain (or any chain Dexscreener indexes).
- QuoterFeed hook: live executor can also mark price from the on-chain
  Uniswap v3 QuoterV2 (see chains.py).
"""
from __future__ import annotations

import asyncio
import math
import random
import time
from collections import deque

import httpx

from .config import BotConfig, ChainConfig, TokenConfig


def has_live_price_source(tok: TokenConfig, chain: ChainConfig) -> bool:
    """True when Dexscreener can price this token (slug + address or pair)."""
    return bool(chain.dexscreener_slug and (tok.address or tok.dexscreener_pair))

CANDLE_SECONDS = 15
MAX_CANDLES = 5000  # matches SQLite per-token cap (~20h of 15s bars)


class Candles:
    def __init__(self) -> None:
        self.data: deque[dict] = deque(maxlen=MAX_CANDLES)

    def push(self, ts: float, price: float) -> dict | None:
        """Update the open bucket. Returns a copy of the prior candle when it closes."""
        bucket = int(ts // CANDLE_SECONDS) * CANDLE_SECONDS
        if self.data and self.data[-1]["time"] == bucket:
            c = self.data[-1]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            return None
        closed = dict(self.data[-1]) if self.data else None
        self.data.append({"time": bucket, "open": price, "high": price,
                          "low": price, "close": price})
        return closed

    def series(self) -> list[dict]:
        return list(self.data)

    def replace(self, candles: list[dict]) -> None:
        """Replace series with pre-built candles (backfill / DB restore)."""
        self.data.clear()
        for c in candles[-MAX_CANDLES:]:
            self.data.append({
                "time": int(c["time"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
            })


class PriceBook:
    """Latest price + candle history per (chain, token)."""

    def __init__(self, on_candle_close=None) -> None:
        self.last: dict[tuple[str, str], float] = {}
        self.candles: dict[tuple[str, str], Candles] = {}
        self.on_candle_close = on_candle_close

    def update(self, chain: str, token: str, price: float) -> None:
        key = (chain, token.upper())
        self.last[key] = price
        closed = self.candles.setdefault(key, Candles()).push(time.time(), price)
        if closed and self.on_candle_close:
            try:
                self.on_candle_close(chain, key[1], closed)
            except Exception:
                pass

    def seed_history(self, chain: str, token: str, candles: list[dict]) -> None:
        """Merge historical candles under any existing live series (ascending, no overlap)."""
        from .history import merge_candles
        key = (chain, token.upper())
        cur = self.candles.setdefault(key, Candles())
        merged = merge_candles(candles, list(cur.data))
        cur.replace(merged)
        if merged and key not in self.last:
            self.last[key] = float(merged[-1]["close"])

    def price(self, chain: str, token: str) -> float | None:
        return self.last.get((chain, token.upper()))

    def history(self, chain: str, token: str) -> list[dict]:
        c = self.candles.get((chain, token.upper()))
        return c.series() if c else []


class PaperFeed:
    """Mean-reverting GBM so paper prices wander through your trigger levels."""

    def __init__(self, book: PriceBook, cfg: BotConfig) -> None:
        self.book = book
        self.cfg = cfg
        self._state: dict[tuple[str, str], float] = {}
        self._anchor: dict[tuple[str, str], float] = {}
        self._vol: dict[tuple[str, str], float] = {}

    def register(self, chain: str, tok: TokenConfig) -> None:
        key = (chain, tok.symbol)
        self._state[key] = tok.paper_start_price
        self._anchor[key] = tok.paper_start_price
        self._vol[key] = tok.paper_volatility
        self.book.update(chain, tok.symbol, tok.paper_start_price)

    def apply_impact(self, chain: str, token: str, bps: float) -> None:
        """Trades in paper mode nudge the simulated price.

        No-op when the token was never registered (e.g. live-priced in paper mode).
        """
        key = (chain, token.upper())
        if key not in self._state:
            return
        self._state[key] *= 1 + bps / 10_000
        self.book.update(chain, token, self._state[key])

    async def run(self) -> None:
        while True:
            for key, price in list(self._state.items()):
                sigma = self._vol[key]
                drift = 0.02 * math.log(self._anchor[key] / price)  # mild mean reversion
                shock = random.gauss(0, sigma)
                price = max(1e-9, price * math.exp(drift * 0.01 + shock))
                self._state[key] = price
                self.book.update(key[0], key[1], price)
            await asyncio.sleep(1.0)


class DexscreenerFeed:
    """Polls https://api.dexscreener.com for live pair prices."""

    BASE = "https://api.dexscreener.com/latest/dex"

    def __init__(self, book: PriceBook, cfg: BotConfig) -> None:
        self.book = book
        self.cfg = cfg

    async def run(self) -> None:
        from .httputil import httpx_client_kwargs
        async with httpx.AsyncClient(**httpx_client_kwargs()) as client:
            while True:
                for chain_key, chain in self.cfg.chains.items():
                    slug = chain.dexscreener_slug
                    if not slug:
                        continue
                    for tok in chain.tokens.values():
                        try:
                            if tok.dexscreener_pair:
                                url = f"{self.BASE}/pairs/{slug}/{tok.dexscreener_pair}"
                            elif tok.address:
                                url = f"{self.BASE}/tokens/{tok.address}"
                            else:
                                continue
                            r = await client.get(url)
                            r.raise_for_status()
                            pairs = r.json().get("pairs") or []
                            pairs = [p for p in pairs if p.get("chainId") == slug] or pairs
                            if pairs:
                                best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd") or 0))
                                px = float(best.get("priceUsd") or 0)
                                if px > 0:
                                    self.book.update(chain_key, tok.symbol, px)
                        except Exception:
                            pass  # transient feed errors: keep last price
                await asyncio.sleep(3.0)

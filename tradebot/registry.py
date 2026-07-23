"""Token discovery via Dexscreener (watch-any-CA).

HTTP is injectable so unit tests can feed canned JSON with zero network.
"""
from __future__ import annotations

from typing import Callable

import httpx

from .config import ChainConfig
from .httputil import httpx_client_kwargs, with_ssl_hint

FetchFn = Callable[[str], dict]

DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{address}"


def default_fetch(url: str) -> dict:
    try:
        r = httpx.get(url, **httpx_client_kwargs())
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise ValueError(with_ssl_hint(
            f"Dexscreener request failed: {e}", e)) from e


def resolve(address: str, chain: ChainConfig,
            fetch: FetchFn | None = None) -> dict:
    """Resolve a contract address to the deepest Dexscreener pair on `chain`.

    Filters to pairs on `chain.dexscreener_slug` where `baseToken.address`
    matches (case-insensitive). Ignores pairs where the address is only the
    quote token. Returns symbol, name, price_usd, pair_address, liquidity_usd, dex.
    """
    if not chain.dexscreener_slug:
        raise ValueError(f"chain has no dexscreener_slug")
    addr = address.strip()
    if not addr.startswith("0x") or len(addr) != 42:
        raise ValueError(f"invalid address '{address}'")
    fetch = fetch or default_fetch
    try:
        data = fetch(DEX_TOKENS.format(address=addr))
    except ValueError as e:
        raise ValueError(with_ssl_hint(str(e), e)) from e
    except Exception as e:
        raise ValueError(with_ssl_hint(
            f"Dexscreener lookup failed: {e}", e)) from e
    if not isinstance(data, dict):
        raise ValueError("Dexscreener returned unexpected payload")
    pairs = data.get("pairs") or []
    slug = chain.dexscreener_slug
    addr_l = addr.lower()
    candidates: list[dict] = []
    for p in pairs:
        if (p.get("chainId") or "") != slug:
            continue
        base = ((p.get("baseToken") or {}).get("address") or "").lower()
        if base != addr_l:
            continue
        candidates.append(p)
    if not candidates:
        raise ValueError(
            f"no Dexscreener pairs for {addr} on {slug} "
            f"(as base token)")
    best = max(
        candidates,
        key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0),
    )
    base_tok = best.get("baseToken") or {}
    return {
        "symbol": (base_tok.get("symbol") or addr[2:8]).upper(),
        "name": base_tok.get("name") or "",
        "price_usd": float(best.get("priceUsd") or 0),
        "pair_address": best.get("pairAddress") or "",
        "liquidity_usd": float((best.get("liquidity") or {}).get("usd") or 0),
        "dex": best.get("dexId") or "unknown",
    }

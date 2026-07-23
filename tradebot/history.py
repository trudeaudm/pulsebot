"""GeckoTerminal OHLCV backfill for chart history.

Fetches minute candles for a pool and merges them under live PriceBook
history. Injectable `fetch` keeps tests offline; production uses httpx with
httputil TLS kwargs.
"""
from __future__ import annotations

from typing import Any, Callable

FetchFn = Callable[[str], Any]

GECKO_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/{network}/pools/{pool}"
    "/ohlcv/minute?aggregate=1&limit=180"
)


def parse_ohlcv(payload: Any) -> list[dict]:
    """Parse a GeckoTerminal OHLCV JSON body into candle dicts.

    Rows are ``[ts, open, high, low, close, volume]``. Missing/partial data
    yields ``[]``. Result is sorted ascending by time.
    """
    try:
        rows = payload["data"]["attributes"]["ohlcv_list"]
    except (TypeError, KeyError, AttributeError):
        return []
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        try:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            ts = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
            out.append({"time": ts, "open": o, "high": h, "low": l, "close": c})
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda x: x["time"])
    return out


def fetch_ohlcv(network: str, pool_address: str,
                fetch: FetchFn | None = None) -> list[dict]:
    """GET minute OHLCV for ``pool_address`` on ``network``. Returns ``[]`` on failure."""
    network = (network or "").strip()
    pool = (pool_address or "").strip()
    if not network or not pool:
        return []
    url = GECKO_OHLCV_URL.format(network=network, pool=pool)

    def _default_fetch(u: str) -> Any:
        import httpx
        from .httputil import httpx_client_kwargs
        with httpx.Client(**httpx_client_kwargs()) as client:
            r = client.get(u)
            r.raise_for_status()
            return r.json()

    try:
        payload = (fetch or _default_fetch)(url)
        return parse_ohlcv(payload)
    except Exception:
        return []


def merge_candles(backfill: list[dict], live: list[dict]) -> list[dict]:
    """Keep ascending time; drop backfill candles at/after the first live candle."""
    bf = sorted(
        (dict(c) for c in backfill if "time" in c),
        key=lambda c: c["time"],
    )
    lv = sorted(
        (dict(c) for c in live if "time" in c),
        key=lambda c: c["time"],
    )
    if lv:
        first = lv[0]["time"]
        bf = [c for c in bf if c["time"] < first]
    # de-dupe by time preferring live
    by_t: dict[int, dict] = {}
    for c in bf:
        by_t[int(c["time"])] = c
    for c in lv:
        by_t[int(c["time"])] = c
    return [by_t[t] for t in sorted(by_t)]

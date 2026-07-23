"""SQLite persistence for trades, strategies, equity, and resume metadata.

Write-through: every persist call is isolated so a DB failure never interrupts
the trading loop (invariant 1). Callers pass an optional on_error callback
(typically Engine.log) to surface failures in engine.events.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Callable

from .commands import Condition, StrategySpec
from .portfolio import Portfolio, Trade

OnError = Callable[[str], None]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    chain TEXT NOT NULL,
    token TEXT NOT NULL,
    side TEXT NOT NULL,
    usd REAL NOT NULL,
    qty REAL NOT NULL,
    price REAL NOT NULL,
    ref_price REAL NOT NULL,
    strategy_id TEXT NOT NULL,
    tx_hash TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'paper'
);
CREATE TABLE IF NOT EXISTS strategies (
    id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    chain TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL,
    paused INTEGER NOT NULL,
    accrued_usd REAL NOT NULL,
    spent_usd REAL NOT NULL,
    fills INTEGER NOT NULL,
    qty_bought REAL NOT NULL,
    qty_sold REAL NOT NULL,
    usd_bought REAL NOT NULL,
    usd_sold REAL NOT NULL,
    created REAL NOT NULL,
    first_fill REAL NOT NULL,
    last_fill REAL NOT NULL,
    last_tick REAL NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    blocked_reason TEXT NOT NULL DEFAULT '',
    peak_price REAL NOT NULL DEFAULT 0,
    prev_price REAL NOT NULL DEFAULT 0,
    grid_lots_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS equity (
    time REAL NOT NULL,
    value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watched_tokens (
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    address TEXT NOT NULL,
    pair TEXT NOT NULL DEFAULT '',
    decimals INTEGER NOT NULL DEFAULT 18,
    PRIMARY KEY (chain, symbol)
);
CREATE TABLE IF NOT EXISTS candles (
    chain TEXT NOT NULL,
    token TEXT NOT NULL,
    time INTEGER NOT NULL,
    o REAL NOT NULL,
    h REAL NOT NULL,
    l REAL NOT NULL,
    c REAL NOT NULL,
    PRIMARY KEY (chain, token, time)
);
"""

CANDLE_HISTORY_CAP = 5000


def spec_to_json(spec: StrategySpec) -> str:
    return json.dumps(asdict(spec))


def spec_from_json(raw: str) -> StrategySpec:
    data = json.loads(raw)
    for k in ("trigger", "condition"):
        if data.get(k):
            data[k] = Condition(op=data[k]["op"], value=float(data[k]["value"]))
    allowed = {f.name for f in fields(StrategySpec)}
    return StrategySpec(**{k: v for k, v in data.items() if k in allowed})


def archive_db(path: str | Path) -> Path | None:
    """Rename an existing DB out of the way. Returns the archive path, or None."""
    p = Path(path)
    if not p.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = p.with_name(f"{p.name}.{stamp}")
    n = 1
    while dest.exists():
        dest = p.with_name(f"{p.name}.{stamp}-{n}")
        n += 1
    shutil.move(str(p), str(dest))
    # also move WAL/SHM sidecars if present
    for suffix in ("-wal", "-shm"):
        side = Path(str(p) + suffix)
        if side.exists():
            shutil.move(str(side), str(dest) + suffix)
    return dest


class Store:
    """Write-through SQLite store. All mutating methods swallow DB errors."""

    def __init__(self, path: str | Path, on_error: OnError | None = None) -> None:
        self.path = Path(path)
        self.on_error = on_error
        self._last_equity_write = 0.0
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(strategies)")}
        if "blocked_reason" not in cols:
            self._conn.execute(
                "ALTER TABLE strategies ADD COLUMN blocked_reason TEXT NOT NULL DEFAULT ''"
            )
        if "peak_price" not in cols:
            self._conn.execute(
                "ALTER TABLE strategies ADD COLUMN peak_price REAL NOT NULL DEFAULT 0"
            )
        if "prev_price" not in cols:
            self._conn.execute(
                "ALTER TABLE strategies ADD COLUMN prev_price REAL NOT NULL DEFAULT 0"
            )
        if "grid_lots_json" not in cols:
            self._conn.execute(
                "ALTER TABLE strategies ADD COLUMN grid_lots_json TEXT NOT NULL DEFAULT '{}'"
            )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS watched_tokens ("
            "chain TEXT NOT NULL, symbol TEXT NOT NULL, address TEXT NOT NULL, "
            "pair TEXT NOT NULL DEFAULT '', decimals INTEGER NOT NULL DEFAULT 18, "
            "PRIMARY KEY (chain, symbol))"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS candles ("
            "chain TEXT NOT NULL, token TEXT NOT NULL, time INTEGER NOT NULL, "
            "o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, "
            "PRIMARY KEY (chain, token, time))"
        )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def set_on_error(self, cb: OnError | None) -> None:
        self.on_error = cb

    def _err(self, e: Exception) -> None:
        if self.on_error:
            self.on_error(f"db error: {e}")

    def _safe(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:
            self._err(e)

    # ------------------------------------------------------------- meta

    def set_meta(self, key: str, value: str) -> None:
        def go() -> None:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()
        self._safe(go)

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        try:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else default
        except Exception as e:
            self._err(e)
            return default

    def save_portfolio_meta(self, portfolio: Portfolio, engine_paused: bool = False) -> None:
        self.set_meta("start_equity", str(portfolio.start_equity))
        self.set_meta("engine_paused", "1" if engine_paused else "0")

    # ------------------------------------------------------------- trades

    def save_trade(self, trade: Trade) -> None:
        def go() -> None:
            self._conn.execute(
                "INSERT INTO trades(ts, chain, token, side, usd, qty, price, "
                "ref_price, strategy_id, tx_hash, mode) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (trade.ts, trade.chain, trade.token, trade.side, trade.usd,
                 trade.qty, trade.price, trade.ref_price, trade.strategy_id,
                 trade.tx_hash, trade.mode),
            )
            self._conn.commit()
        self._safe(go)

    def load_trades(self) -> list[Trade]:
        try:
            rows = self._conn.execute(
                "SELECT ts, chain, token, side, usd, qty, price, ref_price, "
                "strategy_id, tx_hash, mode FROM trades ORDER BY rowid"
            ).fetchall()
            return [
                Trade(
                    ts=r["ts"], chain=r["chain"], token=r["token"], side=r["side"],
                    usd=r["usd"], qty=r["qty"], price=r["price"],
                    ref_price=r["ref_price"], strategy_id=r["strategy_id"],
                    tx_hash=r["tx_hash"] or "", mode=r["mode"] or "paper",
                )
                for r in rows
            ]
        except Exception as e:
            self._err(e)
            return []

    # ------------------------------------------------------------- strategies

    def save_strategy(self, s) -> None:  # Strategy — avoid circular import typing
        def go() -> None:
            lots_json = json.dumps(
                {str(k): v for k, v in (s.grid_lots or {}).items()})
            self._conn.execute(
                "INSERT INTO strategies("
                "id, spec_json, chain, status, phase, paused, accrued_usd, "
                "spent_usd, fills, qty_bought, qty_sold, usd_bought, usd_sold, "
                "created, first_fill, last_fill, last_tick, error, blocked_reason, "
                "peak_price, prev_price, grid_lots_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "spec_json=excluded.spec_json, chain=excluded.chain, "
                "status=excluded.status, phase=excluded.phase, "
                "paused=excluded.paused, accrued_usd=excluded.accrued_usd, "
                "spent_usd=excluded.spent_usd, fills=excluded.fills, "
                "qty_bought=excluded.qty_bought, qty_sold=excluded.qty_sold, "
                "usd_bought=excluded.usd_bought, usd_sold=excluded.usd_sold, "
                "created=excluded.created, first_fill=excluded.first_fill, "
                "last_fill=excluded.last_fill, last_tick=excluded.last_tick, "
                "error=excluded.error, blocked_reason=excluded.blocked_reason, "
                "peak_price=excluded.peak_price, prev_price=excluded.prev_price, "
                "grid_lots_json=excluded.grid_lots_json",
                (
                    s.id, spec_to_json(s.spec), s.chain, s.status, s.phase,
                    1 if s.paused else 0, s.accrued_usd, s.spent_usd, s.fills,
                    s.qty_bought, s.qty_sold, s.usd_bought, s.usd_sold,
                    s.created, s.first_fill, s.last_fill, s.last_tick, s.error,
                    s.blocked_reason or "", s.peak_price or 0.0,
                    s.prev_price or 0.0, lots_json,
                ),
            )
            self._conn.commit()
        self._safe(go)

    def load_strategy_rows(self) -> list[sqlite3.Row]:
        try:
            return list(self._conn.execute(
                "SELECT * FROM strategies ORDER BY created, id"
            ).fetchall())
        except Exception as e:
            self._err(e)
            return []

    # ------------------------------------------------------------- equity

    def save_equity_point(self, point: dict, min_interval: float = 10.0) -> None:
        """Persist an equity sample at most once per min_interval seconds."""
        now = time.time()
        if now - self._last_equity_write < min_interval:
            return
        t = float(point.get("time", now))
        v = float(point["value"])

        def go() -> None:
            self._conn.execute(
                "INSERT INTO equity(time, value) VALUES(?, ?)", (t, v)
            )
            self._conn.commit()
            self._last_equity_write = now

        self._safe(go)

    def load_equity(self) -> list[dict]:
        try:
            rows = self._conn.execute(
                "SELECT time, value FROM equity ORDER BY time"
            ).fetchall()
            return [{"time": r["time"], "value": r["value"]} for r in rows]
        except Exception as e:
            self._err(e)
            return []

    # ------------------------------------------------------------- queries

    def has_data(self) -> bool:
        try:
            n = self._conn.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"]
            if n:
                return True
            n = self._conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
            return bool(n)
        except Exception as e:
            self._err(e)
            return False

    def max_strategy_num(self) -> int:
        """Highest numeric suffix among strategy IDs like S1, S12. 0 if none."""
        try:
            rows = self._conn.execute("SELECT id FROM strategies").fetchall()
            best = 0
            for r in rows:
                sid = r["id"]
                if sid.startswith("S") and sid[1:].isdigit():
                    best = max(best, int(sid[1:]))
            return best
        except Exception as e:
            self._err(e)
            return 0

    # ------------------------------------------------------------- watched tokens

    def save_watched_token(self, chain: str, symbol: str, address: str,
                           pair: str, decimals: int = 18) -> None:
        def go() -> None:
            self._conn.execute(
                "INSERT INTO watched_tokens(chain, symbol, address, pair, decimals) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(chain, symbol) DO UPDATE SET "
                "address=excluded.address, pair=excluded.pair, "
                "decimals=excluded.decimals",
                (chain, symbol, address, pair, decimals),
            )
            self._conn.commit()
        self._safe(go)

    def delete_watched_token(self, chain: str, symbol: str) -> None:
        def go() -> None:
            self._conn.execute(
                "DELETE FROM watched_tokens WHERE chain=? AND symbol=?",
                (chain, symbol),
            )
            self._conn.commit()
        self._safe(go)

    def load_watched_tokens(self) -> list[dict]:
        try:
            rows = self._conn.execute(
                "SELECT chain, symbol, address, pair, decimals "
                "FROM watched_tokens ORDER BY chain, symbol"
            ).fetchall()
            return [
                {"chain": r["chain"], "symbol": r["symbol"],
                 "address": r["address"], "pair": r["pair"],
                 "decimals": int(r["decimals"])}
                for r in rows
            ]
        except Exception as e:
            self._err(e)
            return []

    # ------------------------------------------------------------- candles

    def save_candle(self, chain: str, token: str, candle: dict) -> None:
        """Write-behind one closed candle; prune oldest beyond CANDLE_HISTORY_CAP."""
        def go() -> None:
            self._conn.execute(
                "INSERT INTO candles(chain, token, time, o, h, l, c) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(chain, token, time) DO UPDATE SET "
                "o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c",
                (chain, token.upper(), int(candle["time"]),
                 float(candle["open"]), float(candle["high"]),
                 float(candle["low"]), float(candle["close"])),
            )
            self._conn.execute(
                "DELETE FROM candles WHERE chain=? AND token=? AND time IN ("
                "  SELECT time FROM candles WHERE chain=? AND token=? "
                "  ORDER BY time DESC LIMIT -1 OFFSET ?"
                ")",
                (chain, token.upper(), chain, token.upper(), CANDLE_HISTORY_CAP),
            )
            self._conn.commit()
        self._safe(go)

    def load_candles(self, chain: str | None = None,
                     token: str | None = None) -> list[dict]:
        try:
            if chain and token:
                rows = self._conn.execute(
                    "SELECT chain, token, time, o, h, l, c FROM candles "
                    "WHERE chain=? AND token=? ORDER BY time",
                    (chain, token.upper()),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT chain, token, time, o, h, l, c FROM candles "
                    "ORDER BY chain, token, time"
                ).fetchall()
            return [
                {"chain": r["chain"], "token": r["token"], "time": int(r["time"]),
                 "open": float(r["o"]), "high": float(r["h"]),
                 "low": float(r["l"]), "close": float(r["c"])}
                for r in rows
            ]
        except Exception as e:
            self._err(e)
            return []

    def delete_candles(self, chain: str, token: str) -> None:
        def go() -> None:
            self._conn.execute(
                "DELETE FROM candles WHERE chain=? AND token=?",
                (chain, token.upper()),
            )
            self._conn.commit()
        self._safe(go)

    def prune_candles(self, chain: str, token: str,
                      keep: int = CANDLE_HISTORY_CAP) -> int:
        """Drop oldest rows beyond ``keep``. Returns rows deleted (best-effort)."""
        deleted = 0

        def go() -> None:
            nonlocal deleted
            cur = self._conn.execute(
                "DELETE FROM candles WHERE chain=? AND token=? AND time IN ("
                "  SELECT time FROM candles WHERE chain=? AND token=? "
                "  ORDER BY time DESC LIMIT -1 OFFSET ?"
                ")",
                (chain, token.upper(), chain, token.upper(), keep),
            )
            deleted = cur.rowcount if cur.rowcount is not None else 0
            self._conn.commit()

        self._safe(go)
        return deleted

"""Strategy engine.

Each parsed command becomes a Strategy with a small state machine, ticked by
the engine loop. Rate strategies accrue budget continuously (rate_usd_per_min)
and flush a child order whenever the accrued amount crosses min_slice_usd, so
"$300 per minute" becomes a stream of ~$10-50 market orders rather than one
big lurch — the same shape as a TWAP with a gate condition.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .commands import StrategySpec
from .config import BotConfig
from .portfolio import Portfolio, Trade
from .prices import PaperFeed, PriceBook

if TYPE_CHECKING:
    from .store import Store

_id_counter = itertools.count(1)


def seed_id_counter(next_n: int) -> None:
    """Set the next Strategy id number (so the next id is S{next_n})."""
    global _id_counter
    _id_counter = itertools.count(max(1, next_n))


def grid_level_prices(lower: float, upper: float, n: int) -> list[float]:
    """Evenly spaced levels L0=lower … L(n-1)=upper."""
    if n <= 1:
        return [lower]
    return [lower + (upper - lower) * i / (n - 1) for i in range(n)]


def _loads_grid_lots(row) -> dict:
    import json
    if "grid_lots_json" not in row.keys() or not row["grid_lots_json"]:
        return {}
    try:
        raw = json.loads(row["grid_lots_json"])
        return {int(k): {"qty": float(v["qty"]), "usd": float(v["usd"])}
                for k, v in raw.items()}
    except Exception:
        return {}


@dataclass
class Strategy:
    spec: StrategySpec
    chain: str
    id: str = field(default_factory=lambda: f"S{next(_id_counter)}")
    status: str = "active"        # active | waiting | done | cancelled | error
    phase: str = ""               # waiting_trigger | streaming | ""
    paused: bool = False
    accrued_usd: float = 0.0
    spent_usd: float = 0.0
    fills: int = 0
    qty_bought: float = 0.0
    qty_sold: float = 0.0
    usd_bought: float = 0.0
    usd_sold: float = 0.0
    created: float = field(default_factory=time.time)
    first_fill: float = 0.0
    last_fill: float = 0.0
    last_tick: float = field(default_factory=time.time)
    error: str = ""
    blocked_reason: str = ""
    peak_price: float = 0.0       # trailing stop: highest price seen
    prev_price: float = 0.0       # grid: last tick price (0 = re-anchor)
    grid_lots: dict = field(default_factory=dict)  # level_index -> {qty, usd}
    _last_block_log: float = 0.0  # rate-limit blocked log lines (not persisted)

    # ---- per-strategy analytics -------------------------------------

    def vwap(self) -> float:
        qty = self.qty_bought + self.qty_sold
        return (self.usd_bought + self.usd_sold) / qty if qty > 1e-12 else 0.0

    def pnl(self, price: float | None) -> float:
        """Mark-to-market attribution: net USD flow + net inventory at price."""
        if price is None:
            return 0.0
        usd_net = self.usd_sold - self.usd_bought
        qty_net = self.qty_bought - self.qty_sold
        return usd_net + qty_net * price

    def realized_rate(self) -> float:
        """Actual $/min achieved since first fill (0 if <1 fill)."""
        if not self.first_fill or self.last_fill <= self.first_fill:
            return 0.0
        mins = (self.last_fill - self.first_fill) / 60.0
        return self.spent_usd / mins if mins > 0 else 0.0

    def trail_trigger(self) -> float | None:
        if self.spec.kind != "trailing_stop" or self.peak_price <= 0:
            return None
        return self.peak_price * (1.0 - self.spec.trail_pct / 100.0)

    def trail_distance_pct(self, price: float | None) -> float | None:
        """How far price is above the trigger, as % of peak. 0 = at trigger."""
        trig = self.trail_trigger()
        if trig is None or price is None or self.peak_price <= 0:
            return None
        return (price - trig) / self.peak_price * 100.0

    def as_dict(self, price: float | None) -> dict:
        cap = self.spec.total_cap_usd or 0
        deployed = sum(float(lot.get("usd", 0)) for lot in self.grid_lots.values())
        d = {
            "id": self.id,
            "text": self.spec.describe(),
            "raw": self.spec.raw_text,
            "kind": self.spec.kind,
            "side": self.spec.side,
            "token": self.spec.token,
            "chain": self.chain,
            "status": self.status,
            "phase": self.phase,
            "paused": self.paused,
            "spent_usd": self.spent_usd,
            "cap_usd": cap,
            "progress": (self.spent_usd / cap) if cap else None,
            "fills": self.fills,
            "price": price,
            "vwap": self.vwap(),
            "pnl": self.pnl(price),
            "rate_target": self.spec.rate_usd_per_min,
            "rate_actual": self.realized_rate(),
            "created": self.created,
            "last_fill": self.last_fill,
            "error": self.error,
            "blocked_reason": self.blocked_reason,
            "notes": list(self.spec.notes),
            "trail_pct": self.spec.trail_pct if self.spec.kind == "trailing_stop" else None,
            "peak_price": self.peak_price if self.spec.kind == "trailing_stop" else None,
            "trail_trigger": self.trail_trigger(),
            "trail_distance_pct": self.trail_distance_pct(price),
            "grid_lots_held": len(self.grid_lots) if self.spec.kind == "grid" else None,
            "grid_levels": self.spec.grid_levels if self.spec.kind == "grid" else None,
            "grid_deployed_usd": deployed if self.spec.kind == "grid" else None,
            "grid_lower": self.spec.grid_lower if self.spec.kind == "grid" else None,
            "grid_upper": self.spec.grid_upper if self.spec.kind == "grid" else None,
            "usd_per_level": self.spec.usd_per_level if self.spec.kind == "grid" else None,
        }
        return d


class Engine:
    def __init__(self, cfg: BotConfig, book: PriceBook, portfolio: Portfolio,
                 paper_feed: PaperFeed | None, live_clients: dict,
                 store: Store | None = None) -> None:
        self.cfg = cfg
        self.book = book
        self.portfolio = portfolio
        self.paper_feed = paper_feed
        self.live_clients = live_clients   # chain_key -> ChainClient
        self.store = store
        self.strategies: list[Strategy] = []
        self.paused = False
        self.events: list[dict] = []
        if store is not None:
            store.set_on_error(lambda msg: self.log(msg))

    def _persist_strategy(self, s: Strategy) -> None:
        if self.store:
            self.store.save_strategy(s)

    def _persist_trade(self, trade: Trade) -> None:
        if self.store:
            self.store.save_trade(trade)

    def _persist_meta(self) -> None:
        if self.store:
            self.store.save_portfolio_meta(self.portfolio, self.paused)

    def _persist_equity(self) -> None:
        if self.store and self.portfolio.equity_history:
            self.store.save_equity_point(self.portfolio.equity_history[-1])

    def restore(self) -> None:
        """Reload portfolio + strategies from the store (no-op if empty)."""
        if not self.store or not self.store.has_data():
            self._persist_meta()
            return
        from .store import spec_from_json

        start_s = self.store.get_meta("start_equity")
        start = float(start_s) if start_s is not None else self.portfolio.start_equity
        self.portfolio.cash_usd = start
        self.portfolio.start_equity = start
        self.portfolio.positions.clear()
        self.portfolio.trades.clear()
        self.portfolio.equity_history.clear()

        for trade in self.store.load_trades():
            self.portfolio.record_fill(trade)

        self.portfolio.equity_history = self.store.load_equity()
        if len(self.portfolio.equity_history) > 2000:
            del self.portfolio.equity_history[:-2000]

        paused_meta = self.store.get_meta("engine_paused", "0")
        self.paused = paused_meta == "1"

        now = time.time()
        live = self.cfg.mode == "live"
        restored = 0
        for row in self.store.load_strategy_rows():
            s = Strategy(
                spec=spec_from_json(row["spec_json"]),
                chain=row["chain"],
                id=row["id"],
                status=row["status"],
                phase=row["phase"],
                paused=bool(row["paused"]),
                accrued_usd=0.0,  # never catch up for downtime
                spent_usd=row["spent_usd"],
                fills=row["fills"],
                qty_bought=row["qty_bought"],
                qty_sold=row["qty_sold"],
                usd_bought=row["usd_bought"],
                usd_sold=row["usd_sold"],
                created=row["created"],
                first_fill=row["first_fill"],
                last_fill=row["last_fill"],
                last_tick=now,
                error=row["error"] or "",
                blocked_reason=(row["blocked_reason"] if "blocked_reason" in row.keys()
                                else "") or "",
                peak_price=float(row["peak_price"]) if (
                    "peak_price" in row.keys() and row["peak_price"] is not None
                ) else 0.0,
                prev_price=0.0,  # re-anchor on first post-restore tick
                grid_lots=_loads_grid_lots(row),
            )
            if s.status in ("active", "waiting"):
                if live:
                    s.paused = True
                    self.log(f"{s.id} resumed paused after restart (live mode)")
                else:
                    s.paused = False
                restored += 1
            self.strategies.append(s)

        seed_id_counter(self.store.max_strategy_num() + 1)
        if restored:
            self.log(f"restored {restored} active/waiting strategies from {self.store.path}")
        self.log(f"restored {len(self.portfolio.trades)} trades, "
                 f"{len(self.strategies)} strategies from {self.store.path}")

    # ------------------------------------------------------------- intake

    def submit(self, spec: StrategySpec) -> Strategy | None:
        if spec.kind == "cancel":
            n = 0
            for s in self.strategies:
                if s.status in ("active", "waiting"):
                    self._note_grid_cancel(s)
                    s.status = "cancelled"
                    self._persist_strategy(s)
                    n += 1
            self.log(f"cancelled {n} strategies")
            return None
        if spec.kind == "pause":
            self.paused = True
            self._persist_meta()
            self.log("engine paused")
            return None
        if spec.kind == "resume":
            self.paused = False
            self._persist_meta()
            self.log("engine resumed")
            return None

        chain = spec.chain or self.cfg.default_chain
        if chain not in self.cfg.chains:
            raise ValueError(f"unknown chain '{chain}'")
        if spec.token and spec.token not in self.cfg.chains[chain].tokens:
            known = ", ".join(self.cfg.chains[chain].tokens) or "none configured"
            raise ValueError(f"unknown token '{spec.token}' on {chain} (configured: {known})")
        if spec.kind == "grid" and spec.usd_per_level < self.cfg.min_slice_usd:
            raise ValueError(
                f"grid usd per level (${spec.usd_per_level:g}) is below "
                f"min_slice_usd (${self.cfg.min_slice_usd:g})")

        s = Strategy(spec=spec, chain=chain)
        if spec.kind in ("rate", "triggered_rate") and not spec.total_cap_usd:
            dcap = self.cfg.risk.default_cap_usd_for_uncapped
            if dcap > 0:
                spec.total_cap_usd = dcap
                note = f"default cap ${dcap:g} applied"
                spec.notes.append(note)
                self.log(f"{s.id} {note}")
        if spec.kind in ("triggered_rate", "stop", "limit"):
            s.phase = "waiting_trigger"
            s.status = "waiting"
        elif spec.kind == "rate":
            s.phase = "streaming"
        elif spec.kind == "trailing_stop":
            s.status = "active"
            s.peak_price = 0.0  # set on first observed tick price
        elif spec.kind == "grid":
            s.status = "active"
            s.prev_price = 0.0
            s.grid_lots = {}
        self.strategies.append(s)
        self._persist_strategy(s)
        self.log(f"{s.id} accepted: {spec.describe()}")
        return s

    def _note_grid_cancel(self, s: Strategy) -> None:
        if s.spec.kind != "grid" or not s.grid_lots:
            return
        deployed = sum(float(lot.get("usd", 0)) for lot in s.grid_lots.values())
        self.log(f"{s.id} cancelled with {len(s.grid_lots)} lots remaining "
                 f"(${deployed:,.0f} deployed; inventory left untouched)")

    def cancel(self, sid: str) -> bool:
        for s in self.strategies:
            if s.id == sid and s.status in ("active", "waiting"):
                self._note_grid_cancel(s)
                s.status = "cancelled"
                self._persist_strategy(s)
                self.log(f"{s.id} cancelled")
                return True
        return False

    def set_paused(self, sid: str, paused: bool) -> bool:
        for s in self.strategies:
            if s.id == sid and s.status in ("active", "waiting"):
                s.paused = paused
                self._persist_strategy(s)
                self.log(f"{s.id} {'paused' if paused else 'resumed'}")
                return True
        return False

    def find(self, sid: str) -> Strategy | None:
        return next((s for s in self.strategies if s.id == sid), None)

    def strategy_detail(self, sid: str) -> dict | None:
        """Everything the expanded card shows: stats + fills + cumulative series."""
        s = self.find(sid)
        if s is None:
            return None
        price = self.book.price(s.chain, s.spec.token)
        trades = [t for t in self.portfolio.trades if t.strategy_id == sid]
        slips = [t.slippage_bps for t in trades if t.ref_price]
        cum, cum_series = 0.0, []
        for t in trades:
            cum += t.usd
            cum_series.append({"time": t.ts, "value": cum})
        d = s.as_dict(price)
        elapsed = (time.time() - s.created)
        d.update({
            "trigger": s.spec.trigger.describe() if s.spec.trigger else None,
            "condition": s.spec.condition.describe() if s.spec.condition else None,
            "limit_price": s.spec.limit_price,
            "gate_open": (s.spec.condition.check(price)
                          if (s.spec.condition and price is not None) else None),
            "elapsed_s": elapsed,
            "qty_bought": s.qty_bought,
            "qty_sold": s.qty_sold,
            "usd_bought": s.usd_bought,
            "usd_sold": s.usd_sold,
            "avg_slippage_bps": sum(slips) / len(slips) if slips else 0.0,
            "avg_fill_usd": s.spent_usd / s.fills if s.fills else 0.0,
            "cum_series": cum_series,
            "fills_detail": [t.as_dict() for t in trades][::-1],
            "explorer": self.cfg.chains[s.chain].explorer if s.chain in self.cfg.chains else "",
            "prev_price": s.prev_price if s.spec.kind == "grid" else None,
            "grid_levels_view": self._grid_levels_view(s) if s.spec.kind == "grid" else None,
        })
        return d

    def _grid_levels_view(self, s: Strategy) -> list[dict]:
        levels = grid_level_prices(s.spec.grid_lower, s.spec.grid_upper, s.spec.grid_levels)
        out = []
        for i, px in enumerate(levels):
            lot = s.grid_lots.get(i)
            out.append({
                "index": i,
                "price": px,
                "holding_qty": float(lot["qty"]) if lot else 0.0,
                "holding_usd": float(lot["usd"]) if lot else 0.0,
            })
        return out

    def log(self, msg: str) -> None:
        self.events.append({"ts": time.time(), "msg": msg})
        if len(self.events) > 400:
            del self.events[:100]

    # ------------------------------------------------------------- loop

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as e:  # engine must never die
                self.log(f"engine error: {e}")
            self.portfolio.mark(self.book.price)
            self._persist_equity()
            await asyncio.sleep(self.cfg.tick_seconds)

    def tick(self) -> None:
        now = time.time()
        for s in self.strategies:
            if s.status not in ("active", "waiting"):
                continue
            dt = min(now - s.last_tick, 10.0)
            s.last_tick = now
            if self.paused or s.paused:
                continue
            price = self.book.price(s.chain, s.spec.token)
            if price is None:
                continue
            try:
                prev_status, prev_phase = s.status, s.phase
                prev_peak, prev_pp = s.peak_price, s.prev_price
                prev_lots = len(s.grid_lots)
                self._tick_strategy(s, price, dt)
                if (s.status != prev_status or s.phase != prev_phase
                        or s.peak_price != prev_peak
                        or s.prev_price != prev_pp
                        or len(s.grid_lots) != prev_lots):
                    self._persist_strategy(s)
            except Exception as e:
                s.status = "error"
                s.error = str(e)
                self._persist_strategy(s)
                self.log(f"{s.id} error: {e}")

    def _tick_strategy(self, s: Strategy, price: float, dt: float) -> None:
        spec = s.spec

        if spec.kind == "market":
            if self._fill_market(s, price):
                s.status = "done"
            return

        if spec.kind == "limit":
            hit = price <= spec.limit_price if spec.side == "buy" else price >= spec.limit_price
            if hit and self._fill_market(s, price):
                s.status = "done"
            return

        if spec.kind == "stop":
            if spec.trigger and spec.trigger.check(price):
                if self._fill_market(s, price):
                    s.status = "done"
                    self.log(f"{s.id} stop fired at ${price:.6f}")
            return

        if spec.kind == "trailing_stop":
            if s.peak_price <= 0:
                s.peak_price = price
            else:
                s.peak_price = max(s.peak_price, price)
            trig = s.peak_price * (1.0 - spec.trail_pct / 100.0)
            if price <= trig + 1e-12:
                if self._fill_market(s, price):
                    s.status = "done"
                    self.log(f"{s.id} trailing stop fired at ${price:.6f} "
                             f"(peak ${s.peak_price:.6f})")
            return

        if spec.kind == "grid":
            self._tick_grid(s, price)
            return

        if spec.kind == "triggered_rate" and s.phase == "waiting_trigger":
            if spec.trigger and spec.trigger.check(price):
                s.phase = "streaming"
                s.status = "active"
                self.log(f"{s.id} trigger hit at ${price:.6f}")
            else:
                return

        if s.phase == "streaming":
            # one-shot / initial trigger notional (retries if risk-blocked)
            if (spec.kind == "triggered_rate" and spec.usd_amount > 0
                    and s.fills == 0 and s.spent_usd < 1e-9):
                if not self._child_order(s, spec.usd_amount, price):
                    return
            if not spec.rate_usd_per_min:
                s.status = "done"
                return
            if spec.condition and not spec.condition.check(price):
                return  # gated: accrue nothing while condition is false
            s.accrued_usd += spec.rate_usd_per_min * (dt / 60.0)
            cap = spec.total_cap_usd
            remaining = (cap - s.spent_usd) if cap else float("inf")
            slice_usd = min(max(self.cfg.min_slice_usd, spec.rate_usd_per_min / 6), remaining)
            if s.blocked_reason:
                s.accrued_usd = min(s.accrued_usd, slice_usd)
            if s.accrued_usd >= slice_usd and remaining > 0:
                amt = min(s.accrued_usd, remaining)
                if self._child_order(s, amt, price):
                    s.accrued_usd = 0.0
                else:
                    s.accrued_usd = min(s.accrued_usd, slice_usd)
            if cap and s.spent_usd >= cap - 1e-9:
                s.status = "done"
                self.log(f"{s.id} completed cap of ${cap:g}")

    def _tick_grid(self, s: Strategy, price: float) -> None:
        spec = s.spec
        levels = grid_level_prices(spec.grid_lower, spec.grid_upper, spec.grid_levels)
        if s.prev_price <= 0:
            s.prev_price = price
            return
        prev = s.prev_price
        lo, hi = spec.grid_lower, spec.grid_upper
        in_band = lo - 1e-12 <= price <= hi + 1e-12

        # DOWNWARD crossings: highest buyable level first (skip top — sell-only)
        if price < prev:
            top = len(levels) - 1
            for i in range(top - 1, -1, -1):
                lv = levels[i]
                if prev > lv >= price and i not in s.grid_lots and in_band:
                    if self._grid_buy(s, spec.usd_per_level, price):
                        trade = self.portfolio.trades[-1]
                        s.grid_lots[i] = {"qty": trade.qty, "usd": trade.usd}
                        self.log(f"{s.id} grid buy L{i} @ ${lv:g} "
                                 f"(${spec.usd_per_level:g})")

        # UPWARD crossings: lowest level first; sell lot at j-1 when crossing j
        if price > prev:
            for j in range(1, len(levels)):
                lv = levels[j]
                if prev < lv <= price and (j - 1) in s.grid_lots:
                    lot = s.grid_lots[j - 1]
                    if self._grid_sell(s, float(lot["qty"]), price):
                        del s.grid_lots[j - 1]
                        self.log(f"{s.id} grid sell L{j - 1}→L{j} @ ${lv:g}")

        s.prev_price = price

    def _grid_buy(self, s: Strategy, usd: float, price: float) -> bool:
        spec = s.spec
        old_side, old_usd, old_ta, old_sa = (
            spec.side, spec.usd_amount, spec.token_amount, spec.sell_all)
        try:
            spec.side = "buy"
            spec.usd_amount = usd
            spec.token_amount = None
            spec.sell_all = False
            return self._child_order(s, usd, price)
        finally:
            spec.side, spec.usd_amount = old_side, old_usd
            spec.token_amount, spec.sell_all = old_ta, old_sa

    def _grid_sell(self, s: Strategy, qty: float, price: float) -> bool:
        spec = s.spec
        old_side, old_usd, old_ta, old_sa = (
            spec.side, spec.usd_amount, spec.token_amount, spec.sell_all)
        try:
            spec.side = "sell"
            spec.token_amount = qty
            spec.sell_all = False
            spec.usd_amount = 0.0
            return self._fill_market(s, price)
        finally:
            spec.side, spec.usd_amount = old_side, old_usd
            spec.token_amount, spec.sell_all = old_ta, old_sa

    # ------------------------------------------------------------- fills

    def _fill_market(self, s: Strategy, price: float) -> bool:
        spec = s.spec
        if spec.side == "sell" and (spec.sell_all or spec.token_amount is not None):
            qty = (self.portfolio.qty(s.chain, spec.token) if spec.sell_all
                   else spec.token_amount)
            usd = qty * price
        else:
            usd = spec.usd_amount
        if usd <= 0:
            raise ValueError("nothing to trade (zero size)")
        return self._child_order(s, usd, price)

    def _risk_block_reason(self, s: Strategy, usd: float, ref_price: float) -> str | None:
        risk = self.cfg.risk
        if risk.max_open_notional_usd_per_token > 0 and s.spec.side == "buy":
            open_n = self.portfolio.qty(s.chain, s.spec.token) * ref_price
            if open_n + usd > risk.max_open_notional_usd_per_token + 1e-9:
                return (f"open notional cap "
                        f"${risk.max_open_notional_usd_per_token:g} for {s.spec.token}")
        if risk.max_daily_spend_usd > 0:
            # Protective exits reduce exposure — never block them with the daily cap.
            protective = (s.spec.kind in ("stop", "trailing_stop")
                          or (s.spec.side == "sell" and s.spec.sell_all))
            if not protective:
                cutoff = time.time() - 86_400
                spent_24h = sum(t.usd for t in self.portfolio.trades if t.ts >= cutoff)
                if spent_24h + usd > risk.max_daily_spend_usd + 1e-9:
                    return f"daily spend cap ${risk.max_daily_spend_usd:g}"
        return None

    def _note_blocked(self, s: Strategy, reason: str) -> None:
        s.blocked_reason = reason
        now = time.time()
        if now - s._last_block_log >= 60.0:
            self.log(f"{s.id} blocked: {reason}")
            s._last_block_log = now
        self._persist_strategy(s)

    def _child_order(self, s: Strategy, usd: float, ref_price: float) -> bool:
        reason = self._risk_block_reason(s, usd, ref_price)
        if reason:
            self._note_blocked(s, reason)
            return False
        spec = s.spec
        if self.cfg.mode == "live":
            trade = self._execute_live(s, usd, ref_price)
        else:
            trade = self._execute_paper(s, usd, ref_price)
        self.portfolio.record_fill(trade)
        self._persist_trade(trade)
        s.spent_usd += usd
        s.fills += 1
        if trade.side == "buy":
            s.qty_bought += trade.qty
            s.usd_bought += trade.usd
        else:
            s.qty_sold += trade.qty
            s.usd_sold += trade.usd
        if not s.first_fill:
            s.first_fill = trade.ts
        s.last_fill = trade.ts
        if s.blocked_reason:
            s.blocked_reason = ""
        self._persist_strategy(s)
        self._persist_meta()
        self.log(f"{s.id} {spec.side} ${usd:,.2f} {spec.token} @ ${trade.price:.6f}"
                 + (f" tx {trade.tx_hash[:10]}…" if trade.tx_hash else ""))
        return True
    def _execute_paper(self, s: Strategy, usd: float, ref_price: float) -> Trade:
        spec = s.spec
        fee = self.cfg.paper_fee_bps
        impact = self.cfg.paper_impact_bps_per_1k * (usd / 1000.0)
        adj = (fee + impact) / 10_000
        px = ref_price * (1 + adj) if spec.side == "buy" else ref_price * (1 - adj)
        qty = usd / px
        if spec.side == "buy" and usd > self.portfolio.cash_usd:
            raise ValueError(f"insufficient paper cash (${self.portfolio.cash_usd:,.2f})")
        if self.paper_feed:
            self.paper_feed.apply_impact(
                s.chain, spec.token, impact if spec.side == "buy" else -impact)
        return Trade(ts=time.time(), chain=s.chain, token=spec.token,
                     side=spec.side, usd=usd, qty=qty, price=px,
                     ref_price=ref_price, strategy_id=s.id, mode="paper")

    def _execute_live(self, s: Strategy, usd: float, ref_price: float) -> Trade:
        spec = s.spec
        client = self.live_clients.get(s.chain)
        chain_cfg = self.cfg.chains[s.chain]
        tok = chain_cfg.tokens[spec.token]
        if client is None or not client.account:
            raise RuntimeError("live mode not configured: set the private key env "
                               "var and router/quote_token addresses in config.yaml")
        slip = self.cfg.max_slippage_bps / 10_000
        if spec.side == "buy":
            amount_in_raw = int(usd * 10 ** chain_cfg.quote_decimals)
            expected_out = usd / ref_price
            min_out_raw = int(expected_out * (1 - slip) * 10 ** tok.decimals)
            res = client.swap(chain_cfg.quote_token, tok.address, amount_in_raw,
                              tok.pool_fee, min_out_raw,
                              out_decimals=tok.decimals,
                              in_decimals=chain_cfg.quote_decimals,
                              quoted_out=expected_out)
            qty, px = res.amount_out, usd / res.amount_out
        else:
            qty = usd / ref_price
            amount_in_raw = int(qty * 10 ** tok.decimals)
            expected_out = usd
            min_out_raw = int(usd * (1 - slip) * 10 ** chain_cfg.quote_decimals)
            res = client.swap(tok.address, chain_cfg.quote_token, amount_in_raw,
                              tok.pool_fee, min_out_raw,
                              out_decimals=chain_cfg.quote_decimals,
                              in_decimals=tok.decimals,
                              quoted_out=expected_out)
            px = res.amount_out / qty if qty else ref_price
            usd = res.amount_out
        return Trade(ts=time.time(), chain=s.chain, token=spec.token,
                     side=spec.side, usd=usd, qty=qty, price=px,
                     ref_price=ref_price, strategy_id=s.id,
                     tx_hash=res.tx_hash, mode="live",
                     quoted_price=ref_price, estimated=res.estimated)

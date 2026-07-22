"""Portfolio accounting: balances, trade ledger, PnL and execution stats."""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Trade:
    ts: float
    chain: str
    token: str
    side: str            # buy | sell
    usd: float           # notional in USD
    qty: float           # token units
    price: float         # fill price
    ref_price: float     # feed price at decision time (for slippage stats)
    strategy_id: str
    tx_hash: str = ""
    mode: str = "paper"
    quoted_price: float = 0.0
    estimated: bool = False

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["slippage_bps"] = self.slippage_bps
        return d

    @property
    def slippage_bps(self) -> float:
        if not self.ref_price:
            return 0.0
        raw = (self.price - self.ref_price) / self.ref_price * 10_000
        return raw if self.side == "buy" else -raw  # positive = cost


@dataclass
class Position:
    qty: float = 0.0
    cost_usd: float = 0.0      # cost basis of current holding
    realized_pnl: float = 0.0

    @property
    def avg_price(self) -> float:
        return self.cost_usd / self.qty if self.qty > 1e-12 else 0.0


@dataclass
class Portfolio:
    cash_usd: float
    positions: dict[tuple[str, str], Position] = field(default_factory=lambda: defaultdict(Position))
    trades: list[Trade] = field(default_factory=list)
    equity_history: list[dict] = field(default_factory=list)
    start_equity: float = 0.0

    def __post_init__(self) -> None:
        self.start_equity = self.cash_usd

    # ------------------------------------------------------------- fills

    def record_fill(self, trade: Trade) -> None:
        pos = self.positions[(trade.chain, trade.token)]
        if trade.side == "buy":
            self.cash_usd -= trade.usd
            pos.qty += trade.qty
            pos.cost_usd += trade.usd
        else:
            self.cash_usd += trade.usd
            if pos.qty > 1e-12:
                avg = pos.avg_price
                sold = min(trade.qty, pos.qty)
                pos.realized_pnl += (trade.price - avg) * sold
                pos.cost_usd -= avg * sold
                pos.qty -= sold
                if pos.qty <= 1e-12:
                    pos.qty, pos.cost_usd = 0.0, 0.0
            else:
                pos.qty -= trade.qty  # short in paper mode
        self.trades.append(trade)

    def qty(self, chain: str, token: str) -> float:
        return self.positions[(chain, token.upper())].qty

    # ------------------------------------------------------------- marks

    def equity(self, price_of) -> float:
        total = self.cash_usd
        for (chain, token), pos in self.positions.items():
            px = price_of(chain, token) or pos.avg_price
            total += pos.qty * px
        return total

    def mark(self, price_of) -> None:
        eq = self.equity(price_of)
        now = time.time()
        if self.equity_history and now - self.equity_history[-1]["time"] < 5:
            self.equity_history[-1]["value"] = eq
        else:
            self.equity_history.append({"time": now, "value": eq})
        if len(self.equity_history) > 2000:
            del self.equity_history[:500]

    # ------------------------------------------------------------- stats

    def stats(self, price_of) -> dict:
        eq = self.equity(price_of)
        realized = sum(p.realized_pnl for p in self.positions.values())
        unrealized = 0.0
        pos_view = []
        for (chain, token), pos in self.positions.items():
            if abs(pos.qty) < 1e-12 and abs(pos.realized_pnl) < 1e-9:
                continue
            px = price_of(chain, token) or pos.avg_price
            upnl = (px - pos.avg_price) * pos.qty
            unrealized += upnl
            pos_view.append({
                "chain": chain, "token": token, "qty": pos.qty,
                "avg_price": pos.avg_price, "price": px,
                "value": pos.qty * px, "unrealized": upnl,
                "realized": pos.realized_pnl,
            })
        buys = [t for t in self.trades if t.side == "buy"]
        sells = [t for t in self.trades if t.side == "sell"]
        slippages = [t.slippage_bps for t in self.trades if t.ref_price]
        return {
            "equity": eq,
            "cash": self.cash_usd,
            "pnl_total": eq - self.start_equity,
            "pnl_pct": (eq / self.start_equity - 1) * 100 if self.start_equity else 0,
            "realized": realized,
            "unrealized": unrealized,
            "trades": len(self.trades),
            "buy_volume": sum(t.usd for t in buys),
            "sell_volume": sum(t.usd for t in sells),
            "avg_slippage_bps": sum(slippages) / len(slippages) if slippages else 0.0,
            "positions": pos_view,
        }

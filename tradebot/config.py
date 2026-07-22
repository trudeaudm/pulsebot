"""Configuration loading for Pulse trading bot."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = ["config.yaml", "config.example.yaml"]


@dataclass
class TokenConfig:
    symbol: str
    address: str = ""
    decimals: int = 18
    pool_fee: int = 3000          # Uniswap v3 fee tier for the token/quote pool
    dexscreener_pair: str = ""    # optional pair address for price feed
    paper_start_price: float = 0.12
    paper_volatility: float = 0.004  # per-tick lognormal sigma for the simulator


@dataclass
class ChainConfig:
    name: str
    chain_id: int
    rpc_url: str
    explorer: str = ""
    router: str = ""              # Uniswap v3 SwapRouter02-compatible router
    quoter: str = ""              # Quoter V2
    quote_token: str = ""         # address of USD stable used as the dollar leg
    quote_symbol: str = "USDC"
    quote_decimals: int = 6
    dexscreener_slug: str = ""    # dexscreener chain slug, e.g. "base"
    tokens: dict[str, TokenConfig] = field(default_factory=dict)


@dataclass
class BotConfig:
    mode: str = "paper"                   # "paper" or "live"
    default_chain: str = "base"
    tick_seconds: float = 1.0
    min_slice_usd: float = 10.0           # smallest child order the engine will send
    max_slippage_bps: int = 100           # live-mode swap slippage tolerance
    paper_cash_usd: float = 10_000.0
    paper_fee_bps: int = 30               # simulated pool fee
    paper_impact_bps_per_1k: float = 8.0  # simulated price impact per $1k notional
    anthropic_parser: bool = False        # use Claude API to parse commands it can't match
    host: str = "127.0.0.1"
    port: int = 8420
    private_key_env: str = "PULSE_PRIVATE_KEY"
    db_path: str = "pulse.db"
    chains: dict[str, ChainConfig] = field(default_factory=dict)

    @property
    def private_key(self) -> str | None:
        return os.environ.get(self.private_key_env)


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | None = None) -> BotConfig:
    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path)
    else:
        for candidate in DEFAULT_CONFIG_PATHS:
            if Path(candidate).exists():
                cfg_path = Path(candidate)
                break
    raw = _load_yaml(cfg_path) if cfg_path else {}

    chains: dict[str, ChainConfig] = {}
    for key, c in (raw.get("chains") or {}).items():
        tokens = {
            t["symbol"].upper(): TokenConfig(
                symbol=t["symbol"].upper(),
                address=t.get("address", ""),
                decimals=int(t.get("decimals", 18)),
                pool_fee=int(t.get("pool_fee", 3000)),
                dexscreener_pair=t.get("dexscreener_pair", ""),
                paper_start_price=float(t.get("paper_start_price", 0.12)),
                paper_volatility=float(t.get("paper_volatility", 0.004)),
            )
            for t in (c.get("tokens") or [])
        }
        chains[key] = ChainConfig(
            name=c.get("name", key),
            chain_id=int(c["chain_id"]),
            rpc_url=c.get("rpc_url", ""),
            explorer=c.get("explorer", ""),
            router=c.get("router", ""),
            quoter=c.get("quoter", ""),
            quote_token=c.get("quote_token", ""),
            quote_symbol=c.get("quote_symbol", "USDC"),
            quote_decimals=int(c.get("quote_decimals", 6)),
            dexscreener_slug=c.get("dexscreener_slug", ""),
            tokens=tokens,
        )

    bot = raw.get("bot") or {}
    return BotConfig(
        mode=bot.get("mode", "paper"),
        default_chain=bot.get("default_chain", next(iter(chains), "base")),
        tick_seconds=float(bot.get("tick_seconds", 1.0)),
        min_slice_usd=float(bot.get("min_slice_usd", 10.0)),
        max_slippage_bps=int(bot.get("max_slippage_bps", 100)),
        paper_cash_usd=float(bot.get("paper_cash_usd", 10_000.0)),
        paper_fee_bps=int(bot.get("paper_fee_bps", 30)),
        paper_impact_bps_per_1k=float(bot.get("paper_impact_bps_per_1k", 8.0)),
        anthropic_parser=bool(bot.get("anthropic_parser", False)),
        host=bot.get("host", "127.0.0.1"),
        port=int(bot.get("port", 8420)),
        private_key_env=bot.get("private_key_env", "PULSE_PRIVATE_KEY"),
        db_path=bot.get("db_path", "pulse.db"),
        chains=chains,
    )

"""Configuration loading for Pulse trading bot."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = ["config.yaml", "config.example.yaml"]
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class TokenConfig:
    symbol: str
    address: str = ""
    decimals: int = 18
    pool_fee: int = 3000          # Uniswap v3 fee tier (token<->USDC or token<->WETH)
    pool_type: str = "v3"         # "v3" | "v2"
    route: str = "direct"         # "direct" | "weth" (token<->WETH<->USDC)
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
    weth_token: str = ""          # wrapped native for multi-hop routes
    v2_router: str = ""           # Uniswap v2-style router
    weth_usdc_fee: int = 500      # v3 fee tier for WETH<->USDC hop
    # Variable name if rpc_url was ${VAR}; never log the URL (may embed API keys).
    rpc_env_var: str = ""
    tokens: dict[str, TokenConfig] = field(default_factory=dict)


@dataclass
class RiskConfig:
    max_open_notional_usd_per_token: float = 0.0  # 0 = unlimited
    max_daily_spend_usd: float = 0.0              # trailing 24h, 0 = unlimited
    default_cap_usd_for_uncapped: float = 0.0     # 0 = leave uncapped


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
    risk: RiskConfig = field(default_factory=RiskConfig)
    chains: dict[str, ChainConfig] = field(default_factory=dict)

    @property
    def private_key(self) -> str | None:
        return os.environ.get(self.private_key_env)


def load_dotenv(path: str | Path | None = None) -> None:
    """Load KEY=VALUE pairs from .env into os.environ if not already set.

    Real environment variables always win. No dependency — stdlib only.
    """
    p = Path(path) if path is not None else Path(".env")
    if not p.is_file():
        return
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        os.environ[key] = val


def _expand_env(value: str, *, mode: str, required_in_live: bool = False) -> str:
    """Expand ${VAR} placeholders. Live mode fails if a required var is unset."""
    if not isinstance(value, str) or "${" not in value:
        return value

    missing: list[str] = []

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in os.environ:
            return os.environ[name]
        missing.append(name)
        return ""

    out = _ENV_VAR_RE.sub(repl, value)
    if missing and mode == "live" and required_in_live:
        names = ", ".join(dict.fromkeys(missing))
        raise ValueError(
            f"live mode requires environment variable(s) {names} "
            f"(set in .env or the process environment)")
    return out


def _rpc_env_var_name(raw: str) -> str:
    """If rpc_url is exactly ${VAR}, return VAR; else empty."""
    m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", (raw or "").strip())
    return m.group(1) if m else ""


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_config(path: str | None = None, *, dotenv_path: str | Path | None = None
                ) -> BotConfig:
    load_dotenv(dotenv_path)

    cfg_path: Path | None = None
    if path:
        cfg_path = Path(path)
    else:
        for candidate in DEFAULT_CONFIG_PATHS:
            if Path(candidate).exists():
                cfg_path = Path(candidate)
                break
    raw = _load_yaml(cfg_path) if cfg_path else {}

    bot = raw.get("bot") or {}
    mode = str(bot.get("mode", "paper")).lower()

    chains: dict[str, ChainConfig] = {}
    for key, c in (raw.get("chains") or {}).items():
        tokens = {}
        for t in (c.get("tokens") or []):
            pool_type = str(t.get("pool_type") or "v3").lower()
            route = str(t.get("route") or "direct").lower()
            if pool_type not in ("v2", "v3"):
                raise ValueError(
                    f"token {t.get('symbol')}: pool_type must be 'v2' or 'v3'")
            if route not in ("direct", "weth"):
                raise ValueError(
                    f"token {t.get('symbol')}: route must be 'direct' or 'weth'")
            sym = t["symbol"].upper()
            tokens[sym] = TokenConfig(
                symbol=sym,
                address=t.get("address", ""),
                decimals=int(t.get("decimals", 18)),
                pool_fee=int(t.get("pool_fee", 3000)),
                pool_type=pool_type,
                route=route,
                dexscreener_pair=t.get("dexscreener_pair", ""),
                paper_start_price=float(t.get("paper_start_price", 0.12)),
                paper_volatility=float(t.get("paper_volatility", 0.004)),
            )
        rpc_raw = c.get("rpc_url", "") or ""
        rpc_var = _rpc_env_var_name(rpc_raw)
        rpc_url = _expand_env(rpc_raw, mode=mode, required_in_live=True)
        chains[key] = ChainConfig(
            name=c.get("name", key),
            chain_id=int(c["chain_id"]),
            rpc_url=rpc_url,
            explorer=c.get("explorer", ""),
            router=c.get("router", ""),
            quoter=c.get("quoter", ""),
            quote_token=c.get("quote_token", ""),
            quote_symbol=c.get("quote_symbol", "USDC"),
            quote_decimals=int(c.get("quote_decimals", 6)),
            dexscreener_slug=c.get("dexscreener_slug", ""),
            weth_token=c.get("weth_token", "") or "",
            v2_router=c.get("v2_router", "") or "",
            weth_usdc_fee=int(c.get("weth_usdc_fee", 500) or 500),
            rpc_env_var=rpc_var,
            tokens=tokens,
        )

    risk_raw = bot.get("risk") or {}
    risk = RiskConfig(
        max_open_notional_usd_per_token=float(
            risk_raw.get("max_open_notional_usd_per_token", 0) or 0),
        max_daily_spend_usd=float(risk_raw.get("max_daily_spend_usd", 0) or 0),
        default_cap_usd_for_uncapped=float(
            risk_raw.get("default_cap_usd_for_uncapped", 0) or 0),
    )
    return BotConfig(
        mode=mode,
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
        risk=risk,
        chains=chains,
    )


def describe_rpc_sources(cfg: BotConfig) -> list[str]:
    """Startup lines naming env vars only — never the RPC URL itself."""
    lines = []
    for key, chain in cfg.chains.items():
        if chain.rpc_env_var:
            if chain.rpc_url:
                lines.append(f"{key} RPC: from ${{{chain.rpc_env_var}}}")
            else:
                lines.append(f"{key} RPC: ${{{chain.rpc_env_var}}} unset (empty)")
        elif chain.rpc_url:
            lines.append(f"{key} RPC: literal in config")
        else:
            lines.append(f"{key} RPC: empty")
    return lines

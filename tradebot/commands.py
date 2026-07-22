"""Natural-language command parsing.

Turns free text like:

  "sell TokenA at a rate of $300 per minute while the price is above $0.15"
  "buy $450 of TokenA if the price goes below $0.1 while the price is below
   $0.1 continue to buy at a rate of $100 per minute until you have bought
   a total of $1200"

into a StrategySpec the engine can run. A deterministic clause grammar handles
the common shapes; if `anthropic_parser` is enabled and the grammar can't
match, the text is sent to the Claude API with a JSON schema prompt.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

NUM = r"\$?\s*([0-9][0-9,]*\.?[0-9]*)"


def _f(s: str) -> float:
    return float(s.replace(",", "").replace("$", "").strip())


@dataclass
class Condition:
    """price {above|below} value — evaluated against the live price."""
    op: str      # "above" | "below"
    value: float

    def check(self, price: float) -> bool:
        return price > self.value if self.op == "above" else price < self.value

    def describe(self) -> str:
        return f"price {self.op} ${self.value:g}"


@dataclass
class StrategySpec:
    kind: str                     # market | rate | triggered_rate | limit | stop | trailing_stop | grid | cancel | pause | resume
    side: str = "buy"             # buy | sell
    token: str = ""
    chain: str = ""               # empty -> default chain
    usd_amount: float = 0.0       # one-shot notional (market/limit/trigger leg)
    token_amount: Optional[float] = None  # sell N tokens instead of $ notional
    sell_all: bool = False
    rate_usd_per_min: float = 0.0
    total_cap_usd: Optional[float] = None
    trigger: Optional[Condition] = None   # fires once, starts the strategy
    condition: Optional[Condition] = None # must hold for rate execution to run
    limit_price: Optional[float] = None
    trail_pct: float = 0.0        # trailing stop drawdown percent from peak
    grid_lower: float = 0.0
    grid_upper: float = 0.0
    grid_levels: int = 0
    usd_per_level: float = 0.0
    raw_text: str = ""
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.kind == "cancel":
            return "cancel all strategies"
        if self.kind in ("pause", "resume"):
            return f"{self.kind} engine"
        bits = [self.side, self.token or "?"]
        if self.kind == "market":
            if self.sell_all:
                bits = [f"sell all {self.token}"]
            elif self.token_amount is not None:
                bits = [f"{self.side} {self.token_amount:g} {self.token}"]
            else:
                bits = [f"{self.side} ${self.usd_amount:g} of {self.token}"]
        elif self.kind == "limit":
            bits = [f"{self.side} ${self.usd_amount:g} {self.token} @ limit ${self.limit_price:g}"]
        elif self.kind == "stop":
            tgt = f"${self.usd_amount:g} of" if self.usd_amount else "all"
            bits = [f"stop: sell {tgt} {self.token} if {self.trigger.describe()}"]
        elif self.kind == "trailing_stop":
            if self.sell_all:
                tgt = f"all {self.token}"
            elif self.token_amount is not None:
                tgt = f"{self.token_amount:g} {self.token}"
            else:
                tgt = f"${self.usd_amount:g} of {self.token}"
            bits = [f"trailing stop: sell {tgt} {self.trail_pct:g}% below peak"]
        elif self.kind == "grid":
            bits = [f"grid {self.token} ${self.grid_lower:g}–${self.grid_upper:g}, "
                    f"{self.grid_levels} levels @ ${self.usd_per_level:g}"]
        elif self.kind == "rate":
            bits = [f"{self.side} {self.token} @ ${self.rate_usd_per_min:g}/min"]
            if self.condition:
                bits.append(f"while {self.condition.describe()}")
            if self.total_cap_usd:
                bits.append(f"until ${self.total_cap_usd:g} total")
        elif self.kind == "triggered_rate":
            bits = [f"{self.side} ${self.usd_amount:g} {self.token} if {self.trigger.describe()}"]
            if self.rate_usd_per_min:
                cond = f" while {self.condition.describe()}" if self.condition else ""
                bits.append(f"then ${self.rate_usd_per_min:g}/min{cond}")
            if self.total_cap_usd:
                bits.append(f"until ${self.total_cap_usd:g} total")
        return ", ".join(bits)


class ParseError(Exception):
    pass


# ---------------------------------------------------------------- clause regexes

RE_SIDE_TOKEN = re.compile(
    rf"\b(buy|sell|purchase|acquire|dump|unload)\b(?:\s+{NUM}\s*(?:of|worth of)\s+)?\s*([A-Za-z][A-Za-z0-9_\-]{{1,15}})?",
    re.I,
)
RE_USD_OF = re.compile(rf"{NUM}\s*(?:of|worth of)\s+([A-Za-z][A-Za-z0-9_\-]{{1,15}})", re.I)
RE_TOKEN_AMOUNT = re.compile(
    r"\b(?:buy|sell)\s+([0-9][0-9,]*\.?[0-9]*)\s+([A-Za-z][A-Za-z0-9_\-]{1,15})\b(?!\s*(?:per|/))", re.I
)
RE_RATE = re.compile(rf"rate\s+of\s+{NUM}\s*(?:per|a|/)\s*(minute|min|hour|hr|second|sec)", re.I)
RE_RATE_ALT = re.compile(rf"{NUM}\s*(?:per|a|/)\s*(minute|min|hour|hr|second|sec)", re.I)
RE_WHILE = re.compile(
    rf"(?:while|as long as|so long as|when(?:ever)?)\s+(?:the\s+)?price\s+(?:is|stays|remains|holds)\s*(above|below|over|under|greater than|less than)\s*{NUM}",
    re.I,
)
RE_TRIGGER = re.compile(
    rf"(?:if|once|when)\s+(?:the\s+)?price\s+(?:goes|drops?|falls?|dips?|rises?|climbs?|moves|gets|is|crosses)?\s*(above|below|over|under|past|to under|to over)\s*{NUM}",
    re.I,
)
RE_TOTAL = re.compile(rf"total\s+of\s+{NUM}|{NUM}\s+(?:in\s+)?total", re.I)
RE_LIMIT = re.compile(rf"\bat\s+(?:a\s+)?(?:limit\s+(?:price\s+)?(?:of\s+)?)?{NUM}\s*(?:per\s+token|each)?\s*$", re.I)
RE_STOPLOSS = re.compile(rf"stop\s*loss\s+(?:at|@)?\s*{NUM}", re.I)
RE_TAKEPROFIT = re.compile(rf"take\s*profit\s+(?:at|@)?\s*{NUM}", re.I)
RE_CANCEL = re.compile(r"\b(cancel|stop|kill|halt)\b.*\b(all|everything|strategies|orders)\b|\bcancel\b\s*$", re.I)
RE_PAUSE = re.compile(r"^\s*pause\b", re.I)
RE_RESUME = re.compile(r"^\s*(resume|unpause|continue)\b\s*$", re.I)
RE_SELL_ALL = re.compile(r"\bsell\s+all\s+(?:my\s+|of\s+my\s+)?([A-Za-z][A-Za-z0-9_\-]{1,15})", re.I)
RE_ON_CHAIN = re.compile(r"\bon\s+(base|robinhood(?:\s*chain)?)\b", re.I)
RE_TRAILING_STOP = re.compile(
    rf"trailing\s+stop\s+(?:of\s+)?{NUM}\s*%",
    re.I,
)
RE_FALLS_FROM_HIGH = re.compile(
    rf"(?:falls?|drops?|declines?)\s+{NUM}\s*%\s+from\s+(?:its\s+)?(?:high|peak|ath)",
    re.I,
)
RE_ON_TOKEN = re.compile(
    r"\bon\s+([A-Za-z][A-Za-z0-9_\-]{1,15})\b",
    re.I,
)
RE_GRID = re.compile(
    rf"\bgrid\s+([A-Za-z][A-Za-z0-9_\-]{{1,15}})\s+"
    rf"(?:between|from)\s*{NUM}\s*(?:and|to)\s*{NUM}",
    re.I,
)
RE_GRID_LEVELS = re.compile(r"(\d+)\s*levels?", re.I)
RE_GRID_SIZE = re.compile(rf"{NUM}\s*(?:per\s+level|each)\b", re.I)

_STOPWORDS = {
    "THE", "A", "AN", "OF", "AT", "IF", "WHILE", "WHEN", "PRICE", "ALL", "MY",
    "IT", "NOW", "TOTAL", "RATE", "PER", "MINUTE", "HOUR", "SECOND", "USD",
    "DOLLARS", "WORTH", "UNTIL", "ONCE", "TO", "BASE", "ROBINHOOD", "CHAIN",
    "TRAILING", "STOP", "FROM", "ITS", "HIGH", "PEAK", "WITH", "GRID",
    "BETWEEN", "LEVELS", "LEVEL", "EACH",
}


def _norm_op(op: str) -> str:
    op = op.lower()
    if op in ("above", "over", "greater than", "past", "to over"):
        return "above"
    return "below"


def _rate_per_min(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit.startswith("min"):
        return value
    if unit.startswith(("hour", "hr")):
        return value / 60.0
    return value * 60.0  # per second


def _find_token(text: str) -> str:
    m = RE_USD_OF.search(text)
    if m:
        return m.group(2).upper()
    m = RE_SELL_ALL.search(text)
    if m:
        return m.group(1).upper()
    m = RE_TOKEN_AMOUNT.search(text)
    if m and m.group(2).upper() not in _STOPWORDS:
        return m.group(2).upper()
    m = RE_SIDE_TOKEN.search(text)
    if m and m.group(3) and m.group(3).upper() not in _STOPWORDS:
        return m.group(3).upper()
    # "trailing stop … on TOKEN"
    for m in RE_ON_TOKEN.finditer(text):
        cand = m.group(1).upper()
        if cand not in _STOPWORDS:
            return cand
    # last resort: any TitleCase/UPPER symbol-looking word
    for w in re.findall(r"\b[A-Z][A-Za-z0-9]{1,14}\b", text):
        if w.upper() not in _STOPWORDS:
            return w.upper()
    return ""


def parse_command(text: str) -> StrategySpec:
    """Deterministic grammar parse. Raises ParseError when nothing matches."""
    t = " ".join(text.strip().split())
    if not t:
        raise ParseError("empty command")

    if RE_RESUME.search(t):
        return StrategySpec(kind="resume", raw_text=text)
    if RE_PAUSE.search(t):
        return StrategySpec(kind="pause", raw_text=text)
    if RE_CANCEL.search(t) and not re.search(r"stop\s*loss", t, re.I):
        return StrategySpec(kind="cancel", raw_text=text)

    chain = ""
    mc = RE_ON_CHAIN.search(t)
    if mc:
        chain = "robinhood" if mc.group(1).lower().startswith("robinhood") else "base"

    # -------- grid (before generic token/side parsing)
    mg = RE_GRID.search(t)
    if mg:
        token = mg.group(1).upper()
        lo, hi = _f(mg.group(2)), _f(mg.group(3))
        ml = RE_GRID_LEVELS.search(t)
        ms = RE_GRID_SIZE.search(t)
        if not ml or not ms:
            raise ParseError("grid needs N levels and $X per level / each")
        levels = int(ml.group(1))
        per = _f(ms.group(1))
        if hi <= lo:
            raise ParseError("grid upper must be greater than lower")
        if not (2 <= levels <= 50):
            raise ParseError("grid levels must be between 2 and 50")
        if per <= 0:
            raise ParseError("grid usd per level must be positive")
        return StrategySpec(
            kind="grid", side="buy", token=token, chain=chain,
            grid_lower=lo, grid_upper=hi, grid_levels=levels, usd_per_level=per,
            raw_text=text,
        )

    side = "buy"
    ms = re.search(r"\b(buy|purchase|acquire)\b", t, re.I)
    if not ms and re.search(r"\b(sell|dump|unload)\b", t, re.I):
        side = "sell"

    token = _find_token(t)

    # stop loss / take profit shorthand
    msl = RE_STOPLOSS.search(t)
    mtp = RE_TAKEPROFIT.search(t)
    if msl or mtp:
        m = msl or mtp
        op = "below" if msl else "above"
        usd = 0.0
        mu = RE_USD_OF.search(t)
        if mu:
            usd = _f(mu.group(1))
        return StrategySpec(
            kind="stop", side="sell", token=token, chain=chain, usd_amount=usd,
            trigger=Condition(op, _f(m.group(1))), raw_text=text,
        )

    trigger = None
    mt = RE_TRIGGER.search(t)
    if mt:
        trigger = Condition(_norm_op(mt.group(1)), _f(mt.group(2)))

    condition = None
    mw = RE_WHILE.search(t)
    if mw:
        condition = Condition(_norm_op(mw.group(1)), _f(mw.group(2)))

    rate = 0.0
    mr = RE_RATE.search(t) or RE_RATE_ALT.search(t)
    if mr:
        rate = _rate_per_min(_f(mr.group(1)), mr.group(2))

    total = None
    for m in RE_TOTAL.finditer(t):
        total = _f(m.group(1) or m.group(2))

    usd = 0.0
    mu = RE_USD_OF.search(t)
    if mu:
        usd = _f(mu.group(1))
    elif "$" in t and not rate:
        m = re.search(NUM.replace(r"\$?", r"\$"), t)
        if m:
            usd = _f(m.group(1))

    token_amount = None
    sell_all = bool(RE_SELL_ALL.search(t))
    if not sell_all and not usd:
        mta = RE_TOKEN_AMOUNT.search(t)
        if mta and mta.group(2).upper() not in _STOPWORDS:
            token_amount = _f(mta.group(1))

    if not token and not sell_all:
        raise ParseError("couldn't work out which token you mean")

    # -------- trailing stop (before other classify so "% from high" wins)
    trail_m = RE_TRAILING_STOP.search(t) or RE_FALLS_FROM_HIGH.search(t)
    if trail_m:
        trail_pct = _f(trail_m.group(1))
        if trail_pct <= 0 or trail_pct >= 100:
            raise ParseError("trailing stop percent must be between 0 and 100")
        sa = sell_all
        if not sa and not usd and token_amount is None:
            sa = True  # default size: sell all
        return StrategySpec(
            kind="trailing_stop", side="sell", token=token, chain=chain,
            usd_amount=usd, token_amount=token_amount, sell_all=sa,
            trail_pct=trail_pct, raw_text=text,
        )

    # -------- classify
    if trigger and rate:
        return StrategySpec(
            kind="triggered_rate", side=side, token=token, chain=chain,
            usd_amount=usd, rate_usd_per_min=rate, total_cap_usd=total,
            trigger=trigger, condition=condition or Condition(trigger.op, trigger.value),
            raw_text=text,
        )
    if trigger and side == "sell" and (usd or sell_all or token_amount):
        return StrategySpec(
            kind="stop", side="sell", token=token, chain=chain, usd_amount=usd,
            token_amount=token_amount, sell_all=sell_all, trigger=trigger, raw_text=text,
        )
    if trigger:
        return StrategySpec(
            kind="triggered_rate", side=side, token=token, chain=chain,
            usd_amount=usd, trigger=trigger, total_cap_usd=total, raw_text=text,
        )
    if rate:
        return StrategySpec(
            kind="rate", side=side, token=token, chain=chain,
            rate_usd_per_min=rate, condition=condition, total_cap_usd=total,
            raw_text=text,
        )
    ml = RE_LIMIT.search(t)
    if ml and usd:
        return StrategySpec(
            kind="limit", side=side, token=token, chain=chain, usd_amount=usd,
            limit_price=_f(ml.group(1)), raw_text=text,
        )
    if usd or sell_all or token_amount is not None:
        return StrategySpec(
            kind="market", side=side, token=token, chain=chain, usd_amount=usd,
            token_amount=token_amount, sell_all=sell_all, raw_text=text,
        )
    raise ParseError("couldn't understand that command")


# ---------------------------------------------------------------- LLM fallback

_LLM_SYSTEM = """You translate trading commands into JSON. Respond ONLY with JSON matching:
{"kind":"market|rate|triggered_rate|limit|stop|cancel|pause|resume",
 "side":"buy|sell","token":"SYMBOL","chain":"","usd_amount":0,
 "token_amount":null,"sell_all":false,"rate_usd_per_min":0,
 "total_cap_usd":null,
 "trigger":{"op":"above|below","value":0} or null,
 "condition":{"op":"above|below","value":0} or null,
 "limit_price":null}
Rules: rates are converted to USD per minute. "stop" means sell when trigger hits.
If the command is ambiguous or not a trading command, respond {"kind":"error","reason":"..."}."""


def parse_with_claude(text: str) -> StrategySpec:
    """Fallback parser using the Anthropic API (requires ANTHROPIC_API_KEY)."""
    import httpx

    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ParseError("ANTHROPIC_API_KEY not set; enable it or rephrase the command")
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 400,
            "system": _LLM_SYSTEM,
            "messages": [{"role": "user", "content": text}],
        },
        timeout=30,
    )
    r.raise_for_status()
    body = "".join(b.get("text", "") for b in r.json()["content"])
    body = re.sub(r"```(?:json)?|```", "", body).strip()
    data = json.loads(body)
    if data.get("kind") == "error":
        raise ParseError(data.get("reason", "could not parse"))
    for k in ("trigger", "condition"):
        if data.get(k):
            data[k] = Condition(data[k]["op"], float(data[k]["value"]))
    data.setdefault("raw_text", text)
    allowed = StrategySpec.__dataclass_fields__.keys()
    return StrategySpec(**{k: v for k, v in data.items() if k in allowed})

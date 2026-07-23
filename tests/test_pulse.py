"""Run with: python -m pytest tests/ (or just python tests/test_pulse.py)"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tradebot.commands import parse_command
from tradebot.config import BotConfig, ChainConfig, RiskConfig, TokenConfig
from tradebot.portfolio import Portfolio
from tradebot.prices import PaperFeed, PriceBook, has_live_price_source
from tradebot.server import _wire_paper_feed
from tradebot.strategies import Engine


def test_rate_sell():
    s = parse_command("sell TokenA at a rate of $300 per minute while the price is above $0.15")
    assert s.kind == "rate" and s.side == "sell" and s.token == "TOKENA"
    assert s.rate_usd_per_min == 300
    assert s.condition.op == "above" and s.condition.value == 0.15


def test_triggered_dca():
    s = parse_command(
        "Buy $450 of TokenA if the price goes below $0.1 while the price is below "
        "$0.1 continue to buy at a rate of $100 per minute until you have bought a total of $1200")
    assert s.kind == "triggered_rate" and s.side == "buy" and s.token == "TOKENA"
    assert s.usd_amount == 450 and s.rate_usd_per_min == 100 and s.total_cap_usd == 1200
    assert s.trigger.op == "below" and s.trigger.value == 0.1
    assert s.condition.op == "below" and s.condition.value == 0.1


def test_market_stop_misc():
    assert parse_command("buy $200 of TOKENA").kind == "market"
    s = parse_command("sell all TokenA if the price drops below $0.08")
    assert s.kind == "stop" and s.sell_all and s.trigger.value == 0.08
    assert parse_command("cancel all").kind == "cancel"
    assert parse_command("pause").kind == "pause"
    s = parse_command("sell TokenB at a rate of $600 per hour on robinhood")
    assert s.chain == "robinhood" and abs(s.rate_usd_per_min - 10) < 1e-9
    s = parse_command("stop loss at $0.09 for $500 of TokenA")
    assert s.kind == "stop" and s.trigger.op == "below"


def test_trailing_stop_parse():
    s = parse_command("sell all TOKENA if the price falls 10% from its high")
    assert s.kind == "trailing_stop" and s.sell_all and s.trail_pct == 10
    assert "10%" in s.describe() and "peak" in s.describe()
    s = parse_command("trailing stop 10% on TOKENA")
    assert s.kind == "trailing_stop" and s.sell_all and s.token == "TOKENA"
    s = parse_command("sell $500 of TOKENA with a trailing stop of 8%")
    assert s.kind == "trailing_stop" and abs(s.usd_amount - 500) < 1e-9 and s.trail_pct == 8
    assert not s.sell_all
    s = parse_command("trailing stop 12% on TOKENA on robinhood")
    assert s.chain == "robinhood" and s.trail_pct == 12 and s.token == "TOKENA"


def _engine():
    cfg = BotConfig(chains={"base": ChainConfig(
        name="Base", chain_id=8453, rpc_url="",
        tokens={"TOKENA": TokenConfig(symbol="TOKENA")})})
    cfg.min_slice_usd = 5
    book = PriceBook()
    pf = Portfolio(cash_usd=10_000)
    return Engine(cfg, book, pf, None, {}), book, pf


def test_engine_rate_execution():
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.20)
    strat = eng.submit(parse_command(
        "sell TokenA at a rate of $300 per minute while the price is above $0.15"))
    # seed inventory
    eng.submit(parse_command("buy $2000 of TOKENA"))
    eng.tick()
    # simulate 60s above the gate: should sell ~$300
    for _ in range(60):
        strat.last_tick -= 1  # pretend 1s passed
        eng.tick()
    sold = sum(t.usd for t in pf.trades if t.side == "sell")
    assert 250 <= sold <= 350, sold
    # drop below gate: no further sells
    book.update("base", "TOKENA", 0.10)
    before = sold
    for _ in range(30):
        strat.last_tick -= 1
        eng.tick()
    sold_after = sum(t.usd for t in pf.trades if t.side == "sell")
    assert sold_after == before


def test_engine_triggered_cap():
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.12)
    strat = eng.submit(parse_command(
        "Buy $450 of TokenA if the price goes below $0.1 while the price is below "
        "$0.1 continue to buy at a rate of $100 per minute until you have bought a total of $1200"))
    eng.tick()
    assert strat.status == "waiting"  # armed, not fired at 0.12
    book.update("base", "TOKENA", 0.095)
    eng.tick()
    assert strat.fills == 1 and abs(strat.spent_usd - 450) < 1e-6
    for _ in range(60 * 12):
        strat.last_tick -= 1
        eng.tick()
        if strat.status == "done":
            break
    assert strat.status == "done"
    assert abs(strat.spent_usd - 1200) < 1e-6
    bought = sum(t.usd for t in pf.trades if t.side == "buy")
    assert abs(bought - 1200) < 1e-6


def test_strategy_detail_and_pause():
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.10)
    s = eng.submit(parse_command("buy TokenA at a rate of $120 per minute until a total of $600"))
    for _ in range(120):
        s.last_tick -= 1
        eng.tick()
    d = eng.strategy_detail(s.id)
    assert d["fills"] >= 2 and abs(d["spent_usd"] - sum(f["usd"] for f in d["fills_detail"])) < 1e-6
    assert d["vwap"] > 0 and d["cum_series"][-1]["value"] == d["spent_usd"]
    # PnL attribution: price doubles -> bought qty worth more
    book.update("base", "TOKENA", 0.20)
    d2 = eng.strategy_detail(s.id)
    assert d2["pnl"] > 0
    # pause freezes fills
    assert eng.set_paused(s.id, True) or s.status == "done"
    if s.status != "done":
        before = s.fills
        for _ in range(60):
            s.last_tick -= 1
            eng.tick()
        assert s.fills == before
        eng.set_paused(s.id, False)
    # concurrent second strategy runs independently
    s2 = eng.submit(parse_command("sell TokenA at a rate of $60 per minute while the price is above $0.05"))
    for _ in range(30):
        s2.last_tick -= 1
        eng.tick()
    assert eng.strategy_detail(s2.id)["fills"] >= 1
    assert eng.strategy_detail("nope") is None


def _engine_with_store(db_path):
    from tradebot.store import Store
    cfg = BotConfig(chains={"base": ChainConfig(
        name="Base", chain_id=8453, rpc_url="",
        tokens={"TOKENA": TokenConfig(symbol="TOKENA")})},
        db_path=str(db_path))
    cfg.min_slice_usd = 5
    book = PriceBook()
    pf = Portfolio(cash_usd=10_000)
    store = Store(db_path)
    eng = Engine(cfg, book, pf, None, {}, store=store)
    eng.restore()
    return eng, book, pf, store


def test_persistence_survives_restart(tmp_path=None):
    import tempfile
    from pathlib import Path
    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    db = root / "pulse.db"

    eng, book, pf, store = _engine_with_store(db)
    book.update("base", "TOKENA", 0.10)
    # market buy builds a position; rate sell streams against it
    buy = eng.submit(parse_command("buy $500 of TOKENA"))
    eng.tick()
    sell = eng.submit(parse_command(
        "sell TokenA at a rate of $120 per minute while the price is above $0.05 "
        "until a total of $200"))
    for _ in range(90):
        sell.last_tick -= 1
        eng.tick()
    assert sell.fills >= 1 and sell.spent_usd > 0
    sid_buy, sid_sell = buy.id, sell.id
    spent, fills = sell.spent_usd, sell.fills
    cash = pf.cash_usd
    pos_qty = pf.qty("base", "TOKENA")
    n_trades = len(pf.trades)
    # leave a non-zero accrual as if the process died mid-stream
    sell.accrued_usd = 50.0
    store.save_strategy(sell)
    store.close()

    eng2, book2, pf2, store2 = _engine_with_store(db)
    book2.update("base", "TOKENA", 0.10)
    s_sell = eng2.find(sid_sell)
    s_buy = eng2.find(sid_buy)
    assert s_buy is not None and s_buy.id == sid_buy
    assert s_sell is not None and s_sell.id == sid_sell
    assert abs(s_sell.spent_usd - spent) < 1e-9
    assert s_sell.fills == fills
    assert abs(pf2.cash_usd - cash) < 1e-6
    assert abs(pf2.qty("base", "TOKENA") - pos_qty) < 1e-9
    assert len(pf2.trades) == n_trades
    assert s_sell.accrued_usd == 0.0
    # next new strategy must not reuse restored IDs
    s3 = eng2.submit(parse_command("buy $10 of TOKENA"))
    assert s3.id not in (sid_buy, sid_sell)
    store2.close()


def test_no_burst_fill_after_restore(tmp_path=None):
    import tempfile
    from pathlib import Path
    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    db = root / "noburst.db"

    eng, book, pf, store = _engine_with_store(db)
    book.update("base", "TOKENA", 0.10)
    eng.submit(parse_command("buy $2000 of TOKENA"))
    eng.tick()
    sell = eng.submit(parse_command(
        "sell TokenA at a rate of $300 per minute while the price is above $0.05"))
    for _ in range(20):
        sell.last_tick -= 1
        eng.tick()
    # simulate long downtime with a huge pending accrual written to disk
    sell.accrued_usd = 500.0
    sell.last_tick = time.time() - 3600
    store.save_strategy(sell)
    fills_before = sell.fills
    store.close()

    eng2, book2, pf2, store2 = _engine_with_store(db)
    book2.update("base", "TOKENA", 0.10)
    s = eng2.find(sell.id)
    assert s is not None
    assert s.accrued_usd == 0.0
    assert s.fills == fills_before
    # first post-restore tick: last_tick was set to now on restore, so dt ~ 0
    # — no catch-up burst even though disk had accrued_usd=500
    eng2.tick()
    assert s.fills == fills_before
    assert s.accrued_usd < 1.0  # at most a sub-second tick of accrual, not $500
    store2.close()


def test_reset_archives_db(tmp_path=None):
    import tempfile
    from pathlib import Path
    from tradebot.store import Store, archive_db
    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    db = root / "pulse.db"
    store = Store(db)
    store.set_meta("start_equity", "10000")
    store.close()
    assert db.exists()
    archived = archive_db(db)
    assert archived is not None
    assert archived.exists()
    assert not db.exists()
    assert archived.name.startswith("pulse.db.")
    # second reset with nothing to archive
    assert archive_db(db) is None


def test_risk_open_notional_blocks_buys_not_sells():
    eng, book, pf = _engine()
    eng.cfg.risk = RiskConfig(max_open_notional_usd_per_token=100)
    book.update("base", "TOKENA", 0.10)
    eng.submit(parse_command("buy $80 of TOKENA"))
    eng.tick()
    assert pf.qty("base", "TOKENA") * 0.10 > 70
    buy = eng.submit(parse_command("buy $50 of TOKENA"))
    eng.tick()
    assert buy.status == "active"
    assert buy.blocked_reason and "open notional" in buy.blocked_reason
    assert buy.fills == 0
    # sells are never blocked by open-notional
    s2 = eng.submit(parse_command(
        "sell TokenA at a rate of $60 per minute while the price is above $0.05 "
        "until a total of $30"))
    for _ in range(60):
        s2.last_tick -= 1
        eng.tick()
    assert s2.fills >= 1
    assert "open notional" not in (s2.blocked_reason or "")


def test_risk_daily_spend_window():
    eng, book, pf = _engine()
    eng.cfg.risk = RiskConfig(max_daily_spend_usd=100)
    eng.cfg.min_slice_usd = 10
    book.update("base", "TOKENA", 0.10)
    s = eng.submit(parse_command(
        "buy TokenA at a rate of $300 per minute until a total of $400"))
    for _ in range(120):
        s.last_tick -= 1
        eng.tick()
        if s.blocked_reason:
            break
    assert s.blocked_reason and "daily spend" in s.blocked_reason
    assert s.status == "active"
    spent = sum(t.usd for t in pf.trades)
    assert spent <= 100 + eng.cfg.min_slice_usd  # may overshoot by at most one slice attempt size
    assert spent <= 100 + 50  # generous bound
    fills_at_block = s.fills
    # age trades out of the 24h window — cap should free
    for t in pf.trades:
        t.ts -= 86_400 + 10
    s._last_block_log = 0
    for _ in range(10):
        s.last_tick -= 1
        eng.tick()
    assert s.fills > fills_at_block
    assert s.blocked_reason == ""


def test_risk_default_cap_for_uncapped():
    eng, book, pf = _engine()
    eng.cfg.risk = RiskConfig(default_cap_usd_for_uncapped=250)
    book.update("base", "TOKENA", 0.10)
    s = eng.submit(parse_command(
        "buy TokenA at a rate of $120 per minute while the price is above $0.05"))
    assert s.spec.total_cap_usd == 250
    assert any("default cap" in n for n in s.spec.notes)
    assert any("default cap" in e["msg"] for e in eng.events)


def test_risk_blocked_rate_no_accrual_burst():
    eng, book, pf = _engine()
    eng.cfg.risk = RiskConfig(max_daily_spend_usd=25)
    eng.cfg.min_slice_usd = 10
    book.update("base", "TOKENA", 0.10)
    s = eng.submit(parse_command(
        "buy TokenA at a rate of $600 per minute until a total of $500"))
    for _ in range(80):
        s.last_tick -= 1
        eng.tick()
        if s.blocked_reason:
            break
    assert s.blocked_reason
    slice_usd = min(max(eng.cfg.min_slice_usd, s.spec.rate_usd_per_min / 6),
                    (s.spec.total_cap_usd or 500) - s.spent_usd)
    # keep ticking while blocked — accrued must stay <= one slice
    for _ in range(60):
        s.last_tick -= 1
        eng.tick()
        assert s.accrued_usd <= slice_usd + 1e-6
    assert s.status == "active"


def test_protective_exits_bypass_daily_spend():
    """Stops and sell-all still fire when the daily spend cap is exhausted."""
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.10)
    # Build inventory first, then tighten the daily cap so it's already exhausted.
    eng.submit(parse_command("buy $80 of TOKENA"))
    eng.tick()
    assert pf.qty("base", "TOKENA") > 0
    qty_before = pf.qty("base", "TOKENA")
    eng.cfg.risk = RiskConfig(max_daily_spend_usd=50)  # already spent ~$80

    # Plain rate-sell remains blocked by the daily cap.
    rate_sell = eng.submit(parse_command(
        "sell TokenA at a rate of $60 per minute while the price is above $0.05 "
        "until a total of $50"))
    for _ in range(30):
        rate_sell.last_tick -= 1
        eng.tick()
    assert rate_sell.fills == 0
    assert rate_sell.blocked_reason and "daily spend" in rate_sell.blocked_reason

    # Stop still fires its protective sell.
    stop = eng.submit(parse_command(
        "sell all TokenA if the price drops below $0.12"))
    book.update("base", "TOKENA", 0.09)
    eng.tick()
    assert stop.status == "done"
    assert stop.fills == 1
    assert pf.qty("base", "TOKENA") < qty_before - 1e-9


def _transfer_log(token: str, to_addr: str, amount: int, frm: str = "0x" + "11" * 20):
    """Synthetic ERC-20 Transfer log dict (no web3 types)."""
    topic0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    pad = lambda a: "0x" + a.lower().removeprefix("0x").rjust(64, "0")
    return {
        "address": token,
        "topics": [topic0, pad(frm), pad(to_addr)],
        "data": hex(amount),
    }


def test_decode_transfer_amount():
    from tradebot.chains import decode_transfer_amount

    token = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # checksummed USDC-like
    recipient = "0xAb5801a7D398351b8bE11C439e05C5B3259aeC9B"
    other = "0x00000000000000000000000000000000000000aa"

    # (a) single matching transfer
    logs = [_transfer_log(token, recipient, 1_500_000)]
    assert decode_transfer_amount(logs, token, recipient) == 1_500_000

    # (b) multiple transfers — only matching token+recipient summed
    logs = [
        _transfer_log(token, other, 999),
        _transfer_log(token, recipient, 100),
        _transfer_log("0x" + "22" * 20, recipient, 777),
        _transfer_log(token, recipient, 50),
    ]
    assert decode_transfer_amount(logs, token, recipient) == 150

    # (c) no match
    assert decode_transfer_amount(
        [_transfer_log(token, other, 1)], token, recipient) is None
    assert decode_transfer_amount([], token, recipient) is None

    # (d) lowercase vs checksummed address forms
    assert decode_transfer_amount(
        [_transfer_log(token.lower(), recipient.lower(), 42)],
        token, recipient) == 42
    assert decode_transfer_amount(
        [_transfer_log(token, recipient, 42)],
        token.lower(), recipient.lower()) == 42


def test_trailing_stop_engine_and_persist(tmp_path=None):
    import tempfile
    from pathlib import Path
    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    db = root / "trail.db"

    # (a) peak ratchets up and never down; (c) shallow dip does not fire
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.10)
    eng.submit(parse_command("buy $500 of TOKENA"))
    eng.tick()
    trail = eng.submit(parse_command("trailing stop 10% on TOKENA"))
    eng.tick()
    assert abs(trail.peak_price - 0.10) < 1e-12
    book.update("base", "TOKENA", 0.12)
    eng.tick()
    assert abs(trail.peak_price - 0.12) < 1e-12
    book.update("base", "TOKENA", 0.11)
    eng.tick()
    assert abs(trail.peak_price - 0.12) < 1e-12
    book.update("base", "TOKENA", 0.109)  # trigger at 0.108
    eng.tick()
    assert trail.status == "active" and trail.fills == 0

    # (b) fires at threshold and sells full position for sell_all
    book.update("base", "TOKENA", 0.20)
    eng.tick()
    assert abs(trail.peak_price - 0.20) < 1e-12
    book.update("base", "TOKENA", 0.20 * 0.9)
    eng.tick()
    assert trail.status == "done" and trail.fills == 1
    assert pf.qty("base", "TOKENA") < 1e-9

    # (d) peak persists through store round-trip and does not reset
    eng, book, pf, store = _engine_with_store(db)
    book.update("base", "TOKENA", 0.10)
    eng.submit(parse_command("buy $200 of TOKENA"))
    eng.tick()
    trail = eng.submit(parse_command("trailing stop 10% on TOKENA"))
    book.update("base", "TOKENA", 0.14)
    eng.tick()
    assert abs(trail.peak_price - 0.14) < 1e-12
    sid, peak = trail.id, trail.peak_price
    store.close()

    eng2, book2, pf2, store2 = _engine_with_store(db)
    book2.update("base", "TOKENA", 0.13)  # below prior peak, above trigger 0.126
    r = eng2.find(sid)
    assert r is not None and abs(r.peak_price - peak) < 1e-12
    eng2.tick()
    assert abs(r.peak_price - peak) < 1e-12  # did not reset to 0.13
    store2.close()

    # (e) trailing stop fires even when daily spend cap is exhausted
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.10)
    eng.submit(parse_command("buy $80 of TOKENA"))
    eng.tick()
    eng.cfg.risk = RiskConfig(max_daily_spend_usd=50)
    trail = eng.submit(parse_command("trailing stop 10% on TOKENA"))
    book.update("base", "TOKENA", 0.15)
    eng.tick()
    book.update("base", "TOKENA", 0.15 * 0.9)
    eng.tick()
    assert trail.status == "done" and trail.fills == 1
    assert pf.qty("base", "TOKENA") < 1e-9


def test_grid_parse():
    s = parse_command(
        "grid TOKENA between $0.08 and $0.14 with 7 levels, $50 per level")
    assert s.kind == "grid" and s.token == "TOKENA"
    assert s.grid_lower == 0.08 and s.grid_upper == 0.14
    assert s.grid_levels == 7 and s.usd_per_level == 50
    assert "7 levels" in s.describe()
    s = parse_command("grid TOKENA from $0.08 to $0.14, 7 levels, $50 each")
    assert s.kind == "grid" and s.usd_per_level == 50
    s = parse_command(
        "grid TOKENA between $0.08 and $0.14, 5 levels, $100 per level on robinhood")
    assert s.chain == "robinhood" and s.grid_levels == 5
    try:
        parse_command("grid TOKENA between $0.14 and $0.08 with 3 levels, $10 per level")
        assert False, "expected ParseError"
    except Exception as e:
        assert "upper" in str(e).lower() or "greater" in str(e).lower()


def test_grid_engine(tmp_path=None):
    import tempfile
    from pathlib import Path
    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    db = root / "grid.db"

    # 3 levels: 0.10, 0.12, 0.14
    eng, book, pf = _engine()
    eng.cfg.min_slice_usd = 5
    book.update("base", "TOKENA", 0.15)
    g = eng.submit(parse_command(
        "grid TOKENA between $0.10 and $0.14 with 3 levels, $50 per level"))
    eng.tick()  # anchor at 0.15
    assert g.prev_price == 0.15 and len(g.grid_lots) == 0

    # (a) downward sweep buys lots on all but the top level
    book.update("base", "TOKENA", 0.10)  # still in-band; crosses 0.14, 0.12, 0.10
    eng.tick()
    assert len(g.grid_lots) == 2  # L0, L1 only — top is sell-only
    assert set(g.grid_lots) == {0, 1}
    buys = sum(1 for t in pf.trades if t.side == "buy" and t.strategy_id == g.id)
    assert buys == 2
    book.update("base", "TOKENA", 0.08)  # outside band — no new buys
    eng.tick()
    assert len(g.grid_lots) == 2
    buys2 = sum(1 for t in pf.trades if t.side == "buy" and t.strategy_id == g.id)
    assert buys2 == 2

    # (c) price wandering outside the band does nothing after exiting
    book.update("base", "TOKENA", 0.20)
    eng.tick()  # upward sells lots when crossing levels
    assert len(g.grid_lots) == 0
    fills_at = g.fills
    book.update("base", "TOKENA", 0.25)
    eng.tick()
    book.update("base", "TOKENA", 0.22)
    eng.tick()
    assert g.fills == fills_at

    # (b) oscillation across one level pair — positive realized PnL
    eng, book, pf = _engine()
    eng.cfg.min_slice_usd = 5
    book.update("base", "TOKENA", 0.13)
    g = eng.submit(parse_command(
        "grid TOKENA between $0.10 and $0.14 with 3 levels, $50 per level"))
    eng.tick()
    # buy L1 (0.12) by crossing down through it
    book.update("base", "TOKENA", 0.115)
    eng.tick()
    assert 1 in g.grid_lots
    # sell by crossing L2 (0.14) upward
    book.update("base", "TOKENA", 0.145)
    eng.tick()
    assert 1 not in g.grid_lots
    # repeat
    book.update("base", "TOKENA", 0.115)
    eng.tick()
    book.update("base", "TOKENA", 0.145)
    eng.tick()
    sells = [t for t in pf.trades if t.side == "sell" and t.strategy_id == g.id]
    assert len(sells) >= 2
    # buy low / sell high → strategy pnl should be positive at current mark
    assert g.pnl(0.145) > 0

    # (d) restart preserves lots; re-anchor tick does not fire
    eng, book, pf, store = _engine_with_store(db)
    eng.cfg.min_slice_usd = 5
    book.update("base", "TOKENA", 0.15)
    g = eng.submit(parse_command(
        "grid TOKENA between $0.10 and $0.14 with 3 levels, $50 per level"))
    eng.tick()
    book.update("base", "TOKENA", 0.10)
    eng.tick()
    assert len(g.grid_lots) == 2
    sid, lots = g.id, dict(g.grid_lots)
    fills_before = g.fills
    store.close()

    eng2, book2, pf2, store2 = _engine_with_store(db)
    book2.update("base", "TOKENA", 0.10)
    r = eng2.find(sid)
    assert r is not None and len(r.grid_lots) == 2
    assert r.prev_price == 0.0
    eng2.tick()  # re-anchor only
    assert r.fills == fills_before
    assert len(r.grid_lots) == 2
    assert abs(r.prev_price - 0.10) < 1e-12
    store2.close()

    # (e) risk-blocked buy does not create a phantom lot
    eng, book, pf = _engine()
    eng.cfg.min_slice_usd = 5
    eng.cfg.risk = RiskConfig(max_daily_spend_usd=40)
    book.update("base", "TOKENA", 0.15)
    g = eng.submit(parse_command(
        "grid TOKENA between $0.10 and $0.14 with 3 levels, $50 per level"))
    eng.tick()
    book.update("base", "TOKENA", 0.10)
    eng.tick()
    # first buy of $50 would exceed $40 daily cap → all buys blocked, no lots
    assert len(g.grid_lots) == 0
    assert g.blocked_reason and "daily spend" in g.blocked_reason


def test_grid_no_buy_at_top_level():
    """Oscillation tightly around the upper bound must not buy the top line."""
    eng, book, pf = _engine()
    eng.cfg.min_slice_usd = 5
    # 3 levels: 0.10, 0.12, 0.14 — top is sell-only
    book.update("base", "TOKENA", 0.145)
    g = eng.submit(parse_command(
        "grid TOKENA between $0.10 and $0.14 with 3 levels, $50 per level"))
    eng.tick()
    for _ in range(8):
        book.update("base", "TOKENA", 0.135)  # cross down through 0.14
        eng.tick()
        book.update("base", "TOKENA", 0.145)  # cross back up through 0.14
        eng.tick()
    assert len(g.grid_lots) == 0
    buys = sum(1 for t in pf.trades if t.side == "buy" and t.strategy_id == g.id)
    assert buys == 0


def test_event_levels():
    eng, book, pf = _engine()
    book.update("base", "TOKENA", 0.12)
    # trigger fire → alert
    s = eng.submit(parse_command(
        "Buy $50 of TokenA if the price goes below $0.1"))
    eng.tick()
    book.update("base", "TOKENA", 0.09)
    eng.tick()
    assert any(e["level"] == "alert" and "trigger hit" in e["msg"] for e in eng.events)

    # risk block → error
    eng.cfg.risk = RiskConfig(max_daily_spend_usd=10)
    eng.submit(parse_command("buy $50 of TOKENA"))
    eng.tick()
    assert any(e["level"] == "error" and "blocked" in e["msg"] for e in eng.events)

    # strategy error → error (force insufficient cash on a market buy after draining)
    eng2, book2, pf2 = _engine()
    book2.update("base", "TOKENA", 0.10)
    pf2.cash_usd = 1.0
    s2 = eng2.submit(parse_command("buy $500 of TOKENA"))
    eng2.tick()
    assert s2.status == "error"
    assert any(e["level"] == "error" and s2.id in e["msg"] for e in eng2.events)


def test_dotenv_and_rpc_interpolation():
    import os
    import tempfile
    from tradebot.config import describe_rpc_sources, load_config, load_dotenv

    root = Path(tempfile.mkdtemp())
    envf = root / ".env"
    envf.write_text(
        "# comment\n\n"
        "QUOTED=\"hello world\"\n"
        "SINGLE='xyz'\n"
        "export EXPORTED=yes\n"
        "PRESET=fromfile\n"
        "PULSE_BASE_RPC=https://example.invalid/from-dotenv\n",
        encoding="utf-8",
    )
    for k in ("QUOTED", "SINGLE", "EXPORTED", "PULSE_BASE_RPC",
              "PULSE_ROBINHOOD_RPC", "PULSE_TEST_RPC"):
        os.environ.pop(k, None)
    os.environ["PRESET"] = "fromenv"
    load_dotenv(envf)
    assert os.environ["QUOTED"] == "hello world"
    assert os.environ["SINGLE"] == "xyz"
    assert os.environ["EXPORTED"] == "yes"
    assert os.environ["PRESET"] == "fromenv"  # existing env wins

    yaml_path = root / "cfg.yaml"
    yaml_path.write_text(
        "bot:\n  mode: paper\n"
        "chains:\n  base:\n    name: Base\n    chain_id: 8453\n"
        "    rpc_url: ${PULSE_BASE_RPC}\n    tokens: []\n",
        encoding="utf-8",
    )
    cfg = load_config(str(yaml_path), dotenv_path=envf)
    assert cfg.chains["base"].rpc_url == "https://example.invalid/from-dotenv"
    assert cfg.chains["base"].rpc_env_var == "PULSE_BASE_RPC"
    src = "\n".join(describe_rpc_sources(cfg))
    assert "PULSE_BASE_RPC" in src
    assert "example.invalid" not in src  # never echo the URL

    os.environ["PULSE_TEST_RPC"] = "https://example.invalid/from-env"
    yaml2 = root / "cfg2.yaml"
    yaml2.write_text(
        "bot:\n  mode: paper\n"
        "chains:\n  base:\n    name: Base\n    chain_id: 8453\n"
        "    rpc_url: ${PULSE_TEST_RPC}\n    tokens: []\n",
        encoding="utf-8",
    )
    cfg2 = load_config(str(yaml2), dotenv_path=root / "missing.env")
    assert cfg2.chains["base"].rpc_url == "https://example.invalid/from-env"

    # live mode: missing var → clear error naming the variable
    os.environ.pop("PULSE_MISSING_RPC", None)
    live_yaml = root / "live.yaml"
    live_yaml.write_text(
        "bot:\n  mode: live\n"
        "chains:\n  base:\n    name: Base\n    chain_id: 8453\n"
        "    rpc_url: ${PULSE_MISSING_RPC}\n    tokens: []\n",
        encoding="utf-8",
    )
    try:
        load_config(str(live_yaml), dotenv_path=root / "missing.env")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "PULSE_MISSING_RPC" in str(e)
        assert "live mode" in str(e).lower()

    # paper mode boots with everything unset (example config)
    for k in ("PULSE_BASE_RPC", "PULSE_ROBINHOOD_RPC"):
        os.environ.pop(k, None)
    paper = load_config("config.example.yaml",
                        dotenv_path=root / "missing.env")
    assert paper.mode == "paper"
    assert paper.chains["base"].rpc_url == ""
    assert paper.chains["robinhood"].rpc_url == ""
    assert paper.chains["base"].rpc_env_var == "PULSE_BASE_RPC"


def test_encode_v3_path_and_plan_route():
    from tradebot.chains import encode_v3_path, plan_route
    from tradebot.config import ChainConfig, TokenConfig

    usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth = "0x4200000000000000000000000000000000000006"
    tok = "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed"
    # Known packing: addr20 + fee3 + addr20 + fee3 + addr20
    packed = encode_v3_path([usdc, weth, tok], [500, 3000])
    expected = (
        bytes.fromhex(usdc[2:])
        + (500).to_bytes(3, "big")
        + bytes.fromhex(weth[2:])
        + (3000).to_bytes(3, "big")
        + bytes.fromhex(tok[2:])
    )
    assert packed == expected
    assert packed.hex() == expected.hex()

    chain = ChainConfig(
        name="Base", chain_id=8453, rpc_url="",
        quote_token=usdc, weth_token=weth, v2_router="0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        weth_usdc_fee=500,
    )
    direct = TokenConfig(symbol="T", address=tok, pool_fee=3000,
                         pool_type="v3", route="direct")
    weth_tok = TokenConfig(symbol="D", address=tok, pool_fee=3000,
                           pool_type="v3", route="weth")
    v2_weth = TokenConfig(symbol="D2", address=tok, pool_fee=3000,
                          pool_type="v2", route="weth")

    buy_d = plan_route("buy", direct, chain)
    assert buy_d.path == [usdc, tok] and buy_d.fees == [3000] and buy_d.pool_type == "v3"
    sell_d = plan_route("sell", direct, chain)
    assert sell_d.path == [tok, usdc] and sell_d.fees == [3000]

    buy_w = plan_route("buy", weth_tok, chain)
    assert buy_w.path == [usdc, weth, tok] and buy_w.fees == [500, 3000]
    sell_w = plan_route("sell", weth_tok, chain)
    assert sell_w.path == [tok, weth, usdc] and sell_w.fees == [3000, 500]

    buy_v2 = plan_route("buy", v2_weth, chain)
    assert buy_v2.pool_type == "v2" and buy_v2.path == [usdc, weth, tok]
    sell_v2 = plan_route("sell", v2_weth, chain)
    assert sell_v2.path == [tok, weth, usdc]


def test_decode_transfer_ignores_intermediate_weth_hop():
    """Multi-hop receipt: WETH transfer + final token transfer — count only token."""
    from tradebot.chains import TRANSFER_TOPIC0, decode_transfer_amount

    recipient = "0x1111111111111111111111111111111111111111"
    weth = "0x4200000000000000000000000000000000000006"
    token = "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"
    other = "0x2222222222222222222222222222222222222222"

    def transfer_log(token_addr: str, to_addr: str, amount: int) -> dict:
        to_topic = "0x" + ("0" * 24) + to_addr[2:].lower()
        return {
            "address": token_addr,
            "topics": [TRANSFER_TOPIC0,
                       "0x" + "0" * 64,  # from (ignored)
                       to_topic],
            "data": hex(amount),
        }

    logs = [
        transfer_log(weth, recipient, 10**18),          # intermediate hop
        transfer_log(token, recipient, 123456789),      # final out
        transfer_log(token, other, 999),                # wrong recipient
    ]
    got = decode_transfer_amount(logs, token, recipient)
    assert got == 123456789
    assert decode_transfer_amount(logs, weth, recipient) == 10**18


def test_pool_route_config_defaults():
    import tempfile
    from tradebot.config import load_config

    root = Path(tempfile.mkdtemp())
    p = root / "c.yaml"
    p.write_text(
        "bot:\n  mode: paper\n"
        "chains:\n  base:\n    name: Base\n    chain_id: 8453\n"
        "    rpc_url: ''\n"
        "    quote_token: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'\n"
        "    tokens:\n"
        "      - symbol: PLAIN\n        address: '0xabc'\n"
        "      - symbol: DEGEN\n        address: '0xdef'\n"
        "        pool_type: v2\n        route: weth\n        pool_fee: 10000\n"
        "    weth_token: '0x4200000000000000000000000000000000000006'\n"
        "    v2_router: '0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24'\n"
        "    weth_usdc_fee: 500\n",
        encoding="utf-8",
    )
    cfg = load_config(str(p), dotenv_path=root / "none")
    plain = cfg.chains["base"].tokens["PLAIN"]
    assert plain.pool_type == "v3" and plain.route == "direct"
    degen = cfg.chains["base"].tokens["DEGEN"]
    assert degen.pool_type == "v2" and degen.route == "weth" and degen.pool_fee == 10000
    assert cfg.chains["base"].weth_usdc_fee == 500
    assert cfg.chains["base"].weth_token.lower().startswith("0x4200")
    # example config defaults
    ex = load_config("config.example.yaml", dotenv_path=root / "none")
    ta = ex.chains["base"].tokens["TOKENA"]
    assert ta.pool_type == "v3" and ta.route == "direct"
    assert ex.chains["base"].weth_usdc_fee == 500
    assert ex.chains["base"].weth_token == ""  # commented in example


# ----- watch any CA -------------------------------------------------------

_WATCH_ADDR = "0x4ed4e862860bed51a9570b96d89af5e1b0efefed"
_QUOTE_ONLY = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _canned_pairs_payload():
    """Deep base-token pair + shallower base pair + quote-side match (ignore)."""
    return {
        "pairs": [
            {
                "chainId": "base",
                "dexId": "uniswap",
                "pairAddress": "0xpairdeep",
                "priceUsd": "0.00212",
                "liquidity": {"usd": 35_400_000},
                "baseToken": {"address": _WATCH_ADDR, "symbol": "DEGEN",
                              "name": "Degen"},
                "quoteToken": {"address": _QUOTE_ONLY, "symbol": "USDC"},
            },
            {
                "chainId": "base",
                "dexId": "aerodrome",
                "pairAddress": "0xpairshallow",
                "priceUsd": "0.00200",
                "liquidity": {"usd": 1_000_000},
                "baseToken": {"address": _WATCH_ADDR, "symbol": "DEGEN",
                              "name": "Degen"},
                "quoteToken": {"address": _QUOTE_ONLY, "symbol": "USDC"},
            },
            {
                # watched address is only the quote — must be ignored
                "chainId": "base",
                "dexId": "uniswap",
                "pairAddress": "0xquoteonly",
                "priceUsd": "1.0",
                "liquidity": {"usd": 99_000_000},
                "baseToken": {"address": "0xother", "symbol": "OTHER",
                              "name": "Other"},
                "quoteToken": {"address": _WATCH_ADDR, "symbol": "DEGEN"},
            },
            {
                "chainId": "ethereum",  # wrong chain
                "dexId": "uniswap",
                "pairAddress": "0xeth",
                "priceUsd": "0.01",
                "liquidity": {"usd": 99_000_000},
                "baseToken": {"address": _WATCH_ADDR, "symbol": "DEGEN",
                              "name": "Degen"},
                "quoteToken": {"address": _QUOTE_ONLY, "symbol": "USDC"},
            },
        ]
    }


def _low_liq_payload():
    return {
        "pairs": [{
            "chainId": "base",
            "dexId": "uniswap",
            "pairAddress": "0xlow",
            "priceUsd": "0.05",
            "liquidity": {"usd": 12_500},
            "baseToken": {"address": _WATCH_ADDR, "symbol": "THIN",
                          "name": "ThinCoin"},
            "quoteToken": {"address": _QUOTE_ONLY, "symbol": "USDC"},
        }]
    }


def _watch_engine(fetch=None):
    cfg = BotConfig(chains={"base": ChainConfig(
        name="Base", chain_id=8453, rpc_url="",
        dexscreener_slug="base",
        tokens={"TOKENA": TokenConfig(symbol="TOKENA")})})
    cfg.min_slice_usd = 5
    book = PriceBook()
    pf = Portfolio(cash_usd=10_000)
    return Engine(cfg, book, pf, None, {},
                  registry_fetch=fetch or (lambda url: _canned_pairs_payload())), book, pf


def test_watch_parse():
    s = parse_command(f"watch {_WATCH_ADDR}")
    assert s.kind == "watch" and s.address.lower() == _WATCH_ADDR
    s = parse_command(f"add token {_WATCH_ADDR} on base")
    assert s.kind == "watch" and s.chain == "base"
    s = parse_command(_WATCH_ADDR)
    assert s.kind == "watch" and s.address.lower() == _WATCH_ADDR
    s = parse_command("unwatch DEGEN")
    assert s.kind == "unwatch" and s.token == "DEGEN"
    s = parse_command("remove DEGEN-2 on robinhood")
    assert s.kind == "unwatch" and s.token == "DEGEN-2" and s.chain == "robinhood"


def test_registry_picks_deepest_base_pair():
    from tradebot.registry import resolve
    chain = ChainConfig(name="Base", chain_id=8453, rpc_url="",
                        dexscreener_slug="base")
    info = resolve(_WATCH_ADDR, chain, fetch=lambda url: _canned_pairs_payload())
    assert info["pair_address"] == "0xpairdeep"
    assert info["symbol"] == "DEGEN"
    assert abs(info["liquidity_usd"] - 35_400_000) < 1
    assert abs(info["price_usd"] - 0.00212) < 1e-9


def test_watch_then_market_buy():
    eng, book, pf = _watch_engine()
    w = parse_command(f"watch {_WATCH_ADDR}")
    eng.submit(w)
    assert "DEGEN" in eng.cfg.chains["base"].tokens
    assert ("base", "DEGEN") in eng.watched_keys
    assert book.price("base", "DEGEN") is not None
    assert "watching DEGEN" in w.describe() and "uniswap" in w.describe()
    # not registered with PaperFeed (none) — live source by address+slug
    from tradebot.prices import has_live_price_source
    assert has_live_price_source(
        eng.cfg.chains["base"].tokens["DEGEN"], eng.cfg.chains["base"])
    s = eng.submit(parse_command("buy $50 of DEGEN"))
    eng.tick()
    assert s.fills == 1
    assert abs(pf.trades[-1].usd - 50) < 1e-6
    assert pf.trades[-1].token == "DEGEN"


def test_unwatch_blocked_by_strategy():
    eng, book, pf = _watch_engine()
    eng.submit(parse_command(f"watch {_WATCH_ADDR}"))
    eng.submit(parse_command(
        "buy DEGEN at a rate of $60 per minute until a total of $120"))
    try:
        eng.submit(parse_command("unwatch DEGEN"))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "strategy" in str(e).lower()
    # config token refused
    try:
        eng.submit(parse_command("unwatch TOKENA"))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "config" in str(e).lower()


def test_watched_tokens_survive_store_roundtrip():
    import tempfile
    from tradebot.store import Store
    root = Path(tempfile.mkdtemp())
    db = root / "w.db"
    store = Store(db)
    eng, book, pf = _watch_engine()
    eng.store = store
    store.set_on_error(lambda msg: eng.log(msg))
    eng.submit(parse_command(f"watch {_WATCH_ADDR}"))
    assert store.load_watched_tokens()
    # new engine + restore
    cfg2 = BotConfig(chains={"base": ChainConfig(
        name="Base", chain_id=8453, rpc_url="",
        dexscreener_slug="base",
        tokens={"TOKENA": TokenConfig(symbol="TOKENA")})})
    book2 = PriceBook()
    pf2 = Portfolio(cash_usd=10_000)
    store2 = Store(db)
    eng2 = Engine(cfg2, book2, pf2, None, {}, store=store2)
    eng2.restore()
    assert "DEGEN" in eng2.cfg.chains["base"].tokens
    assert ("base", "DEGEN") in eng2.watched_keys
    tok = eng2.cfg.chains["base"].tokens["DEGEN"]
    assert tok.address.lower() == _WATCH_ADDR
    assert tok.dexscreener_pair == "0xpairdeep"
    # strategy on watched symbol validates after restore
    s = eng2.submit(parse_command("buy $10 of DEGEN"))
    assert s is not None and s.spec.token == "DEGEN"


def test_watch_low_liquidity_alert():
    eng, book, pf = _watch_engine(fetch=lambda url: _low_liq_payload())
    w = parse_command(f"watch {_WATCH_ADDR}")
    eng.submit(w)
    assert "low liquidity" in w.describe().lower()
    assert any(e["level"] == "alert" and "low liquidity" in e["msg"]
               for e in eng.events)


def test_paper_feed_defers_to_live_source():
    """Address+slug → Dexscreener only; slugless (even with address) stays simulated."""
    cfg = BotConfig(chains={
        "base": ChainConfig(
            name="Base", chain_id=8453, rpc_url="",
            dexscreener_slug="base",
            tokens={
                "LIVE": TokenConfig(symbol="LIVE", address="0xabc"),
                "PAIR": TokenConfig(symbol="PAIR", dexscreener_pair="0xpair"),
                "SIM": TokenConfig(symbol="SIM"),
            },
        ),
        "robinhood": ChainConfig(
            name="RH", chain_id=4663, rpc_url="",
            dexscreener_slug="",
            tokens={
                "ADDR": TokenConfig(symbol="ADDR", address="0xdef"),
            },
        ),
    })
    assert has_live_price_source(cfg.chains["base"].tokens["LIVE"], cfg.chains["base"])
    assert has_live_price_source(cfg.chains["base"].tokens["PAIR"], cfg.chains["base"])
    assert not has_live_price_source(cfg.chains["base"].tokens["SIM"], cfg.chains["base"])
    assert not has_live_price_source(
        cfg.chains["robinhood"].tokens["ADDR"], cfg.chains["robinhood"])

    book = PriceBook()
    feed = PaperFeed(book, cfg)
    _wire_paper_feed(feed, cfg)
    assert ("base", "LIVE") not in feed._state
    assert ("base", "PAIR") not in feed._state
    assert ("base", "SIM") in feed._state
    assert ("robinhood", "ADDR") in feed._state
    assert book.price("base", "LIVE") is None
    assert book.price("base", "SIM") is not None


def test_apply_impact_unregistered_noop():
    cfg = BotConfig(chains={"base": ChainConfig(
        name="Base", chain_id=8453, rpc_url="",
        tokens={"TOKENA": TokenConfig(symbol="TOKENA")})})
    book = PriceBook()
    feed = PaperFeed(book, cfg)
    # never registered — must not crash or invent state
    feed.apply_impact("base", "NOSUCH", 50)
    assert ("base", "NOSUCH") not in feed._state
    assert book.price("base", "NOSUCH") is None
    feed.register("base", cfg.chains["base"].tokens["TOKENA"])
    before = book.price("base", "TOKENA")
    feed.apply_impact("base", "TOKENA", 100)  # +1%
    assert abs(book.price("base", "TOKENA") - before * 1.01) < 1e-12


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")

"""Run with: python -m pytest tests/ (or just python tests/test_pulse.py)"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tradebot.commands import parse_command
from tradebot.config import BotConfig, ChainConfig, TokenConfig
from tradebot.portfolio import Portfolio
from tradebot.prices import PriceBook
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all tests passed")

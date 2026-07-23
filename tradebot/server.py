"""FastAPI app: command intake, state stream, dashboard hosting."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .commands import ParseError, parse_command, parse_with_claude
from .config import BotConfig, load_config
from .portfolio import Portfolio
from .prices import DexscreenerFeed, PaperFeed, PriceBook, has_live_price_source
from .store import Store
from .strategies import Engine

STATIC = Path(__file__).parent / "static"


class CommandIn(BaseModel):
    text: str


def _wire_paper_feed(paper_feed: PaperFeed, cfg: BotConfig) -> None:
    """Register only simulated tokens; live-sourced ones stay on Dexscreener."""
    for chain_key, chain in cfg.chains.items():
        for tok in chain.tokens.values():
            if has_live_price_source(tok, chain):
                print(f"{tok.symbol} ({chain_key}): live via dexscreener")
            else:
                paper_feed.register(chain_key, tok)
                print(f"{tok.symbol} ({chain_key}): simulated")


async def _warn_missing_live_prices(engine: Engine, book: PriceBook,
                                    cfg: BotConfig) -> None:
    """One-time error events for live-sourced tokens that never got a quote."""
    await asyncio.sleep(30.0)
    for chain_key, chain in cfg.chains.items():
        for tok in chain.tokens.values():
            if not has_live_price_source(tok, chain):
                continue
            if book.price(chain_key, tok.symbol) is None:
                engine.log(
                    f"{tok.symbol} ({chain_key}): no live price after 30s — "
                    f"check address or dexscreener_pair",
                    level="error",
                )


def build_app(config_path: str | None = None) -> FastAPI:
    cfg = load_config(config_path)
    book = PriceBook()
    portfolio = Portfolio(cash_usd=cfg.paper_cash_usd if cfg.mode == "paper" else 0.0)

    paper_feed = None
    live_clients: dict = {}
    if cfg.mode == "paper":
        paper_feed = PaperFeed(book, cfg)
        _wire_paper_feed(paper_feed, cfg)
    else:
        from .chains import ChainClient
        for chain_key, chain in cfg.chains.items():
            live_clients[chain_key] = ChainClient(
                name=chain.name, rpc_url=chain.rpc_url, chain_id=chain.chain_id,
                router=chain.router, quoter=chain.quoter,
                quote_token=chain.quote_token, quote_decimals=chain.quote_decimals,
                private_key=cfg.private_key,
                v2_router=chain.v2_router, weth_token=chain.weth_token)

    store = Store(cfg.db_path)
    engine = Engine(cfg, book, portfolio, paper_feed, live_clients, store=store)
    engine.restore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [asyncio.create_task(engine.run())]
        if paper_feed:
            tasks.append(asyncio.create_task(paper_feed.run()))
        if any(c.dexscreener_slug for c in cfg.chains.values()):
            tasks.append(asyncio.create_task(DexscreenerFeed(book, cfg).run()))
        if any(has_live_price_source(tok, chain)
               for chain in cfg.chains.values()
               for tok in chain.tokens.values()):
            tasks.append(asyncio.create_task(
                _warn_missing_live_prices(engine, book, cfg)))
        yield
        for t in tasks:
            t.cancel()
        store.close()

    app = FastAPI(title="Pulse", lifespan=lifespan)
    app.state.engine = engine
    app.state.cfg = cfg
    app.state.book = book
    app.state.store = store

    # ------------------------------------------------------------- routes

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/command")
    async def command(body: CommandIn):
        try:
            spec = parse_command(body.text)
        except ParseError as first_err:
            if cfg.anthropic_parser:
                try:
                    spec = await asyncio.to_thread(parse_with_claude, body.text)
                except Exception as e:
                    return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
            else:
                return JSONResponse({"ok": False, "error": str(first_err)}, status_code=400)
        try:
            strat = engine.submit(spec)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return {"ok": True, "parsed": spec.describe(),
                "id": strat.id if strat else None}

    @app.post("/api/strategy/{sid}/cancel")
    async def cancel(sid: str):
        return {"ok": engine.cancel(sid)}

    @app.post("/api/strategy/{sid}/pause")
    async def pause_strategy(sid: str):
        return {"ok": engine.set_paused(sid, True)}

    @app.post("/api/strategy/{sid}/resume")
    async def resume_strategy(sid: str):
        return {"ok": engine.set_paused(sid, False)}

    @app.get("/api/strategy/{sid}")
    async def strategy_detail(sid: str):
        d = engine.strategy_detail(sid)
        if d is None:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        return d

    @app.get("/api/state")
    async def state():
        return _snapshot(cfg, engine, book, portfolio)

    @app.get("/api/candles/{chain}/{token}")
    async def candles(chain: str, token: str):
        return book.history(chain, token)

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        try:
            while True:
                await sock.send_text(json.dumps(
                    _snapshot(cfg, engine, book, portfolio)))
                await asyncio.sleep(1.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    return app


def _snapshot(cfg: BotConfig, engine: Engine, book: PriceBook,
              portfolio: Portfolio) -> dict:
    tokens = []
    for chain_key, chain in cfg.chains.items():
        for tok in chain.tokens.values():
            tokens.append({"chain": chain_key, "chain_name": chain.name,
                           "token": tok.symbol,
                           "price": book.price(chain_key, tok.symbol)})
    return {
        "mode": cfg.mode,
        "paused": engine.paused,
        "chains": {k: {"name": c.name, "explorer": c.explorer}
                   for k, c in cfg.chains.items()},
        "tokens": tokens,
        "strategies": [s.as_dict(book.price(s.chain, s.spec.token))
                       for s in reversed(engine.strategies)],
        "stats": portfolio.stats(book.price),
        "equity": portfolio.equity_history[-600:],
        "trades": [t.as_dict() for t in portfolio.trades[-100:]][::-1],
        "events": engine.events[-60:][::-1],
    }

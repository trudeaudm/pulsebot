"""Run: python -m tradebot [--config PATH] [--port N] [--reset] [config.yaml]"""
from __future__ import annotations

import argparse
import errno
import socket
import sys

import uvicorn

from .config import describe_rpc_sources, load_config
from .server import build_app
from .store import archive_db


def parse_cli(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI. Positional config path still works; --config is the explicit form."""
    p = argparse.ArgumentParser(
        prog="python -m tradebot",
        description="Pulse trading engine",
    )
    p.add_argument(
        "config_positional",
        nargs="?",
        default=None,
        metavar="CONFIG",
        help="path to config.yaml (optional)",
    )
    p.add_argument(
        "--config",
        dest="config_opt",
        default=None,
        metavar="PATH",
        help="explicit config path (overrides positional)",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="N",
        help="override bot.port from config",
    )
    p.add_argument(
        "--reset",
        action="store_true",
        help="archive the existing database and start fresh",
    )
    return p.parse_args(argv)


def resolve_config_path(ns: argparse.Namespace) -> str | None:
    return ns.config_opt or ns.config_positional


def _is_addr_in_use(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        if getattr(exc, "winerror", None) == 10048:  # WSAEADDRINUSE
            return True
        if getattr(exc, "errno", None) in (
            errno.EADDRINUSE,
            getattr(errno, "WSAEADDRINUSE", 10048),
        ):
            return True
    return False


def _port_busy_message(port: int) -> str:
    return (
        f"port {port} already in use — is another Pulse running?\n"
        f"Try: python -m tradebot --port {port + 1}"
    )


def ensure_port_available(host: str, port: int) -> None:
    """Fail fast with a friendly message if the listen port is taken."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as e:
        if _is_addr_in_use(e):
            print(_port_busy_message(port), file=sys.stderr)
            raise SystemExit(1) from None
        raise
    finally:
        sock.close()


def main(argv: list[str] | None = None) -> None:
    ns = parse_cli(argv if argv is not None else sys.argv[1:])
    path = resolve_config_path(ns)
    cfg = load_config(path)
    if ns.port is not None:
        cfg.port = ns.port
    if ns.reset:
        archived = archive_db(cfg.db_path)
        if archived:
            print(f"  Archived database -> {archived}")
        else:
            print(f"  No database at {cfg.db_path} to reset")
    app = build_app(path)
    # build_app reloads config — re-apply CLI overrides onto the live app state
    live_cfg = app.state.cfg
    if ns.port is not None:
        live_cfg.port = ns.port
    banner = ("PAPER (simulated fills)" if live_cfg.mode == "paper"
              else "LIVE — real funds")
    print(f"\n  Pulse trading engine · mode: {banner}")
    print(f"  Dashboard: http://{live_cfg.host}:{live_cfg.port}")
    print(f"  Database:  {live_cfg.db_path}")
    for line in describe_rpc_sources(live_cfg):
        print(f"  {line}")
    print()
    ensure_port_available(live_cfg.host, live_cfg.port)
    try:
        uvicorn.run(
            app, host=live_cfg.host, port=live_cfg.port, log_level="warning")
    except OSError as e:
        if _is_addr_in_use(e):
            print(_port_busy_message(live_cfg.port), file=sys.stderr)
            raise SystemExit(1) from None
        raise


if __name__ == "__main__":
    main()

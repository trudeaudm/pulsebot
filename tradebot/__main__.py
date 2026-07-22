"""Run: python -m tradebot [config.yaml] [--reset]"""
from __future__ import annotations

import sys

import uvicorn

from .config import load_config
from .server import build_app
from .store import archive_db


def main() -> None:
    argv = sys.argv[1:]
    reset = "--reset" in argv
    args = [a for a in argv if a != "--reset"]
    path = args[0] if args else None
    cfg = load_config(path)
    if reset:
        archived = archive_db(cfg.db_path)
        if archived:
            print(f"  Archived database -> {archived}")
        else:
            print(f"  No database at {cfg.db_path} to reset")
    app = build_app(path)
    banner = "PAPER (simulated fills)" if cfg.mode == "paper" else "LIVE — real funds"
    print(f"\n  Pulse trading engine · mode: {banner}")
    print(f"  Dashboard: http://{cfg.host}:{cfg.port}")
    print(f"  Database:  {cfg.db_path}\n")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()

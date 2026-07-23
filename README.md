# Pulse — command-line trading engine for Base & Robinhood Chain

Type plain English, get executed strategies:

```
> sell TOKENA at a rate of $300 per minute while the price is above $0.15
> buy $450 of TOKENA if the price goes below $0.1 while the price is below $0.1
  continue to buy at a rate of $100 per minute until you have bought a total of $1200
```

Pulse parses commands into strategy state machines, executes them as streams of
child orders, and shows everything on a live dashboard: candlestick price chart,
equity curve, PnL / slippage / volume stats, strategy progress bars, trade tape,
and engine log.

Both chains are standard EVM (Base = 8453, Robinhood Chain = 4663 on Arbitrum
Nitro), so one execution path covers both: ERC-20 approvals + Uniswap-v3-style
`exactInputSingle` swaps against whatever router you configure per chain.

## Quick start (paper mode — no keys, no funds)

```bash
pip install -r requirements.txt
python -m tradebot
# open http://127.0.0.1:8420
```

Paper mode runs a mean-reverting price simulator per token, fills against it
with a fee + size-impact model, and your fills push the simulated price — so
you can rehearse trigger/rate strategies realistically before risking anything.

## Command grammar

| You type | It becomes |
|---|---|
| `buy $200 of TOKENA` | market buy |
| `sell 1500 TOKENA` | market sell of token units |
| `sell all TOKENA` | close position |
| `buy $500 of TOKENA at $0.09` | limit order |
| `sell all TOKENA if the price drops below $0.08` | stop |
| `sell all TOKENA if the price falls 10% from its high` | trailing stop |
| `trailing stop 10% on TOKENA` | trailing stop (sell all) |
| `grid TOKENA between $0.08 and $0.14 with 7 levels, $50 per level` | synthetic grid |
| `stop loss at $0.09 for $500 of TOKENA` | partial stop |
| `sell TOKENA at a rate of $300 per minute while the price is above $0.15` | gated rate stream (TWAP) |
| `buy $450 of TOKENA if the price goes below $0.1 … $100 per minute … total of $1200` | trigger + DCA with cap |
| `… on robinhood` / `… on base` | route to a specific chain |
| `cancel all`, `pause`, `resume` | engine controls |

Rates accept `per second / minute / hour`. Rate strategies accrue budget
continuously and flush a child order each time the accrued amount crosses
`min_slice_usd`, so "$300/min" is a stream of small orders, not one lurch.
The `while price …` gate freezes accrual whenever the condition is false.

### Grid strategy

A grid places evenly spaced price levels between a lower and upper bound.
Crossing a level downward buys `$X` at that level (if no lot is held there);
crossing the next level up sells that lot. The top level is sell-only (no buys).
Inventory is tracked per level, survives restarts, and the strategy runs until
you cancel it (lots are left untouched on cancel). Risk caps apply to grid
orders like any other fill.

### Command bar

ArrowUp / ArrowDown recall the last 50 successfully parsed commands (session
only). While typing, Tab (or ArrowRight at the end of the line) accepts a
ghost completion for configured token symbols and verbs
(`buy` / `sell` / `grid` / `trailing` / `cancel` / `pause` / `resume`).
Escape dismisses the ghost.

### Alerts

Trigger fires, trailing-stop fires, cap completions, and grid sells emit
`alert`-level engine events; risk blocks and strategy errors emit `error`.
After you submit your first command, the dashboard may ask for notification
permission and will flash the engine log (and notify, if allowed) on new
alert/error events.

Set `anthropic_parser: true` (plus `ANTHROPIC_API_KEY`) and any phrasing the
grammar can't match is parsed by the Claude API into the same schema.

## Going live

1. `cp config.example.yaml config.yaml`, fill in token addresses, decimals,
   and pool fee tiers. Base ships pre-wired with Uniswap v3 SwapRouter02,
   QuoterV2, and USDC. For Robinhood Chain, paste the router/quoter/stable
   addresses of the DEX you trade there (any v3-style deployment works).
2. Use a **dedicated RPC** (Alchemy, QuickNode, Chainstack, Dwellir…). The
   public endpoints are rate-limited and will throttle a bot.
3. Put the key of a **dedicated hot wallet** in an env var — never in the file:
   `export PULSE_PRIVATE_KEY=0x...`
4. Set `mode: live` in config.yaml and start. Fills include tx hashes linked
   from the trade tape.

Live fills are reconciled from the swap receipt's ERC-20 `Transfer` logs
(actual `amountOut`), not the slippage floor. If log decode fails, the fill
falls back to `amountOutMinimum` and the price is shown with a `~` prefix.

`max_slippage_bps` caps how far a live fill may deviate from the reference
price; swaps revert instead of filling worse.

## Persistence

Trades, strategies, equity samples, and portfolio meta are write-through
persisted to SQLite (`bot.db_path`, default `pulse.db`, already gitignored).
Restarting the process restores cash/positions (by replaying the trade ledger),
equity history, and every strategy with its original ID (`S1`, `S2`, …).

On resume, `accrued_usd` is reset to 0 and `last_tick` is set to now so downtime
never causes a burst of catch-up child orders. In **paper** mode active strategies
keep running; in **live** mode they come back paused (with a note in the engine
log) until you resume them.

Wipe the ledger and start clean:

```bash
python -m tradebot --reset
# or with an explicit config:
python -m tradebot config.yaml --reset
```

`--reset` renames the existing DB to `pulse.db.<timestamp>` (archives it) and
boots against a fresh file.

## Risk limits

Optional guardrails under `bot.risk` in config (all default to `0` = off):

| Key | Effect |
|---|---|
| `max_open_notional_usd_per_token` | Blocks **buys** that would push mark-to-market position value above the limit for that (chain, token). Sells are never blocked by this. |
| `max_daily_spend_usd` | Blocks any child order when trailing-24h `trade.usd` sum plus this order would exceed the cap. |
| `default_cap_usd_for_uncapped` | At submit, assigns this as `total_cap_usd` for rate / triggered_rate commands that omitted `until a total of $X`. |

A blocked strategy stays `active` with a `blocked_reason` (amber on the card and
detail overlay). Rate strategies do not stockpile accrual while blocked — budget
is capped at one slice — so lifting the limit does not cause a burst. Engine log
lines for blocks are rate-limited to once per minute per strategy.

Live fills with a `tx_hash` link to `{explorer}/tx/{hash}` from each chain's
`explorer` URL in config (trade tape + strategy fill history). Paper fills keep
showing `paper`.

## Safety notes — please read

- **Start in paper mode.** Then go live with small caps on a token you know.
- Use a fresh wallet holding only what the bot may spend. The key never
  leaves your machine, but treat any hot key as expendable.
- Rate strategies with no `total of $X` cap run until cancelled or you run
  out of balance — prefer capped commands.
- On-chain trading carries MEV/sandwich risk on public mempools; keep slippage
  tight and slices small, or point the router config at a protected RPC.
- Robinhood Chain's tokenized-stock assets carry jurisdiction restrictions;
  make sure whatever you trade there is available to you.
- Nothing here is financial advice; the strategies do exactly what you type.

## Theme

Charcoal primary with a chain-keyed electric accent: **Base blue** (`#0052FF`)
when the selected market is on Base, **Robinhood green** (`#00C805`) on
Robinhood Chain. Switching chain tabs re-themes the shell (command bar, tabs,
progress bars, equity curve); every strategy card carries its own chain's
accent on the left edge regardless of the selected tab.

## Working on Pulse with Cursor

The repo ships Cursor-ready:

- `.cursor/rules/pulse.mdc` — always-applied project rules: architecture map,
  hard invariants (key handling, paper/live parity, engine resilience), and
  the definition of done for any change.
- `ROADMAP.md` — numbered backlog that prompts reference.
- Verify loop: `python3 tests/test_pulse.py`, then boot
  `python3 -m tradebot config.example.yaml` and check http://127.0.0.1:8420.

## Layout

```
tradebot/
  commands.py    NL parser (regex grammar + optional Claude fallback)
  strategies.py  strategy state machines + engine loop + executors
  prices.py      paper simulator, Dexscreener poller, candle store
  chains.py      web3 adapter: ERC-20, QuoterV2, exactInputSingle swaps
  portfolio.py   balances, cost basis, realized/unrealized PnL, stats
  store.py       SQLite write-through persistence + restart resume
  server.py      FastAPI REST + WebSocket state stream
  static/        dashboard (single file, lightweight-charts)
tests/           parser + engine tests (python tests/test_pulse.py)
```

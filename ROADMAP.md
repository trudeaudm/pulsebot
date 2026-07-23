# Pulse roadmap

Prioritized backlog. Cursor prompts will reference items by number.
Each item should land with tests and a README update per `.cursor/rules/pulse.mdc`.

## P1 — durability & trust
1. **SQLite persistence + restart resume** (done) — trades, equity history, and
   strategy state survive restarts; active strategies resume armed/streaming.
2. **Explorer links** (done) — trade tape and fill history link tx hashes to the
   per-chain explorer (Basescan / Robinhood Blockscout) from config.
3. **Risk limits** (done) — config-level guardrails: max open notional per token,
   max daily spend, per-strategy default cap when none given.

## P2 — execution quality
4. **Live fill reconciliation** (done) — decode actual `amountOut` from swap receipt
   logs instead of using the slippage floor; record realized vs quoted.
5. **On-chain quoter pricing** — in live mode, mark prices from QuoterV2
   alongside Dexscreener; use the more recent of the two.
6. **Smarter slicing** — randomized slice sizes/jitter within bounds to
   reduce predictability of rate streams.

## P3 — strategy surface
7. **Trailing stop** (done) — "sell all X if price falls 10% from its high".
8. **Grid strategy** (done) — "grid TOKENA between $0.08 and $0.14 with 7 levels, $50 per level".
9. **Strategy templates** — save a card as a template; one-click re-run with
   the same spec.

## P4 — interface
10. **Command history & autocomplete** (done) — up-arrow recall, token symbol completion.
11. **Alerts** (done) — browser notification on trigger fire / cap complete / error.
12. **Mobile pass** — cards and detail overlay audited below 420px.

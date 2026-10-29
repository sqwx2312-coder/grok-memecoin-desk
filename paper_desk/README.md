# memedesk — PAPER-TRADING-ONLY Solana memecoin desk

**No real funds. No private keys. No wallet. No transaction is ever built, signed or sent.**
Live trading is out of scope; there is no code path for it.

Everything here is relative to this folder. The public Solana RPC is `https://api.mainnet-beta.solana.com` unless `SOLANA_RPC` is set.

## Run / stop
* Optional venv. Third-party imports are `requests` and `websockets` (`requirements.txt`). `summary.py` and `dashboard_data.py` are stdlib only.
  `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
* `./run.sh` uses `.venv/bin/python` when that interpreter exists, otherwise `python3`. It is the outer restart loop around `supervisor.py` (restarts any crashed stage; kills a stage whose heartbeat is stale >5 min), which runs the 6 stage processes.
* Start: `tmux new-session -d -s memedesk ./run.sh` (attach with `tmux attach -t memedesk`, detach with Ctrl-b d).
* Stop: `./stop.sh`.
* PIDs: `cat pids.json`.
* Stats: `python3 summary.py` (stdlib only). With the venv: `.venv/bin/python summary.py`.
* Dashboard JSON: `python3 dashboard_data.py`.
* Logs: `logs/` — `kills.log`, `scores.log`, `trades.log`, `events.jsonl` (written by Logger), plus one log per stage. DB: `memedesk.db` (SQLite WAL).

## Pipeline (6 processes, hard handoffs via the SQLite `messages` table: src, dst, kind, payload, claimed_ts, done_ts)
1. **Scout** -> filter `new_pair`. PumpPortal WS (`subscribeNewToken` gives `pool=pump` PumpFun and `pool=bonk` BonkFun; `subscribeMigration` gives PumpSwap graduations), auto-reconnect with backoff. GeckoTerminal `new_pools` every 30s for Raydium (incl. LaunchLab) and Meteora (DLMM/DAMM/DBC).
2. **Filter** -> analyst `candidate`, logger `kill`. Steps: (a) batched RPC mint/freeze-authority check; (b) **volume watch window**: a pair stays on the watchlist up to 60 min and its DexScreener h1 volume is rechecked every 60s. It goes to deep checks as soon as h1 >= $40k, and is killed `volume_lt_40k_1h` if it never gets there within 60 min. New pairs almost never do $40k/h at birth, so killing on the first look would kill everything. (c) deep checks (RugCheck report + RPC) only for pairs that pass the volume gate. All failing reasons are logged, not just the first.
3. **Analyst** -> executor `signal`. Weighted 0..1 score (formula in `stages/analyst.py` docstring); < 0.60 is killed and logged.
4. **Executor** -> risk `order_intent`. Sizes at min(10% of paper equity, cash), with no re-entry on the same mint. It **refuses to fill without a Risk stop plan**. Fills use the DexScreener mark ± (1% fee + 0.5% slip + impact size/(liq/2), capped at 25%) per side.
5. **Risk** -> executor `order_approved` / `exit_order`. It writes stop -8%, trail trigger +15% and trail distance 8% onto the position *before* the fill, and enforces max 10 open positions. It monitors every 10s. **Trailing stop: once the peak reaches +15%, stop = max(stop, peak × 0.92)**, which locks in at least about +5.8% on the mark. A gap through the stop fills at the observed price. If there is no price for 15 min, it exits as `stale_price`.
6. **Logger** — the only writer of the `kills`, `scores` and `trade_log` tables and of the log files.

Bankroll: 10 SOL notional, converted to USD at the SOL price when it started (stored in `account`).

## Gate accuracy
| gate | source | accuracy |
|---|---|---|
| mint authority revoked | Solana RPC getMultipleAccounts + RugCheck | exact |
| freeze authority revoked (extra gate) | same | exact |
| LP locked/burned >=95% | RugCheck markets[].lp.lpLockedPct (main market). Bonding-curve markets have no LP and count as locked | exact-ish (depends on RugCheck locker coverage) |
| top-10 holders <= 25% | RugCheck topHolders minus AMM/curve/locker/pool accounts | exact-ish |
| h1 volume >= $40k | DexScreener, summed across pairs | exact (60-min watch window) |
| dev wallet tied to rugs | RugCheck "Creator history of rugged tokens" + local memory of creators of rugged tokens | **approximate** |
| bundled launch | >=4 txs in the mint's creation slot (RPC getSignaturesForAddress paging, max 10k sigs) + RugCheck insider-flagged holders >=5% or bundle/insider danger risk | **approximate** |

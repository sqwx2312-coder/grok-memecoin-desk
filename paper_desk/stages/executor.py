"""Stage 4 — EXECUTOR (PAPER ONLY — there is no wallet, key, signer or transaction code here).
Input: analyst->executor signal, risk->executor order_approved/order_rejected/exit_order.
Sizing: min(10% of paper equity, cash). Orders go to RISK for a stop plan BEFORE any fill.
Fill model per side: mark * (1 ± (FEE 1% + base slip 0.5% + impact)), impact = size_usd / (liq_usd/2), capped 25%.
"""
import time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (db, init_db, send, claim, done, log_event, dex_tokens, best_pair, get_logger, run_forever,
                    acct_get, acct_set, sol_usd, CFG)

lg = get_logger("executor")

def cost_pct(size_usd, liq):
    impact = min(0.25, size_usd / max(liq / 2, 1.0))
    return CFG["FEE_PCT"] + CFG["BASE_SLIP_PCT"] + impact

def mark_for(mint, pair):
    pairs = dex_tokens([mint]).get(mint) or []
    exact = [p for p in pairs if p.get("pairAddress") == pair]
    bp = best_pair(exact or pairs)
    return (bp["_price"], bp["_liq"]) if bp and bp["_price"] > 0 else (None, None)

def equity(c):
    cash = acct_get(c, "cash_usd", 0.0)
    opn = c.execute("SELECT qty, last_mark, entry_mark FROM positions WHERE status IN ('open','exiting')").fetchall()
    return cash + sum(r["qty"] * (r["last_mark"] or r["entry_mark"]) for r in opn), cash

def ensure_account(c):
    if acct_get(c, "start_usd") is None:
        px = None
        for _ in range(5):
            px = sol_usd()
            if px: break
            time.sleep(3)
        px = px or 150.0
        start = CFG["START_BANKROLL_SOL"] * px
        acct_set(c, "sol_usd_at_start", px); acct_set(c, "start_sol", CFG["START_BANKROLL_SOL"])
        acct_set(c, "start_usd", start); acct_set(c, "cash_usd", start); acct_set(c, "started_ts", time.time())
        lg.info("paper bankroll initialised: %.2f SOL = $%.2f (SOL=$%.2f)", CFG["START_BANKROLL_SOL"], start, px)

def step(c):
    for m in claim(c, "executor", 20):
        mint, p, kind = m["mint"], m["payload"], m["kind"]
        try:
            if kind == "signal":
                if c.execute("SELECT 1 FROM positions WHERE mint=?", (mint,)).fetchone():
                    log_event(c, "executor", "skip", mint, reason="already_traded_this_mint"); continue
                eq, cash = equity(c)
                size = min(CFG["MAX_TRADE_FRAC"] * eq, cash)
                if size < 1.0:
                    log_event(c, "executor", "skip", mint, reason="insufficient_paper_cash", cash=cash); continue
                cur = c.execute("INSERT INTO positions(mint,symbol,pair,status,intent_ts,cost_usd,entry_mark) "
                                "VALUES(?,?,?,'intent',?,?,?)", (mint, p.get("symbol"), p.get("pair"), time.time(), size, p.get("price")))
                send(c, "executor", "risk", "order_intent", mint, dict(position_id=cur.lastrowid, size_usd=size,
                                                                        ref_price=p.get("price"), score=p.get("score")))
            elif kind == "order_rejected":
                c.execute("UPDATE positions SET status='rejected', exit_reason=? WHERE id=?", (p.get("reason"), p["position_id"]))
                log_event(c, "executor", "skip", mint, reason="risk_rejected:" + str(p.get("reason")))
            elif kind == "order_approved":
                pos = c.execute("SELECT * FROM positions WHERE id=?", (p["position_id"],)).fetchone()
                if not pos or pos["status"] != "intent" or pos["stop_pct"] is None:
                    log_event(c, "executor", "skip", mint, reason="no_stop_plan_refusing_entry"); continue
                mark, liq = mark_for(mint, pos["pair"])
                if not mark:
                    c.execute("UPDATE positions SET status='rejected', exit_reason='no_price_at_fill' WHERE id=?", (pos["id"],))
                    log_event(c, "executor", "skip", mint, reason="no_price_at_fill"); continue
                cash = acct_get(c, "cash_usd")
                size = min(pos["cost_usd"], cash)
                fill = mark * (1 + cost_pct(size, liq))
                qty = size / fill
                stop_price = mark * (1 - pos["stop_pct"])
                c.execute("UPDATE positions SET status='open', entry_ts=?, entry_mark=?, entry_fill=?, qty=?, cost_usd=?, "
                          "stop_price=?, peak=?, last_mark=?, last_mark_ts=? WHERE id=?",
                          (time.time(), mark, fill, qty, size, stop_price, mark, mark, time.time(), pos["id"]))
                acct_set(c, "cash_usd", cash - size)
                send(c, "executor", "risk", "position_opened", mint, dict(position_id=pos["id"]))
                log_event(c, "executor", "trade", mint, event="entry", symbol=pos["symbol"], price=fill, size_usd=size,
                          detail=dict(mark=mark, liq=liq, slip_fee_pct=round(cost_pct(size, liq) * 100, 2),
                                      stop_price=stop_price, qty=qty))
            elif kind == "exit_order":
                pos = c.execute("SELECT * FROM positions WHERE id=?", (p["position_id"],)).fetchone()
                if not pos or pos["status"] != "exiting": continue
                mark, liq = mark_for(mint, pos["pair"])
                if not mark:
                    mark, liq = p.get("mark") or pos["last_mark"], 1.0  # stale: assume worst-case impact cap
                proceeds_gross = pos["qty"] * mark
                fill = mark * (1 - cost_pct(proceeds_gross, liq))
                proceeds = pos["qty"] * max(fill, 0)
                pnl = proceeds - pos["cost_usd"]; pnl_pct = pnl / pos["cost_usd"] * 100
                c.execute("UPDATE positions SET status='closed', exit_ts=?, exit_mark=?, exit_fill=?, proceeds_usd=?, "
                          "pnl_usd=?, pnl_pct=?, exit_reason=? WHERE id=?",
                          (time.time(), mark, fill, proceeds, pnl, pnl_pct, p.get("reason"), pos["id"]))
                acct_set(c, "cash_usd", acct_get(c, "cash_usd") + proceeds)
                log_event(c, "executor", "trade", mint, event="exit", symbol=pos["symbol"], price=fill,
                          size_usd=proceeds, pnl_usd=pnl, pnl_pct=pnl_pct,
                          detail=dict(reason=p.get("reason"), mark=mark, entry_mark=pos["entry_mark"],
                                      peak=pos["peak"], held_min=round((time.time() - pos["entry_ts"]) / 60, 1)))
        finally:
            done(c, m["id"])

if __name__ == "__main__":
    init_db(); c = db(); ensure_account(c); c.close()
    run_forever("executor", step, 1, lg)

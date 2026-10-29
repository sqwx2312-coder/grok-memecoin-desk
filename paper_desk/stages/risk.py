"""Stage 5 — RISK. Sets the stop plan BEFORE entry, then guards every open paper position.
  * order_intent -> writes stop_pct=8%, trail_trigger=+15%, trail_dist=8% onto the position, checks limits
    (max 10 open, no duplicate mint), then approves/rejects back to EXECUTOR. Executor refuses to fill
    any order lacking a stop plan.
  * Monitor every 10s with DexScreener marks: hard stop at entry_mark*(1-8%). Once peak >= entry*(1+15%),
    trailing stop = max(stop, peak*(1-8%)) — i.e. it locks in at least ~+5.8% on the mark.
    Exits are evaluated on the observed mark (gaps fill at the observed price, not the stop price).
  * If no price for 15 min -> exit 'stale_price' at last mark with worst-case impact.
"""
import time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import db, init_db, send, claim, done, log_event, dex_tokens, best_pair, get_logger, run_forever, CFG

lg = get_logger("risk")
_last_monitor = [0.0]

def handle_msgs(c):
    for m in claim(c, "risk", 20):
        mint, p = m["mint"], m["payload"]
        try:
            if m["kind"] == "order_intent":
                n_open = c.execute("SELECT COUNT(*) n FROM positions WHERE status IN ('open','exiting')").fetchone()["n"]
                dup = c.execute("SELECT 1 FROM positions WHERE mint=? AND status IN ('open','exiting')", (mint,)).fetchone()
                if n_open >= CFG["MAX_OPEN_POSITIONS"] or dup:
                    send(c, "risk", "executor", "order_rejected", mint,
                         dict(position_id=p["position_id"], reason="max_open_positions" if not dup else "duplicate"))
                    continue
                c.execute("UPDATE positions SET stop_pct=?, trail_trigger_pct=?, trail_dist_pct=? WHERE id=?",
                          (CFG["STOP_PCT"], CFG["TRAIL_TRIGGER_PCT"], CFG["TRAIL_DIST_PCT"], p["position_id"]))
                log_event(c, "risk", "risk_plan", mint, position_id=p["position_id"], stop_pct=-CFG["STOP_PCT"] * 100,
                          trail_trigger_pct=CFG["TRAIL_TRIGGER_PCT"] * 100, trail_dist_pct=CFG["TRAIL_DIST_PCT"] * 100,
                          ref_price=p.get("ref_price"), planned_stop=(p.get("ref_price") or 0) * (1 - CFG["STOP_PCT"]))
                send(c, "risk", "executor", "order_approved", mint, dict(position_id=p["position_id"]))
            elif m["kind"] == "position_opened":
                pass  # position is picked up by the monitor loop from the positions table
        finally:
            done(c, m["id"])

def monitor(c):
    rows = c.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    if not rows: return
    data = dex_tokens(list({r["mint"] for r in rows}))
    now = time.time()
    for r in rows:
        pairs = data.get(r["mint"]) or []
        exact = [p for p in pairs if p.get("pairAddress") == r["pair"]]
        bp = best_pair(exact or pairs)
        if not bp or bp["_price"] <= 0:
            if now - (r["last_mark_ts"] or now) > 900:
                c.execute("UPDATE positions SET status='exiting' WHERE id=? AND status='open'", (r["id"],))
                send(c, "risk", "executor", "exit_order", r["mint"], dict(position_id=r["id"], reason="stale_price", mark=r["last_mark"]))
            continue
        mark = bp["_price"]
        peak = max(r["peak"] or mark, mark)
        stop = r["stop_price"]; trail = r["trail_active"]
        if not trail and peak >= r["entry_mark"] * (1 + r["trail_trigger_pct"]):
            trail = 1
            log_event(c, "risk", "trail_armed", r["mint"], symbol=r["symbol"], peak=peak, entry=r["entry_mark"])
        if trail:
            stop = max(stop, peak * (1 - r["trail_dist_pct"]))
        c.execute("UPDATE positions SET last_mark=?, last_mark_ts=?, peak=?, stop_price=?, trail_active=? WHERE id=?",
                  (mark, now, peak, stop, trail, r["id"]))
        if mark <= stop:
            reason = "trailing_stop" if trail else "stop_loss_-8pct"
            c.execute("UPDATE positions SET status='exiting' WHERE id=? AND status='open'", (r["id"],))
            send(c, "risk", "executor", "exit_order", r["mint"], dict(position_id=r["id"], reason=reason, mark=mark))

def step(c):
    handle_msgs(c)
    if time.time() - _last_monitor[0] >= 10:
        _last_monitor[0] = time.time(); monitor(c)

if __name__ == "__main__":
    init_db(); run_forever("risk", step, 1, lg)

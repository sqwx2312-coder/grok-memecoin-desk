"""Stage 6 — LOGGER. Sole writer of the audit tables kills / scores / trade_log and the human-readable logs:
logs/kills.log, logs/scores.log, logs/trades.log, logs/events.jsonl."""
import json, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import db, init_db, claim, done, get_logger, run_forever, LOG_DIR

lg = get_logger("logger")
def _w(name, line):
    with open(os.path.join(LOG_DIR, name), "a") as f: f.write(line + "\n")
def ts(t): return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))

def step(c):
    msgs = claim(c, "logger", 500)
    for m in msgs:
        p, mint, k, t = m["payload"], m["mint"], m["kind"], m["ts"]
        _w("events.jsonl", json.dumps({"ts": ts(t), "src": m["src"], "kind": k, "mint": mint, **p}, default=str))
        if k == "kill":
            c.execute("INSERT INTO kills(ts,mint,symbol,stage,reasons,detail) VALUES(?,?,?,?,?,?)",
                      (t, mint, p.get("symbol"), p.get("stage"), json.dumps(p.get("reasons")), json.dumps(p.get("detail"), default=str)))
            _w("kills.log", f"{ts(t)} KILL [{p.get('stage')}] {mint} {p.get('symbol') or ''} reasons={','.join(p.get('reasons') or [])} {json.dumps(p.get('detail'), default=str)[:400]}")
        elif k == "score":
            c.execute("INSERT INTO scores(ts,mint,symbol,score,passed,components) VALUES(?,?,?,?,?,?)",
                      (t, mint, p.get("symbol"), p.get("score"), int(bool(p.get("passed"))),
                       json.dumps({"components": p.get("components"), "raw": p.get("raw")}, default=str)))
            _w("scores.log", f"{ts(t)} SCORE {mint} {p.get('symbol')} {p.get('score'):.3f} {'PASS' if p.get('passed') else 'FAIL'} {json.dumps(p.get('components'))}")
        elif k == "trade":
            c.execute("INSERT INTO trade_log(ts,mint,symbol,event,price,size_usd,pnl_usd,pnl_pct,detail) VALUES(?,?,?,?,?,?,?,?,?)",
                      (t, mint, p.get("symbol"), p.get("event"), p.get("price"), p.get("size_usd"), p.get("pnl_usd"),
                       p.get("pnl_pct"), json.dumps(p.get("detail"), default=str)))
            _w("trades.log", f"{ts(t)} {p.get('event','').upper()} {mint} {p.get('symbol')} px={p.get('price')} usd={p.get('size_usd'):.2f} pnl={p.get('pnl_usd')} pnl%={p.get('pnl_pct')} {json.dumps(p.get('detail'), default=str)}")
        done(c, m["id"])

if __name__ == "__main__":
    init_db(); run_forever("logger", step, 1, lg)

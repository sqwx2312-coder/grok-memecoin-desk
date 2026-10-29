#!/usr/bin/env python3
"""Dump every number the 'Memecoin Desk (Paper)' dashboard Page needs, as JSON.
Usage: .venv/bin/python dashboard_data.py [> dashboard.json]   (stdlib only; read-only on the DB)
Times are Europe/Belgrade (CET/CEST)."""
import json, os, sqlite3, subprocess, time, collections
os.environ["TZ"] = "Europe/Belgrade"; time.tzset()
HERE = os.path.dirname(os.path.abspath(__file__))
db = sqlite3.connect(f"file:{os.path.join(HERE, 'memedesk.db')}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
q = lambda s, *a: [dict(r) for r in db.execute(s, a).fetchall()]
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t)) if t else None
hm = lambda t: time.strftime("%H:%M", time.localtime(t)) if t else None
now = time.time()
acct = {r["k"]: json.loads(r["v"]) for r in q("SELECT * FROM account")}
start_usd = float(acct.get("start_usd", 0)); cash = float(acct.get("cash_usd", 0))

# --- process state
def alive(pid):
    try: os.kill(int(pid), 0); return True
    except Exception: return False
pids = json.load(open(os.path.join(HERE, "pids.json")))
tmux = subprocess.run(["tmux", "has-session", "-t", "memedesk"], capture_output=True).returncode == 0
hb = {r["stage"]: {"age_s": round(now - r["ts"]), "info": json.loads(r["info"] or "{}")} for r in q("SELECT * FROM heartbeats")}
procs = {k: {"pid": v, "alive": alive(v), "heartbeat_age_s": hb.get(k, {}).get("age_s"),
             "heartbeat_info": hb.get(k, {}).get("info")} for k, v in pids.items()}

# --- counts
tokens_by_source = {r["source"]: r["n"] for r in q("SELECT source, COUNT(*) n FROM tokens GROUP BY source")}
watch = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM watchlist GROUP BY status")}
reasons = collections.Counter()
for r in q("SELECT reasons FROM kills"): reasons.update(json.loads(r["reasons"] or "[]"))
kills_total = q("SELECT COUNT(DISTINCT mint) n FROM kills")[0]["n"]
kills_by_stage = {r["stage"]: r["n"] for r in q("SELECT stage, COUNT(DISTINCT mint) n FROM kills GROUP BY stage")}
scores = q("SELECT ts, mint, symbol, score, passed FROM scores ORDER BY ts")
msgs = q("SELECT src, dst, kind, COUNT(*) n, SUM(done_ts IS NULL) pending FROM messages GROUP BY 1,2,3")
logs = {}
for fn in ("kills.log", "scores.log", "trades.log", "events.jsonl"):
    p = os.path.join(HERE, "logs", fn)
    logs[fn] = sum(1 for _ in open(p)) if os.path.exists(p) else 0

# --- positions
pos = []
for r in q("SELECT * FROM positions ORDER BY id"):
    d = {k: r[k] for k in ("id", "mint", "symbol", "status", "entry_mark", "entry_fill", "qty", "cost_usd",
                           "stop_price", "peak", "trail_active", "last_mark", "exit_mark", "exit_fill",
                           "proceeds_usd", "pnl_usd", "pnl_pct", "exit_reason", "stop_pct",
                           "trail_trigger_pct", "trail_dist_pct")}
    d.update(entry_time=fmt(r["entry_ts"]), exit_time=fmt(r["exit_ts"]), last_mark_time=fmt(r["last_mark_ts"]))
    if r["status"] in ("open", "exiting") and r["qty"]:
        val = r["qty"] * (r["last_mark"] or 0)
        d["value_usd"] = val; d["upnl_usd"] = val - r["cost_usd"]; d["upnl_pct"] = (val / r["cost_usd"] - 1) * 100
        d["mark_vs_entry_mark_pct"] = (r["last_mark"] / r["entry_mark"] - 1) * 100 if r["entry_mark"] else None
    pos.append(d)
opn = [p for p in pos if p["status"] in ("open", "exiting")]
closed = [p for p in pos if p["status"] == "closed"]
equity = cash + sum(p.get("value_usd", 0) for p in opn)
realized = sum(p["pnl_usd"] or 0 for p in closed)
wins = sum(1 for p in closed if (p["pnl_usd"] or 0) > 0)

# --- equity curve reconstructed from trade_log (cash + qty*last known mark)
curve = [{"t": fmt(acct["started_ts"]), "hm": hm(acct["started_ts"]), "equity": start_usd, "event": "start"}]
c, held, mark = start_usd, {}, {}
for t in q("SELECT * FROM trade_log ORDER BY ts"):
    det = json.loads(t["detail"] or "{}")
    if t["event"] == "entry":
        c -= t["size_usd"]; held[t["mint"]] = det.get("qty", 0); mark[t["mint"]] = det.get("mark", t["price"])
    elif t["event"] == "exit":
        c += t["size_usd"]; held.pop(t["mint"], None)
    curve.append({"t": fmt(t["ts"]), "hm": hm(t["ts"]), "event": f"{t['event']} {t['symbol']}",
                  "equity": c + sum(held[m] * mark[m] for m in held)})
curve.append({"t": fmt(now), "hm": hm(now), "equity": equity, "event": "now (marked)"})

# --- goal track
mult = equity / start_usd if start_usd else 0
miles = [1.25, 1.5, 2, 3, 4, 5]
nxt = next((m for m in miles if m > mult), None)

# --- recent activity (events.jsonl, excluding raw 'seen' and volume_pass noise)
ev = []
for line in open(os.path.join(HERE, "logs", "events.jsonl")):
    e = json.loads(line)
    k = e.get("kind")
    if k == "kill": what = "Killed"; det = ", ".join(e.get("reasons", []))
    elif k == "score": what = "Scored"; det = f"{e['score']:.3f} {'pass' if e.get('passed') else 'fail'}"
    elif k == "trade" and e.get("event") == "entry": what = "Entry"; det = f"${e['size_usd']:.2f} @ {e['price']:.4g}"
    elif k == "trade": what = "Exit"; det = f"{e['detail'].get('reason')} {e['pnl_usd']:+.2f} USD ({e['pnl_pct']:+.1f}%)"
    elif k == "filter_pass": what = "Passed filter"; det = "all gates passed"
    elif k == "risk_plan": what = "Risk plan"; det = f"stop {e['stop_pct']}%, trail +{e['trail_trigger_pct']}%/{e['trail_dist_pct']}%"
    elif k == "trail_armed": what = "Trail armed"; det = ", ".join(f"{a} {e[a]:.4g}" for a in ("peak", "stop", "stop_price") if isinstance(e.get(a), (int, float)))
    else: continue
    ev.append({"time": e["ts"], "stage": e["src"], "event": what, "token": e.get("symbol") or (e.get("mint") or "")[:6] + "…",
               "mint": e.get("mint"), "detail": det})

print(json.dumps({
    "snapshot": fmt(now), "started": fmt(acct.get("started_ts")), "paper_only": True,
    "running": {"tmux_session": tmux, "processes": procs},
    "account": {"start_usd": start_usd, "start_sol": acct.get("start_sol"), "sol_usd_at_start": acct.get("sol_usd_at_start"),
                "cash_usd": cash, "equity_usd": equity, "pnl_usd": equity - start_usd,
                "pnl_pct": (equity / start_usd - 1) * 100 if start_usd else 0, "realized_usd": realized,
                "unrealized_usd": sum(p.get("upnl_usd", 0) for p in opn), "open_positions": len(opn),
                "closed_trades": len(closed), "wins": wins, "win_rate_pct": wins / len(closed) * 100 if closed else None},
    "pipeline": {"tokens_seen": sum(tokens_by_source.values()), "tokens_by_source": tokens_by_source,
                 "watchlist": watch, "killed_tokens": kills_total, "kills_by_stage": kills_by_stage,
                 "scored": len(scores), "score_passed": sum(1 for s in scores if s["passed"]),
                 "orders": sum(m["n"] for m in msgs if m["kind"] == "order_intent"),
                 "fills": sum(1 for p in pos if p["entry_fill"]), "messages": msgs, "log_lines": logs},
    "kills_by_reason": dict(reasons.most_common()),
    "scores": [dict(s, time=fmt(s["ts"])) for s in scores],
    "positions": pos, "equity_curve": curve,
    "goal": {"multiple": mult, "milestones": miles, "next_milestone": nxt,
             "to_next_usd": nxt * start_usd - equity if nxt else None, "to_2x_usd": 2 * start_usd - equity},
    "recent_activity": ev[-15:][::-1],
}, indent=1, default=str))

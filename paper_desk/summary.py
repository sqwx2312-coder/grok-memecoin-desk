#!/usr/bin/env python3
"""python summary.py  — desk stats (times in Europe/Belgrade)."""
import json, sqlite3, time, os, collections
os.environ.setdefault("TZ", "Europe/Belgrade"); time.tzset()
c = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), "memedesk.db"))
c.row_factory = sqlite3.Row
q = lambda s, *a: c.execute(s, a).fetchall()
acct = {r["k"]: json.loads(r["v"]) for r in q("SELECT * FROM account")}
f = lambda t: time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(t)) if t else "-"
print("=== MEMEDESK (PAPER ONLY) ===  now", f(time.time()), " started", f(acct.get("started_ts")))
print("\n-- Stage heartbeats --")
for r in q("SELECT * FROM heartbeats ORDER BY stage"):
    print(f"  {r['stage']:9s} {int(time.time()-r['ts'])}s ago  {r['info']}")
print("\n-- Tokens seen (by source) --")
tot = 0
for r in q("SELECT source, COUNT(*) n FROM tokens GROUP BY source ORDER BY n DESC"):
    print(f"  {r['source']:20s} {r['n']}"); tot += r["n"]
print(f"  {'TOTAL':20s} {tot}")
print("\n-- Watchlist status --")
for r in q("SELECT status, COUNT(*) n FROM watchlist GROUP BY status"):
    print(f"  {r['status']:14s} {r['n']}")
print("\n-- Kills by reason (a token can fail several gates; each counted) --")
cnt, prim = collections.Counter(), collections.Counter()
for r in q("SELECT reasons FROM kills"):
    rs = json.loads(r["reasons"] or "[]")
    cnt.update(rs)
    if rs: prim[rs[0]] += 1
for k, v in cnt.most_common(): print(f"  {k:32s} {v}")
print(f"  total killed tokens: {len(q('SELECT DISTINCT mint FROM kills'))}")
surv = q("SELECT COUNT(*) n FROM watchlist WHERE status='passed'")[0]["n"]
print(f"\n-- Filter survivors: {surv} --")
print("\n-- Scores (latest 20) --")
for r in q("SELECT * FROM scores ORDER BY id DESC LIMIT 20"):
    print(f"  {f(r['ts'])}  {r['mint'][:12]}… {str(r['symbol'])[:12]:12s} {r['score']:.3f} {'PASS' if r['passed'] else 'fail'}")
allsc = [r["score"] for r in q("SELECT score FROM scores")]
if allsc: print(f"  n={len(allsc)} mean={sum(allsc)/len(allsc):.3f} passed={sum(1 for s in allsc if s>=0.6)}")
print("\n-- Trades --")
for r in q("SELECT * FROM positions WHERE status IN ('open','exiting','closed') ORDER BY id"):
    if r["status"] == "closed":
        print(f"  CLOSED {str(r['symbol'])[:10]:10s} cost=${r['cost_usd']:.2f} pnl=${r['pnl_usd']:+.2f} ({r['pnl_pct']:+.1f}%) {r['exit_reason']} {f(r['entry_ts'])}->{f(r['exit_ts'])}")
    else:
        u = r["qty"] * (r["last_mark"] or 0) - r["cost_usd"]
        print(f"  OPEN   {str(r['symbol'])[:10]:10s} cost=${r['cost_usd']:.2f} uPnL=${u:+.2f} mark={r['last_mark']} stop={r['stop_price']:.3g} trail={'on' if r['trail_active'] else 'off'}")
closed = q("SELECT pnl_usd FROM positions WHERE status='closed'")
wins = sum(1 for r in closed if r["pnl_usd"] > 0)
realized = sum(r["pnl_usd"] for r in closed)
opn = q("SELECT qty,last_mark,cost_usd FROM positions WHERE status IN ('open','exiting')")
unreal = sum(r["qty"] * (r["last_mark"] or 0) - r["cost_usd"] for r in opn)
cash = acct.get("cash_usd", 0); start = acct.get("start_usd", 0)
eq = cash + sum(r["qty"] * (r["last_mark"] or 0) for r in opn)
print(f"\n  closed={len(closed)} open={len(opn)} win_rate={(wins/len(closed)*100 if closed else 0):.1f}%")
px = acct.get("sol_usd_at_start") or 1
print(f"  bankroll start=${start:.2f} ({acct.get('start_sol')} SOL @ ${px:.2f})  cash=${cash:.2f}  equity=${eq:.2f}")
print(f"  realized PnL=${realized:+.2f}  unrealized=${unreal:+.2f}  total=${eq-start:+.2f} ({(eq-start)/start*100 if start else 0:+.2f}%)  = {(eq-start)/px:+.3f} SOL")

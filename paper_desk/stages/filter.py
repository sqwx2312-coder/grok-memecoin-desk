"""Stage 2 — FILTER. Hard kill gates. Input: scout->filter new_pair. Output: filter->analyst candidate;
every kill -> logger (kind=kill) with ALL failing reasons.

Gates (any failure kills):
  mint_authority_not_revoked  EXACT   (public RPC getMultipleAccounts jsonParsed; RugCheck re-confirms)
  freeze_authority_not_revoked EXACT  (extra gate, not in spec: frozen tokens can't be sold)
  lp_not_locked_or_burned     EXACT-ish (RugCheck markets[].lp.lpLockedPct of the main market >= 95%;
                                bonding-curve markets (pump.fun curve, Raydium LaunchLab, Meteora DBC) have
                                no withdrawable LP and are treated as locked)
  top10_holders_gt_25pct      EXACT-ish (RugCheck topHolders, excluding AMM/bonding-curve/locker/pool accounts;
                                creator wallet is counted)
  volume_lt_40k_1h            EXACT (DexScreener h1 volume summed across the token's pairs), evaluated over a
                                watch window: re-checked every 60s for up to 60 min after first sighting
  dev_wallet_rug_history      APPROXIMATE (RugCheck 'Creator history of rugged tokens' risk + local memory of
                                creators whose tokens RugCheck marked rugged)
  bundled_launch              APPROXIMATE (>=4 txs in the mint's creation slot via public RPC
                                getSignaturesForAddress paging; + RugCheck insider-flagged holders >=5% or
                                bundle/insider danger risks). PumpPortal per-token trade streams need a funded key.
  rugged                      EXACT (RugCheck rugged flag)
"""
import json, time, sys, os, threading, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import (db, init_db, send, claim, done, log_event, heartbeat, http_json, rpc, dex_tokens,
                    best_pair, get_logger, CFG)

lg = get_logger("filter")
BONDING_TYPES = ("pump_fun", "launchlab", "launch_lab", "dbc", "bonding")

def kill(c, mint, reasons, detail=None):
    c.execute("UPDATE watchlist SET status='killed', note=? WHERE mint=?", (",".join(reasons), mint))
    sym = (c.execute("SELECT symbol FROM tokens WHERE mint=?", (mint,)).fetchone() or {"symbol": None})["symbol"]
    log_event(c, "filter", "kill", mint, stage="filter", symbol=sym, reasons=reasons, detail=detail or {})

# ---------------- intake -------------------
def intake(c):
    for m in claim(c, "filter", 500):
        mint = m["mint"]
        row = c.execute("SELECT status FROM watchlist WHERE mint=?", (mint,)).fetchone()
        if row is None:
            c.execute("INSERT INTO watchlist(mint,added_ts,status,next_check_ts) VALUES(?,?, 'watch', ?)",
                      (mint, time.time(), time.time() + 30))
        done(c, m["id"])

# ---------------- gate 1: authorities via RPC (cheap, batched 100) -------------------
def auth_check(c):
    rows = c.execute("SELECT mint FROM watchlist WHERE status='watch' AND auth_checked=0 LIMIT 100").fetchall()
    if not rows: return
    mints = [r["mint"] for r in rows]
    res = rpc("getMultipleAccounts", [mints, {"encoding": "jsonParsed"}])
    if res is None: return
    for mint, acc in zip(mints, res.get("value", [])):
        try:
            info = acc["data"]["parsed"]["info"]
        except Exception:
            c.execute("UPDATE watchlist SET auth_checked=2 WHERE mint=?", (mint,)); continue  # unknown -> RugCheck
        reasons = []
        if info.get("mintAuthority"): reasons.append("mint_authority_not_revoked")
        if info.get("freezeAuthority"): reasons.append("freeze_authority_not_revoked")
        c.execute("UPDATE watchlist SET auth_checked=1 WHERE mint=?", (mint,))
        if reasons:
            kill(c, mint, reasons, {"mintAuthority": info.get("mintAuthority"), "freezeAuthority": info.get("freezeAuthority")})

# ---------------- gate 2: volume over a watch window -------------------
def volume_sweep(c):
    now = time.time()
    rows = c.execute("SELECT mint, added_ts FROM watchlist WHERE status='watch' AND next_check_ts<=? "
                     "ORDER BY next_check_ts LIMIT 900", (now,)).fetchall()
    if not rows: return
    data = dex_tokens([r["mint"] for r in rows])
    for r in rows:
        mint = r["mint"]; bp = best_pair(data.get(mint))
        vol = bp["_vol_h1"] if bp else 0.0; liq = bp["_liq"] if bp else 0.0
        c.execute("UPDATE watchlist SET checks=checks+1, last_vol_h1=?, last_liq=?, next_check_ts=? WHERE mint=?",
                  (vol, liq, now + CFG["VOL_RECHECK_SEC"], mint))
        if vol >= CFG["MIN_VOL_H1_USD"]:
            c.execute("UPDATE watchlist SET status='deep_pending' WHERE mint=? AND status='watch'", (mint,))
            log_event(c, "filter", "volume_pass", mint, vol_h1=vol, liq=liq)
        elif now - r["added_ts"] > CFG["WATCH_WINDOW_MIN"] * 60:
            kill(c, mint, ["volume_lt_40k_1h"], {"vol_h1": vol, "liq": liq, "watched_min": round((now - r["added_ts"]) / 60, 1),
                                                  "note": "never reached $40k/h within the 60-min watch window"})

# ---------------- gate 3: deep checks (RugCheck + RPC) -------------------
def creation_slot_txs(mint, max_pages=10):
    before = None; sigs = []
    for _ in range(max_pages):
        p = {"limit": 1000}
        if before: p["before"] = before
        res = rpc("getSignaturesForAddress", [mint, p])
        if res is None: return None
        sigs += res
        if len(res) < 1000:
            if not sigs: return None
            slots = collections.Counter(s["slot"] for s in sigs)
            first = min(slots)
            return {"creation_slot": first, "txs_in_creation_slot": slots[first], "total_sigs": len(sigs)}
        before = res[-1]["signature"]
    return {"unknown": True, "total_sigs": len(sigs)}  # too many txs to reach creation cheaply

def deep_check(c):
    row = c.execute("SELECT w.mint, t.creator FROM watchlist w LEFT JOIN tokens t ON t.mint=w.mint "
                    "WHERE w.status='deep_pending' ORDER BY w.added_ts LIMIT 1").fetchone()
    if not row: return False
    mint = row["mint"]
    c.execute("UPDATE watchlist SET status='deep_running' WHERE mint=?", (mint,))
    rep = http_json("rugcheck", "GET", f"https://api.rugcheck.xyz/v1/tokens/{mint}/report")
    if not rep or not isinstance(rep, dict) or "mint" not in rep:
        c.execute("UPDATE watchlist SET status='deep_pending', note='rugcheck_unavailable' WHERE mint=?", (mint,))
        time.sleep(5); return True
    reasons, detail = [], {}
    risks = [(r.get("name", ""), r.get("level", "")) for r in rep.get("risks") or []]
    detail["rugcheck_risks"] = risks
    # authorities (re-confirm)
    if rep.get("mintAuthority"): reasons.append("mint_authority_not_revoked")
    if rep.get("freezeAuthority"): reasons.append("freeze_authority_not_revoked")
    if rep.get("rugged"): reasons.append("rugged")
    # LP
    markets = rep.get("markets") or []
    excluded = set()
    for k, v in (rep.get("knownAccounts") or {}).items():
        if (v or {}).get("type") in ("AMM", "LOCKER"): excluded.add(k)
    for m in markets:
        for k in ("pubkey", "liquidityA", "liquidityB"):
            if m.get(k): excluded.add(m[k])
        for k in ("liquidityAAccount", "liquidityBAccount"):
            if (m.get(k) or {}).get("owner"): excluded.add(m[k]["owner"])
    if markets:
        main = max(markets, key=lambda m: ((m.get("lp") or {}).get("quoteUSD") or 0) + ((m.get("lp") or {}).get("baseUSD") or 0))
        mtype = (main.get("marketType") or "").lower()
        lpl = (main.get("lp") or {}).get("lpLockedPct") or 0
        bonding = any(b in mtype for b in BONDING_TYPES) and "amm" not in mtype
        detail["lp"] = {"market": mtype, "lpLockedPct": lpl, "bonding_curve": bonding}
        if not bonding and lpl < CFG["MIN_LP_LOCKED_PCT"]:
            reasons.append("lp_not_locked_or_burned")
    else:
        detail["lp"] = "no markets in RugCheck report (unverifiable)"
        reasons.append("lp_not_locked_or_burned")
    # top 10 holders
    holders = [h for h in (rep.get("topHolders") or [])
               if h.get("owner") not in excluded and h.get("address") not in excluded]
    top10 = sum(float(h.get("pct") or 0) for h in holders[:10])
    detail["top10_pct_ex_pools"] = round(top10, 2)
    if top10 > CFG["MAX_TOP10_PCT"]: reasons.append("top10_holders_gt_25pct")
    # dev rug history (approximate)
    creator = rep.get("creator") or row["creator"]
    c.execute("CREATE TABLE IF NOT EXISTS rugged_creators(creator TEXT PRIMARY KEY, ts REAL, mint TEXT)")
    dev_flag = any(("rug" in n.lower() and "creator" in n.lower()) for n, _ in risks)
    if creator and c.execute("SELECT 1 FROM rugged_creators WHERE creator=?", (creator,)).fetchone():
        dev_flag = True; detail["dev_local_memory"] = True
    if dev_flag: reasons.append("dev_wallet_rug_history")
    if rep.get("rugged") and creator:
        c.execute("INSERT OR IGNORE INTO rugged_creators VALUES(?,?,?)", (creator, time.time(), mint))
    detail["creator"] = creator
    # bundled launch (approximate)
    insider_pct = sum(float(h.get("pct") or 0) for h in (rep.get("topHolders") or []) if h.get("insider"))
    bund_risk = any(("bundle" in n.lower() or "insider" in n.lower()) and lvl == "danger" for n, lvl in risks)
    slot_info = creation_slot_txs(mint)
    detail["bundle"] = {"insider_holders_pct": round(insider_pct, 2), "rugcheck_bundle_risk": bund_risk,
                        "graphInsidersDetected": rep.get("graphInsidersDetected"), "slot": slot_info}
    same_slot = (slot_info or {}).get("txs_in_creation_slot") or 0
    if bund_risk or insider_pct >= CFG["INSIDER_HOLD_PCT"] or same_slot >= CFG["BUNDLE_SAME_SLOT_TXS"]:
        reasons.append("bundled_launch")
    if reasons:
        kill(c, mint, reasons, detail)
    else:
        c.execute("UPDATE watchlist SET status='passed' WHERE mint=?", (mint,))
        detail["rugcheck_score_normalised"] = rep.get("score_normalised")
        detail["total_holders"] = rep.get("totalHolders")
        detail["risks_warn"] = sum(1 for _, l in risks if l == "warn")
        detail["risks_danger"] = sum(1 for _, l in risks if l == "danger")
        send(c, "filter", "analyst", "candidate", mint, detail)
        log_event(c, "filter", "filter_pass", mint, **detail)
    return True

def loop(name, fn, interval):
    c = db()
    while True:
        try:
            r = fn(c)
            if fn is deep_check and r: continue
        except Exception as e:
            lg.exception("%s error: %s", name, e)
            time.sleep(3)
            try: c.close()
            except Exception: pass
            c = db()
        time.sleep(interval)

def main():
    init_db()
    c = db()
    # recover from crash mid-deep-check
    c.execute("UPDATE watchlist SET status='deep_pending' WHERE status='deep_running'")
    for name, fn, iv in (("auth", auth_check, 3), ("volume", volume_sweep, 5), ("deep", deep_check, 2)):
        threading.Thread(target=loop, args=(name, fn, iv), daemon=True).start()
    while True:
        try:
            intake(c)
            st = dict(c.execute("SELECT status, COUNT(*) FROM watchlist GROUP BY status").fetchall())
            heartbeat(c, "filter", st)
        except Exception as e:
            lg.exception("intake: %s", e); time.sleep(3)
            try: c.close()
            except Exception: pass
            c = db()
        time.sleep(1)

if __name__ == "__main__":
    main()

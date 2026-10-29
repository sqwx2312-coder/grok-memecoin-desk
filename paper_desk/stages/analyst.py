"""Stage 3 — ANALYST. Input filter->analyst candidate. Scores 0..1 with a transparent weighted formula,
every score -> logger. score < 0.60 -> kill (logged). score >= 0.60 -> analyst->executor signal.

score = 0.20*liquidity + 0.20*vol_momentum + 0.15*buy_pressure + 0.15*holder_dist
      + 0.10*holder_count + 0.10*price_trend + 0.05*age + 0.05*residual_risk
  liquidity     = clamp(log10(liq_usd/5k)/log10(20))         $5k->0, $100k->1
  vol_momentum  = clamp((vol_m5*12/vol_h1)/1.5)               last-5m run-rate vs last hour; >=1.5x -> 1
  buy_pressure  = clamp((buys/(buys+sells) - 0.45)/0.25)      h1 txn counts; 45%->0, 70%->1
  holder_dist   = clamp((25 - top10_pct)/20)                   top10 (ex pools) 25%->0, 5%->1
  holder_count  = clamp(log10(holders/100)/log10(20))          100->0, 2000->1
  price_trend   = 0.5*clamp(chg_h1/100) + 0.5*clamp((chg_m5+15)/30)
  age           = pair age: <2m 0, ramps to 1 at 10m, 1 until 6h, then 0.3
  residual_risk = clamp(1 - 0.2*rugcheck_warns - 0.5*rugcheck_dangers)
"""
import math, time, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import db, init_db, send, claim, done, log_event, dex_tokens, best_pair, get_logger, run_forever, CFG

lg = get_logger("analyst")
W = dict(liquidity=0.20, vol_momentum=0.20, buy_pressure=0.15, holder_dist=0.15,
         holder_count=0.10, price_trend=0.10, age=0.05, residual_risk=0.05)
clamp = lambda x: max(0.0, min(1.0, x))

def score(bp, cand):
    liq = bp["_liq"]; vh1 = bp["_vol_h1"] or 1e-9; vm5 = bp["_vol_m5"]
    b, s = bp["_buys_h1"], bp["_sells_h1"]
    pc = bp.get("priceChange") or {}
    top10 = cand.get("top10_pct_ex_pools", 25) or 0
    holders = cand.get("total_holders") or 0
    age_min = (time.time() * 1000 - (bp.get("pairCreatedAt") or time.time() * 1000)) / 60000
    if age_min < 2: age = 0.0
    elif age_min < 10: age = (age_min - 2) / 8
    elif age_min <= 360: age = 1.0
    else: age = 0.3
    comp = dict(
        liquidity=clamp(math.log10(max(liq, 1) / 5000) / math.log10(20)),
        vol_momentum=clamp((vm5 * 12 / vh1) / 1.5),
        buy_pressure=clamp(((b / (b + s)) - 0.45) / 0.25) if b + s else 0.2,
        holder_dist=clamp((25 - top10) / 20),
        holder_count=clamp(math.log10(max(holders, 1) / 100) / math.log10(20)),
        price_trend=0.5 * clamp((pc.get("h1") or 0) / 100) + 0.5 * clamp(((pc.get("m5") or 0) + 15) / 30),
        age=age,
        residual_risk=clamp(1 - 0.2 * (cand.get("risks_warn") or 0) - 0.5 * (cand.get("risks_danger") or 0)),
    )
    total = sum(W[k] * comp[k] for k in W)
    raw = dict(liq=liq, vol_h1=vh1, vol_m5=vm5, buys_h1=b, sells_h1=s, chg_m5=pc.get("m5"), chg_h1=pc.get("h1"),
               top10=top10, holders=holders, age_min=round(age_min, 1), price=bp["_price"], dex=bp.get("dexId"),
               pair=bp.get("pairAddress"), symbol=(bp.get("baseToken") or {}).get("symbol"))
    return round(total, 4), {k: round(v, 3) for k, v in comp.items()}, raw

def step(c):
    msgs = claim(c, "analyst", 20)
    if not msgs: return
    data = dex_tokens([m["mint"] for m in msgs])
    for m in msgs:
        mint, cand = m["mint"], m["payload"]
        bp = best_pair(data.get(mint))
        if not bp or bp["_price"] <= 0:
            log_event(c, "analyst", "kill", mint, stage="analyst", symbol=None, reasons=["no_price_data"], detail={})
            done(c, m["id"]); continue
        sc, comp, raw = score(bp, cand)
        passed = sc >= CFG["SCORE_THRESHOLD"]
        log_event(c, "analyst", "score", mint, symbol=raw["symbol"], score=sc, passed=passed, components=comp, raw=raw)
        if passed:
            send(c, "analyst", "executor", "signal", mint, dict(score=sc, symbol=raw["symbol"], pair=raw["pair"],
                                                                price=raw["price"], liq=raw["liq"]))
        else:
            log_event(c, "analyst", "kill", mint, stage="analyst", symbol=raw["symbol"],
                      reasons=["score_lt_0.6"], detail={"score": sc, "components": comp})
        done(c, m["id"])

if __name__ == "__main__":
    init_db(); run_forever("analyst", step, 2, lg)

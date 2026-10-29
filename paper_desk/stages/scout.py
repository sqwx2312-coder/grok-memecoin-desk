"""Stage 1 — SCOUT. Watches every new pair and hands each to FILTER.
Sources:
  * PumpPortal websocket (free, no key): subscribeNewToken -> pool 'pump' (PumpFun) and 'bonk' (BonkFun/letsbonk);
    subscribeMigration -> PumpFun graduations to PumpSwap.
  * GeckoTerminal /networks/solana/new_pools (free, no key): Raydium (AMM/CPMM/CLMM/LaunchLab) and
    Meteora (DLMM/DAMM/DBC) new pools.
Output: messages src=scout dst=filter kind=new_pair ; and a log copy to logger.
"""
import asyncio, json, time, sys, os, calendar
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import websockets
from common import db, init_db, send, log_event, heartbeat, http_json, get_logger, QUOTE_MINTS

lg = get_logger("scout")
STATS = {"pump": 0, "bonk": 0, "migration": 0, "gecko": 0}
SEEN_GECKO = set()

def emit(c, mint, source, dex, pair, name, symbol, creator, created_ts, extra):
    new = c.execute("SELECT 1 FROM tokens WHERE mint=?", (mint,)).fetchone() is None
    if new:
        c.execute("INSERT INTO tokens(mint,source,dex,pair,name,symbol,creator,created_ts,first_seen_ts,extra) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (mint, source, dex, pair, name, symbol, creator, created_ts, time.time(), json.dumps(extra)))
    payload = dict(source=source, dex=dex, pair=pair, name=name, symbol=symbol, creator=creator,
                   created_ts=created_ts, is_new=new)
    send(c, "scout", "filter", "new_pair", mint, payload)
    log_event(c, "scout", "seen", mint, **payload)

async def pumpportal(c):
    url = "wss://pumpportal.fun/api/data"
    backoff = 2
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                await ws.send(json.dumps({"method": "subscribeMigration"}))
                lg.info("pumpportal connected")
                backoff = 2
                async for raw in ws:
                    try:
                        d = json.loads(raw)
                        tx = d.get("txType")
                        if tx == "create":
                            pool = d.get("pool", "pump")
                            src = {"pump": "pumpfun", "bonk": "bonkfun"}.get(pool, pool)
                            STATS["bonk" if pool == "bonk" else "pump"] += 1
                            emit(c, d["mint"], src, src, d.get("bondingCurveKey") or d.get("mint"),
                                 d.get("name"), d.get("symbol"), d.get("traderPublicKey"), time.time(),
                                 {"initialBuy": d.get("initialBuy"), "solAmount": d.get("solAmount"),
                                  "marketCapSol": d.get("marketCapSol"), "sig": d.get("signature")})
                        elif tx == "migrate" or (d.get("mint") and "pool" in d and tx is None and "signature" in d):
                            STATS["migration"] += 1
                            emit(c, d["mint"], "pumpfun_migration", d.get("pool", "pumpswap"), None,
                                 None, None, None, time.time(), {"sig": d.get("signature")})
                        elif "message" in d:
                            lg.info("pumpportal: %s", d["message"])
                    except Exception as e:
                        lg.warning("bad ws msg: %s", e)
        except Exception as e:
            lg.warning("pumpportal disconnected: %s; reconnect in %ss", e, backoff)
        await asyncio.sleep(backoff); backoff = min(backoff * 2, 60)

def gecko_poll_once(c):
    for page in (1, 2):
        j = http_json("gecko", "GET",
                      f"https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page={page}&include=dex,base_token,quote_token")
        if not j: continue
        for p in j.get("data", []):
            try:
                rel = p["relationships"]; dex = rel["dex"]["data"]["id"]
                if not ("raydium" in dex or "meteora" in dex):
                    continue  # pump-fun / pumpswap come from PumpPortal
                pool = p["attributes"]["address"]
                if pool in SEEN_GECKO: continue
                SEEN_GECKO.add(pool)
                base = rel["base_token"]["data"]["id"].split("_", 1)[1]
                quote = rel["quote_token"]["data"]["id"].split("_", 1)[1]
                mint = quote if base in QUOTE_MINTS and quote not in QUOTE_MINTS else base
                if mint in QUOTE_MINTS: continue
                created = p["attributes"].get("pool_created_at")
                cts = calendar.timegm(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")) if created else time.time()
                STATS["gecko"] += 1
                emit(c, mint, "gecko", dex, pool, p["attributes"].get("name"), None, None, cts,
                     {"reserve_usd": p["attributes"].get("reserve_in_usd")})
            except Exception as e:
                lg.warning("gecko parse: %s", e)

async def gecko_loop(c):
    while True:
        try:
            await asyncio.to_thread(gecko_poll_once, c)
        except Exception as e:
            lg.warning("gecko loop: %s", e)
        await asyncio.sleep(30)

async def hb(c):
    while True:
        try:
            heartbeat(c, "scout", STATS); lg.info("stats %s", STATS)
        except Exception as e:
            lg.warning("hb: %s", e)
        await asyncio.sleep(60)

async def main():
    init_db()
    c = db()
    c2 = db()  # separate connection for the gecko thread
    await asyncio.gather(pumpportal(c), gecko_loop(c2), hb(c))

if __name__ == "__main__":
    asyncio.run(main())

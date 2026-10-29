"""Shared plumbing for the PAPER-TRADING-ONLY memecoin desk.

NO private keys, NO wallets, NO transaction signing/sending anywhere in this codebase.
Every stage is its own OS process; stages talk ONLY via the SQLite `messages` table
(hard, auditable handoffs: src -> dst, kind, payload, claimed/done timestamps).
"""
import json, os, sqlite3, time, logging, threading, random
from logging.handlers import RotatingFileHandler
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "memedesk.db")
LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# ----------------------------- CONFIG ---------------------------------------
CFG = dict(
    PAPER_ONLY=True,
    START_BANKROLL_SOL=10.0,
    MAX_TRADE_FRAC=0.10,          # Executor: max 10% of paper equity per trade
    MAX_OPEN_POSITIONS=10,
    STOP_PCT=0.08,                # Risk: hard stop -8% from entry, set BEFORE entry
    TRAIL_TRIGGER_PCT=0.15,       # Risk: trailing activates once peak >= +15%
    TRAIL_DIST_PCT=0.08,          # Risk: trail 8% below peak once active
    FEE_PCT=0.01,                 # per side: DEX/LP fee + priority/Jito tip allowance
    BASE_SLIP_PCT=0.005,          # per side base slippage
    # Filter
    MIN_VOL_H1_USD=40_000,
    WATCH_WINDOW_MIN=60,          # keep a new pair on the watchlist up to 60 min
    VOL_RECHECK_SEC=60,
    MAX_TOP10_PCT=25.0,
    MIN_LP_LOCKED_PCT=95.0,
    BUNDLE_SAME_SLOT_TXS=4,       # >=4 txs in the creation slot (create + >=3 buys) => bundled
    INSIDER_HOLD_PCT=5.0,         # RugCheck insider-flagged holders >= 5% => bundled/insider
    # Analyst
    SCORE_THRESHOLD=0.60,
)

SOL_MINT = "So11111111111111111111111111111111111111112"
QUOTE_MINTS = {SOL_MINT, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
               "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}
RPC_URL = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")

# ----------------------------- LOGGING --------------------------------------
def get_logger(name):
    lg = logging.getLogger(name)
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(os.path.join(LOG_DIR, f"{name}.log"), maxBytes=20_000_000, backupCount=3)
    fh.setFormatter(fmt); lg.addHandler(fh)
    sh = logging.StreamHandler(); sh.setFormatter(fmt); lg.addHandler(sh)
    return lg

# ----------------------------- DB -------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, src TEXT, dst TEXT, kind TEXT,
  mint TEXT, payload TEXT, claimed_ts REAL, done_ts REAL);
CREATE INDEX IF NOT EXISTS ix_msg_dst ON messages(dst, claimed_ts);
CREATE TABLE IF NOT EXISTS tokens(
  mint TEXT PRIMARY KEY, source TEXT, dex TEXT, pair TEXT, name TEXT, symbol TEXT,
  creator TEXT, created_ts REAL, first_seen_ts REAL, extra TEXT);
CREATE TABLE IF NOT EXISTS watchlist(
  mint TEXT PRIMARY KEY, added_ts REAL, status TEXT, auth_checked INTEGER DEFAULT 0,
  next_check_ts REAL, checks INTEGER DEFAULT 0, last_vol_h1 REAL, last_liq REAL, note TEXT);
CREATE INDEX IF NOT EXISTS ix_wl_status ON watchlist(status);
CREATE TABLE IF NOT EXISTS kills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mint TEXT, symbol TEXT, stage TEXT,
  reasons TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS scores(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mint TEXT, symbol TEXT, score REAL,
  passed INTEGER, components TEXT);
CREATE TABLE IF NOT EXISTS trade_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, mint TEXT, symbol TEXT, event TEXT,
  price REAL, size_usd REAL, pnl_usd REAL, pnl_pct REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, mint TEXT, symbol TEXT, pair TEXT,
  status TEXT, intent_ts REAL, entry_ts REAL, entry_mark REAL, entry_fill REAL, qty REAL,
  cost_usd REAL, stop_pct REAL, trail_trigger_pct REAL, trail_dist_pct REAL,
  stop_price REAL, peak REAL, trail_active INTEGER DEFAULT 0, last_mark REAL, last_mark_ts REAL,
  exit_ts REAL, exit_mark REAL, exit_fill REAL, proceeds_usd REAL, pnl_usd REAL, pnl_pct REAL,
  exit_reason TEXT);
CREATE TABLE IF NOT EXISTS account(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS heartbeats(stage TEXT PRIMARY KEY, ts REAL, info TEXT);
"""

def db():
    c = sqlite3.connect(DB_PATH, timeout=60, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("PRAGMA synchronous=NORMAL")
    return c

def init_db():
    c = db(); c.executescript(SCHEMA); c.close()

def send(c, src, dst, kind, mint=None, payload=None):
    """Hard handoff: append a message addressed to exactly one downstream stage."""
    c.execute("INSERT INTO messages(ts,src,dst,kind,mint,payload) VALUES(?,?,?,?,?,?)",
              (time.time(), src, dst, kind, mint, json.dumps(payload or {}, default=str)))

def claim(c, dst, limit=50):
    """Claim unclaimed messages for this stage (single consumer per stage)."""
    c.execute("BEGIN IMMEDIATE")
    try:
        rows = c.execute("SELECT * FROM messages WHERE dst=? AND claimed_ts IS NULL ORDER BY id LIMIT ?",
                         (dst, limit)).fetchall()
        if rows:
            now = time.time()
            c.executemany("UPDATE messages SET claimed_ts=? WHERE id=?", [(now, r["id"]) for r in rows])
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return [dict(r, payload=json.loads(r["payload"] or "{}")) for r in rows]

def done(c, msg_id):
    c.execute("UPDATE messages SET done_ts=? WHERE id=?", (time.time(), msg_id))

def heartbeat(c, stage, info=None):
    c.execute("INSERT OR REPLACE INTO heartbeats(stage,ts,info) VALUES(?,?,?)",
              (stage, time.time(), json.dumps(info or {})))

def log_event(c, src, kind, mint=None, **payload):
    """Everything loggable is handed to the Logger stage."""
    send(c, src, "logger", kind, mint, payload)

def acct_get(c, k, default=None):
    r = c.execute("SELECT v FROM account WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default

def acct_set(c, k, v):
    c.execute("INSERT OR REPLACE INTO account(k,v) VALUES(?,?)", (k, json.dumps(v)))

# ----------------------------- HTTP w/ rate limiting -------------------------
class RateLimiter:
    def __init__(self, per_sec):
        self.min_gap = 1.0 / per_sec; self.last = 0.0; self.lock = threading.Lock()
    def wait(self):
        with self.lock:
            gap = self.last + self.min_gap - time.time()
            if gap > 0: time.sleep(gap)
            self.last = time.time()

_session = requests.Session()
_session.headers["User-Agent"] = "memedesk-paper/1.0"
LIMITS = {
    "dexscreener": RateLimiter(2.0),   # DexScreener: 300 req/min documented; 2/s per process
    "gecko": RateLimiter(0.4),         # GeckoTerminal free: ~30/min
    "rugcheck": RateLimiter(0.5),      # RugCheck public: be gentle
    "rpc": RateLimiter(0.4),           # public mainnet RPC is aggressively limited
}

def http_json(kind, method, url, retries=3, **kw):
    for attempt in range(retries):
        LIMITS[kind].wait()
        try:
            r = _session.request(method, url, timeout=25, **kw)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1) + random.random()); continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1)); continue
            if r.status_code != 200:
                return None
            j = r.json()
            if kind == "rpc" and isinstance(j, dict) and "error" in j:
                if j["error"].get("code") == 429:
                    time.sleep(3 * (attempt + 1)); continue
                return None
            return j
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None

def rpc(method, params):
    j = http_json("rpc", "POST", RPC_URL, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    return None if j is None else j.get("result")

def dex_tokens(mints):
    """DexScreener batch lookup (<=30 mints). Returns {mint: [pairs]}"""
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        j = http_json("dexscreener", "GET", "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(chunk))
        if not isinstance(j, list): continue
        for p in j:
            m = (p.get("baseToken") or {}).get("address")
            if m: out.setdefault(m, []).append(p)
    return out

def best_pair(pairs):
    """Most-liquid pair = price source; volume/txns summed across all pairs of the token."""
    if not pairs: return None
    bp = max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0, (p.get("volume") or {}).get("h1") or 0))
    agg = dict(bp)
    agg["_vol_h1"] = sum(((p.get("volume") or {}).get("h1") or 0) for p in pairs)
    agg["_vol_m5"] = sum(((p.get("volume") or {}).get("m5") or 0) for p in pairs)
    agg["_buys_h1"] = sum((((p.get("txns") or {}).get("h1") or {}).get("buys") or 0) for p in pairs)
    agg["_sells_h1"] = sum((((p.get("txns") or {}).get("h1") or {}).get("sells") or 0) for p in pairs)
    agg["_liq"] = (bp.get("liquidity") or {}).get("usd") or 0
    agg["_liq_est"] = False
    if not agg["_liq"] and (bp.get("marketCap") or bp.get("fdv")):
        # DexScreener reports no liquidity for bonding-curve pairs (pump.fun/LaunchLab/DBC).
        # Estimate ~25% of market cap (matches RugCheck totalMarketLiquidity on sampled pump curves).
        agg["_liq"] = 0.25 * float(bp.get("marketCap") or bp.get("fdv")); agg["_liq_est"] = True
    agg["_price"] = float(bp.get("priceUsd") or 0)
    return agg

def sol_usd():
    pairs = dex_tokens([SOL_MINT]).get(SOL_MINT, [])
    pairs = [p for p in pairs if (p.get("quoteToken") or {}).get("symbol") in ("USDC", "USDT")]
    bp = best_pair(pairs)
    return bp["_price"] if bp else None

def run_forever(name, step, interval, lg):
    """Exceptions never kill the stage; they are logged and the loop continues."""
    c = db()
    while True:
        t0 = time.time()
        try:
            step(c)
            heartbeat(c, name)
        except Exception as e:
            lg.exception("step error: %s", e)
            try: c.close()
            except Exception: pass
            time.sleep(3); c = db()
        time.sleep(max(0.2, interval - (time.time() - t0)))

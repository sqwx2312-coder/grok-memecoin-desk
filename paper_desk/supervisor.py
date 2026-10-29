"""Watchdog: runs the 6 stage processes, restarts any that exit/crash, and restarts a stage whose
heartbeat is stale for >5 min (hung). PAPER ONLY."""
import subprocess, sys, time, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import init_db, db, get_logger, LOG_DIR, BASE

STAGES = ["scout", "filter", "analyst", "executor", "risk", "logger"]
lg = get_logger("supervisor")
_venv_py = os.path.join(BASE, ".venv", "bin", "python")
# Prefer this folder's venv (same choice as run.sh). Fall back to the interpreter
# that launched the supervisor so a system python3 still starts the stages.
PY = _venv_py if os.path.isfile(_venv_py) and os.access(_venv_py, os.X_OK) else sys.executable

def start(name):
    out = open(os.path.join(LOG_DIR, f"{name}.stdout"), "a")
    p = subprocess.Popen([PY, os.path.join(BASE, "stages", f"{name}.py")], stdout=out, stderr=subprocess.STDOUT, cwd=BASE)
    lg.info("started %s pid=%s", name, p.pid); return p

def main():
    init_db()
    with open(os.path.join(BASE, "supervisor.pid"), "w") as f: f.write(str(os.getpid()))
    procs = {n: start(n) for n in STAGES}
    started = {n: time.time() for n in STAGES}
    c = db()
    while True:
        time.sleep(10)
        for n, p in list(procs.items()):
            if p.poll() is not None:
                lg.warning("%s exited code=%s; restarting", n, p.returncode)
                time.sleep(2); procs[n] = start(n); started[n] = time.time()
        try:
            hb = {r["stage"]: r["ts"] for r in c.execute("SELECT stage, ts FROM heartbeats")}
            for n in STAGES:
                if time.time() - started[n] > 300 and time.time() - hb.get(n, 0) > 300:
                    lg.warning("%s heartbeat stale; killing for restart", n)
                    procs[n].kill()
            with open(os.path.join(BASE, "pids.json"), "w") as f:
                json.dump({n: p.pid for n, p in procs.items()} | {"supervisor": os.getpid()}, f)
        except Exception as e:
            lg.warning("hb check: %s", e)

if __name__ == "__main__":
    main()

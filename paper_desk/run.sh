#!/usr/bin/env bash
# Outer watchdog loop: restarts the supervisor if it ever dies. PAPER ONLY.
cd "$(dirname "$0")"
mkdir -p logs
if [ -x .venv/bin/python ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi
while true; do
  echo "$(date '+%F %T %Z') starting supervisor" >> logs/run.log
  "$PY" supervisor.py >> logs/supervisor.stdout 2>&1
  echo "$(date '+%F %T %Z') supervisor exited ($?), restarting in 5s" >> logs/run.log
  sleep 5
done

#!/usr/bin/env bash
# Stop the paper desk started from this folder. PAPER ONLY.
HERE="$(cd "$(dirname "$0")" && pwd)"
tmux kill-session -t memedesk 2>/dev/null
if [ -f "$HERE/supervisor.pid" ]; then
  spid="$(cat "$HERE/supervisor.pid" 2>/dev/null || true)"
  if [ -n "$spid" ]; then
    ppid="$(ps -o ppid= -p "$spid" 2>/dev/null | tr -d ' ' || true)"
    if [ -n "$ppid" ]; then
      pargs="$(ps -o args= -p "$ppid" 2>/dev/null || true)"
      case "$pargs" in
        *run.sh*) kill "$ppid" 2>/dev/null || true ;;
      esac
    fi
    kill "$spid" 2>/dev/null || true
  fi
fi
pkill -f "$HERE/stages/" 2>/dev/null || true
pkill -f "$HERE/supervisor.py" 2>/dev/null || true
pkill -f "$HERE/.venv/bin/python supervisor.py" 2>/dev/null || true
echo stopped

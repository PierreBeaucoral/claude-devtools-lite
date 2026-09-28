#!/usr/bin/env bash
# Claude DevTools launcher (Linux).
#
# Starts the Python server if it isn't already running, then opens the
# dashboard authenticated via the /launch cookie handoff. Prefers an app-mode
# browser window (looks like a standalone app); falls back to the default
# browser. Full terminal support — Linux has POSIX pseudo-terminals.
set -u

PORT="${PORT:-3456}"
URL="http://127.0.0.1:$PORT"

# resolve the repo next to this script: launchers/linux/ -> repo root
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SERVER="$(cd "$HERE/../.." && pwd)/server.py"
[ -f "$SERVER" ] || SERVER="$HOME/claude-devtools-lite/server.py"
if [ ! -f "$SERVER" ]; then
  echo "server.py not found (looked next to this script and in ~/claude-devtools-lite)" >&2
  exit 1
fi

LOGDIR="${XDG_STATE_HOME:-$HOME/.local/state}"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/claude-devtools.log"

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "python3 not found — install it (e.g. sudo apt install python3)" >&2
  exit 1
fi

if ! curl -s -m 1 -o /dev/null "$URL/"; then
  # the log is private: umask 077 makes a new log file 0600
  ( umask 077; nohup "$PY" "$SERVER" --port "$PORT" > "$LOG" 2>&1 & )   # fresh per start; the server rotates its own log
  for _ in $(seq 1 40); do
    curl -s -m 1 -o /dev/null "$URL/" && break
    sleep 0.25
  done
fi

chmod 600 "$LOG" 2>/dev/null
[ -n "${CDL_NO_OPEN:-}" ] && exit 0
# a one-time login URL (60 s): the token itself never reaches the browser's
# argv; the helper also refuses a server that doesn't hold our token
TARGET="$("$PY" "$SERVER" --port "$PORT" --launch-url)" || exit 1

# app-mode window first (chromium-family), then generic browser openers
for B in google-chrome chromium chromium-browser brave-browser microsoft-edge vivaldi; do
  if command -v "$B" >/dev/null 2>&1; then
    nohup "$B" --app="$TARGET" --window-size=1500,950 >/dev/null 2>&1 &
    exit 0
  fi
done
for O in xdg-open gio open firefox; do
  if command -v "$O" >/dev/null 2>&1; then
    case "$O" in
      gio) nohup gio open "$TARGET" >/dev/null 2>&1 & ;;
      *)   nohup "$O" "$TARGET" >/dev/null 2>&1 & ;;
    esac
    exit 0
  fi
done
echo "Open this URL manually: $TARGET"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DAEMON_JS="$ROOT_DIR/daemon/syn_topbar_daemon.js"

if ! command -v node >/dev/null 2>&1; then
  echo "[antigravity-sync] ERROR: Node.js >= 22 is required." >&2
  exit 1
fi

pkill -f "syn_topbar_daemon.js" >/dev/null 2>&1 || true

nohup node "$DAEMON_JS" >/dev/null 2>&1 &
echo "[antigravity-sync] Topbar 'Syn' daemon started (PID $!)."

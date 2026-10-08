#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export WS_PORT="${WS_PORT:-8765}"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
ELECTRON_DIR="$SCRIPT_DIR/electron"

BACKEND_PID=""
OVERLAY_PID=""

# ── Helpers ────────────────────────────────────────────────────────────────────

die() { echo "[launcher] ERROR: $*" >&2; exit 1; }

# ── Prerequisite checks ────────────────────────────────────────────────────────

[[ -x "$VENV_PYTHON" ]] \
    || die ".venv/bin/python not found. Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

[[ -f "$ELECTRON_DIR/package.json" ]] \
    || die "electron/package.json not found — run from repo root."

command -v npm &>/dev/null \
    || die "npm not found. Install Node.js ≥ 18 from https://nodejs.org"

if [[ ! -d "$ELECTRON_DIR/node_modules/electron" ]]; then
    echo "[launcher] Electron not installed — running npm install in electron/ ..."
    (cd "$ELECTRON_DIR" && npm install --silent)
    echo "[launcher] npm install done."
fi

# ── Ollama (non-destructive: skip if already running) ─────────────────────────

if pgrep -f "ollama serve" &>/dev/null || pgrep -x ollama &>/dev/null; then
    echo "[launcher] Ollama is running."
elif command -v ollama &>/dev/null; then
    echo "[launcher] Starting Ollama daemon..."
    ollama serve &>/dev/null &
    sleep 2
    echo "[launcher] Ollama started."
else
    echo "[launcher] WARNING: ollama not found — LLM hints will be unavailable."
fi

# ── Cleanup: kill child processes on Ctrl+C / TERM ────────────────────────────

cleanup() {
    echo ""
    echo "[launcher] Stopping..."
    [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
    if [[ -n "${OVERLAY_PID:-}" ]]; then
        # Kill npm and its spawned electron child
        kill "$OVERLAY_PID" 2>/dev/null || true
        pkill -TERM -P "$OVERLAY_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    echo "[launcher] Done."
    exit 0
}
trap cleanup INT TERM

# ── Launch ─────────────────────────────────────────────────────────────────────

echo "[launcher] Phase 3 — WS_PORT=${WS_PORT}"
echo ""

# Python backend: prefix every line with [backend]
"$VENV_PYTHON" "$SCRIPT_DIR/run_live_assistant.py" \
    > >(awk '{print "[backend] " $0; fflush()}') 2>&1 &
BACKEND_PID=$!

# Give backend 1 s to bind the WS port before Electron connects
sleep 1

# Electron overlay: prefix every line with [overlay]
(cd "$ELECTRON_DIR" && npm start 2>&1) \
    > >(awk '{print "[overlay] " $0; fflush()}') &
OVERLAY_PID=$!

echo "[launcher] Backend PID=$BACKEND_PID  Overlay PID=$OVERLAY_PID"
echo "[launcher] Press Ctrl+C to stop both."
echo ""

wait

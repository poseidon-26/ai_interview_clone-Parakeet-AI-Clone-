# Phase 3 — Stealth Interview Overlay

## What this adds

- `ws_bridge.py` — Python WebSocket server (port 8765) embedded in the existing pipeline
- `electron/` — frameless transparent overlay showing AI hints in real time
- `start_phase3.sh` — single-command launcher for both processes

---

## Quick start

```bash
./start_phase3.sh
```

To run on a non-default port:

```bash
WS_PORT=9000 ./start_phase3.sh
```

---

## Prerequisites

| Requirement | Check |
|---|---|
| Python 3.11 `.venv` with all Phase 2 deps | `ls .venv/bin/python` |
| `websockets` installed in `.venv` | `.venv/bin/pip show websockets` |
| Ollama running with a model pulled | `ollama list` |
| BlackHole 2ch audio device | System Preferences → Sound |
| Node.js ≥ 18 | `node --version` |
| Electron installed in `electron/` | `cat electron/node_modules/electron/dist/version` |

---

## One-time setup

```bash
# Install Python bridge dependency
.venv/bin/pip install websockets

# Install Electron
cd electron && npm install && cd ..

# Pull a local model (if not already done)
ollama pull llama3.2:3b        # ~2 GB; set OPENAI_MODEL=llama3.2:3b in .env
```

---

## Fallback: manual two-terminal run

**Terminal 1 — Python backend**

```bash
.venv/bin/python run_live_assistant.py
```

**Terminal 2 — Electron overlay**

```bash
cd electron && WS_PORT=8765 npm start
```

---

## Overlay controls

| Key / Action | Effect |
|---|---|
| `⌘ Shift H` | Toggle visibility (show / hide) — global |
| `⌘ Shift T` | Toggle click-through (ghost mode) — global |
| `T` | Toggle click-through — when overlay is focused |
| `+` or `=` | Increase hint font size (10–18 px) |
| `-` | Decrease hint font size |
| `]` | Increase window opacity (0.2–1.0) |
| `[` | Decrease window opacity |
| Drag title bar | Reposition overlay |
| Resize edge/corner | Resize overlay |

**Ghost mode** — overlay dims to 45% and mouse/keyboard pass through to the window underneath. Use during screen share. Press `⌘ Shift T` or `T` again to return.

**Status chip** (top-left) shows connection state:
- 🟢 `live` — backend connected, receiving hints
- 🟡 `connecting` / `reconnect Xs` — waiting / countdown to next attempt
- 🔴 `offline` — max retry reached; restart backend and overlay

---

## Configuration (`.env`)

```ini
# Backend LLM (Ollama, free/local)
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_API_KEY=ollama
OPENAI_MODEL=llama3.2:3b

# WS bridge port — must match WS_PORT used when starting Electron
WS_PORT=8765

# Transcription
WHISPER_MODEL=tiny.en
AUDIO_PROFILE=call      # call = Zoom/Meet; youtube = clean system audio

# Terminal output (off = silent, overlay gets everything)
ASSISTANT_OUTPUT_MODE=off

# Transcript dedup — suppress chunks with >X% word overlap vs previous chunk
TRANSCRIPT_DEDUP_THRESHOLD=0.80

# Session logging — write timestamped transcript+hint JSONL for replay
# SESSION_LOG=session.jsonl

# Replay mode — feed a saved session log back through the pipeline
# REPLAY_FILE=session.jsonl
# REPLAY_SPEED=1.0     # 1.0=real-time, 2.0=double speed, 0=instant

# ── Answer quality (Phase 4) ──────────────────────────────────────────────────
ANSWER_STYLE=strict_bullets   # strict_bullets | legacy
INTERVIEW_MODE=auto           # auto | behavioral | programming

# ── ASR reliability (Phase 4) ────────────────────────────────────────────────
ASR_ADAPTIVE_MODE=true        # auto-switch to HQ profile when quality is poor
ASR_UNCERTAIN_MESSAGE=unclear, ask repeat
# ASR_QUALITY_THRESHOLD=0.35  # 0.0–1.0; lower = more permissive
```

---

## Session logging

Enable by setting `SESSION_LOG` in `.env` (or as an env override):

```bash
SESSION_LOG=session.jsonl .venv/bin/python run_live_assistant.py
```

Each line in the file is a JSON record:

```json
{"ts": 1714000000.1, "type": "transcript", "text": "Can you explain Big O notation?"}
{"ts": 1714000003.8, "type": "hint", "text": "Big O describes worst-case growth...", "confidence": "high", "latency_ms": 812.0}
```

The log appends across sessions (never overwrites), so rotate manually if needed.

---

## Replay mode (offline testing)

Run the full LLM + overlay pipeline against a saved session log — no microphone or BlackHole needed:

```bash
# Real-time replay
REPLAY_FILE=session.jsonl .venv/bin/python run_live_assistant.py

# 2× speed
REPLAY_SPEED=2.0 REPLAY_FILE=session.jsonl .venv/bin/python run_live_assistant.py

# Instant (as fast as the LLM can respond)
REPLAY_SPEED=0 REPLAY_FILE=session.jsonl .venv/bin/python run_live_assistant.py
```

Start the Electron overlay first (`cd electron && npm start`) so you can watch hints arrive in real time during replay.

---

## Troubleshooting

### Overlay status chip stays yellow / "reconnect Xs"

The Electron app cannot reach the WebSocket server.

1. Confirm the backend printed `[WsBridge] listening on ws://127.0.0.1:8765`.
2. Check `WS_PORT` is identical in both processes:
   ```bash
   WS_PORT=8765 .venv/bin/python run_live_assistant.py   # backend
   WS_PORT=8765 npm start                                 # overlay (in electron/)
   ```
3. Change `WS_PORT` in `.env` if port 8765 is already in use.

### "npm not found" on macOS

```bash
brew install node
```

### Electron missing / re-install

```bash
cd electron && rm -rf node_modules package-lock.json && npm install
cat node_modules/electron/dist/version   # e.g. 28.3.3
```

### Ollama not responding

```bash
ollama serve          # start daemon in background
ollama pull llama3.2:3b
```

### Replay: "No transcript events found"

The log file must contain lines with `"type": "transcript"`. Confirm the session was recorded with `SESSION_LOG` set and that `[Heard]` lines appeared during the original run.

### No `[Heard]` lines (transcription silent)

1. BlackHole 2ch must be the active output device (System Preferences → Sound).
2. For Zoom/Meet, set `AUDIO_PROFILE=call` in `.env`.
3. Verify device: `.venv/bin/python check_audio.py`

---

## npm scripts

```bash
cd electron

npm start        # production run (default)
npm run dev      # run with Electron debug logging
npm run start:prod   # NODE_ENV=production
```

---

## Sanity checks

```bash
# Python
.venv/bin/python -m py_compile ws_bridge.py run_live_assistant.py

# Electron JS
node --check electron/main.js electron/preload.js electron/renderer/renderer.js

# Launcher
bash --norc -n start_phase3.sh && echo "start_phase3.sh OK"

# WS bridge smoke test
.venv/bin/python -c "
from ws_bridge import WsBridge; import time
b = WsBridge(); b.start(); time.sleep(0.3)
print('Bridge ready:', b._ready.is_set())
"
```

---

## Architecture

```
BlackHole 2ch / REPLAY_FILE
    │
    ▼
transcriber.py  ──[PCM]──►  faster-whisper  ──[text]──►  on_transcript()
                              ▲  adaptive                      │
                              │  profile                  asr_quality.assess()
                    report_quality() ◄────────────────────────┤
                                                              │
                                                   ┌──────────┴──────────┐
                                         uncertain │                     │ clean
                                                   ▼                     ▼
                                      overlay: "unclear…"       transcript_queue
                                                                         │
                                                                         ▼
                                                                answer_engine.py
                                                              (prompts.classify_mode)
                                                                         │
                                                            ┌────────────┴──────────┐
                                                     behavioral               programming
                                                     2–3 bullets         Approach/Complexity/
                                                                           Code/Edge cases
                                                                         │
                                                              Answer object + ws_bridge
                                                                         │
                                                                         ▼
                                                                  Electron overlay
                                                          status chip · hints · transcript
```

---

## Answer quality (Phase 4)

### Interview mode detection

The system auto-classifies each transcript chunk as **behavioral** or **programming** by
scanning for coding keywords (algorithm, complexity, O(…), BFS, sort, etc.).

Override with `INTERVIEW_MODE=behavioral` or `INTERVIEW_MODE=programming` to lock the mode.

**Behavioral output** (2–3 speakable bullets):
```
• Led backend migration to microservices, cutting deploy time by 40 percent.
• Coordinated across three teams under a two-week hard deadline.
• Used feature flags for iterative rollout to minimise production risk.
```

**Programming output** (structured 4-section format):
```
Approach:
• Use a sliding window to track max sum of k elements.

Complexity: O(n) time, O(1) space

Code:
  window = sum(arr[:k])
  best = window
  for i in range(k, len(arr)):
      window += arr[i] - arr[i-k]
      best = max(best, window)
  return best

Edge cases:
• k > len(arr) → return -1 or raise.
• All negative values → max subarray is the least negative.
```

### ASR uncertainty gate

Before each LLM call, `asr_quality.assess()` scores the transcript 0.0–1.0 across:
- Alpha-character ratio (gibberish check)
- Word-repetition artifacts (Whisper looping)
- Consecutive-word stutter detection
- Minimum meaningful word count

If the score falls below `ASR_QUALITY_THRESHOLD` (default 0.35), the LLM is **skipped** and
the overlay shows `ASR_UNCERTAIN_MESSAGE` ("unclear, ask repeat") instead.

Terminal prefix distinguishes uncertain chunks: `[Heard?]` vs `[Heard]`.

### Adaptive ASR profile switching

When `ASR_ADAPTIVE_MODE=true` (default), the rolling average quality score over the last 5
transcripts controls the Whisper profile in-place:

| Condition | Action |
|---|---|
| avg < 0.40 | Switch to HQ profile (beam_size 5, tighter VAD) |
| avg > 0.60 | Switch back to balanced profile |

The switch is printed to terminal: `[ASR] Quality low (0.31) → switched to HQ profile`.
No process restart required; the transcription thread picks up the new settings each window.

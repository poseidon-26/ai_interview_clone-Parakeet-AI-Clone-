# COMPACT_CHAT.md — AI Interview Assistant session summary
_Last updated: 2026-04-25_

---

## Phase completion status

| Phase | Status |
|-------|--------|
| Phase 1 — ASR (Whisper / transcriber.py) | ✅ Complete |
| Phase 2 — LLM answer engine | ✅ Complete |
| Phase 2.1 — Stability patch (retry/pause/local fallback) | ✅ Complete |
| Phase 2.2 — Transcription hardening (threading, overlap, VAD) | ✅ Complete |
| Phase 2.3 — Silent assistant mode + context queue | ✅ Complete |
| Phase 3 — Electron overlay + WebSocket bridge | ✅ Complete |
| Phase 3.1 — Stability + Readability | ✅ Complete |

---

## Critical constraints

- **FREE ONLY — no paid APIs.** Backend is local Ollama (`OPENAI_BASE_URL=http://localhost:11434/v1`, `OPENAI_API_KEY=ollama`).
- Previously exposed OpenAI cloud key is treated as compromised — do not use.
- Architecture: `transcriber.py` / `asr_quality.py` / `answer_engine.py` interfaces are frozen.
- `start_phase3.sh` startup script must remain functional.
- All existing Phase 3 hotkeys preserved.

---

## Startup commands

```bash
# Terminal 1 — Python backend
cd ~/Desktop/ai-interview-clone
ollama serve &          # if not already running
.venv/bin/python run_live_assistant.py

# Terminal 2 — Electron overlay
cd ~/Desktop/ai-interview-clone/electron
npm start

# Optional: replay a session log
REPLAY_FILE=session.jsonl .venv/bin/python run_live_assistant.py

# Optional: output modes
ASSISTANT_OUTPUT_MODE=full .venv/bin/python run_live_assistant.py
ASSISTANT_OUTPUT_MODE=compact .venv/bin/python run_live_assistant.py
```

---

## Hotkeys (overlay)

| Key | Action |
|-----|--------|
| `+` / `-` | Font size ±1px |
| `[` / `]` | Opacity ±0.1 |
| `T` | Toggle click-through / ghost mode |
| `R` | Force-refresh (bypass freeze, re-query) |
| `⌘⇧H` | Hide/show overlay window |
| `⌘⇧R` | Force-refresh (global shortcut) |

---

## Phase 3.1 — What was implemented (2026-04-25)

### Goal
Stop answer thrashing and make overlay output readable/speakable during real conversational speech (pauses, fillers, rephrasing).

### Changes by file

#### `run_live_assistant.py`
- **Turn commit state machine**: transcripts buffered in `turn_buf`; timer-based commit after 1100 ms silence, or 50 ms on "?" or transcript stability
- **Generation counter** (`turn_gen`) prevents stale timers from committing after reset
- **Answer freeze window**: after answer shown, `freeze_until[0]` blocks replacement for 8 s
- **Semantic gate**: `_semantic_sim()` (Jaccard) suppresses question if similarity ≥ 0.78 to previous committed question
- **Disfluency cleanup**: `_clean_disfluencies()` removes "you know", "I mean", "sort of", "kind of", "basically" + stutter repetitions (`_STUTTER_RE`)
- **Force-refresh handler**: `_on_ws_message()` clears freeze and re-queues last committed question when overlay sends `{"type":"force_refresh"}`
- **New env vars**: `TURN_SILENCE_MS`, `ANSWER_FREEZE_SECONDS`, `QUESTION_STABILITY_WINDOWS`, `QUESTION_SIMILARITY_THRESHOLD`, `FILLER_CLEANUP`

#### `ws_bridge.py`
- Added `send_freeze(seconds)` method
- Added `set_message_handler(callback)` to receive incoming WS messages
- Changed handler from `await websocket.wait_closed()` to `async for raw in websocket:` to enable bidirectional communication

#### `electron/main.js`
- Window size: 560×300
- Added `get-config` IPC handler (returns `wsPort`, `fontSize`, `lineHeight`)
- Added `OVERLAY_FONT_SIZE` / `OVERLAY_LINE_HEIGHT` env var reading
- Added `⌘⇧R` global shortcut → sends `"force-refresh"` to renderer

#### `electron/preload.js`
- Added `getConfig()` IPC invoke
- Added `onForceRefresh(cb)` listener

#### `electron/renderer/index.html`
- Added `<span id="lock-badge"></span>` in titlebar-left
- Titlebar hint text updated to include `⌘⇧R refresh`

#### `electron/renderer/style.css`
- `--hint-font: 20px` (was 12px), `--hint-line-height: 1.45`
- `#lock-badge`: hidden by default; yellow badge shown on `.active`
- `#hints`: `flex-direction: column`, `overflow-y: auto`, `gap: 4px`
- `.hint-item`: `line-height: var(--hint-line-height)`, `word-wrap: break-word; white-space: pre-wrap`

#### `electron/renderer/renderer.js`
- `FONT_MAX` raised to 28
- Freeze state: `freezeEndMs`, `freezeTickId`, `startFreeze()`, `clearFreeze()`, `_tickFreeze()`
- `applyFont()` sets both `--hint-font` and `--hint-line-height`
- `sendForceRefresh()` sends WS message + clears local freeze
- `"R"` key shortcut added
- `"freeze"` WS message type handled via `startFreeze()`
- DOM-diff `renderHints()`: updates nodes in-place (no `innerHTML` wipe) to prevent flicker
- Boot uses `getConfig()` instead of `getWsPort()`

#### `.env`
- Phase 3.1 block added:
  ```
  TURN_SILENCE_MS=1100
  ANSWER_FREEZE_SECONDS=8
  QUESTION_STABILITY_WINDOWS=2
  QUESTION_SIMILARITY_THRESHOLD=0.78
  FILLER_CLEANUP=true
  OVERLAY_FONT_SIZE=20
  OVERLAY_LINE_HEIGHT=1.45
  ```

---

## Key design patterns

| Pattern | Where used | Why |
|---------|-----------|-----|
| List-as-mutable-closure | `turn_timer`, `turn_gen`, `committed_q`, `freeze_until` | Closure mutation without `nonlocal` |
| Generation counter | `turn_gen[0]` | Prevents stale timers from firing after reset |
| Jaccard similarity | `_semantic_sim()` | Symmetric overlap for semantic dedup gate |
| Asymmetric overlap | `_word_overlap()` | Existing transcript dedup (unchanged) |
| DOM-diff rendering | `renderHints()` | Prevents destroy/create flicker in overlay |
| Bidirectional WS | `async for raw in websocket:` | Enables force-refresh round-trip |
| Producer/consumer threading | `pcm_queue` → transcription thread | Capture never blocks on Whisper inference |
| Rolling deque context | `assistant_context_queue` | Phase 3 handoff — latest N answers |

---

## Tests run (Phase 3.1)

```
PASS  disfluency cleanup
PASS  semantic similarity
PASS  WsBridge extensions
PASS  turn silence timer
PASS  stale timer suppressed
All Phase 3.1 tests passed.
```

---

## Phase 3 — What was implemented (before Phase 3.1)

- **`ws_bridge.py`**: WebSocket server broadcasting transcript/hint messages to Electron overlay
- **`electron/`**: Full Electron app with:
  - `main.js` — BrowserWindow, system tray, global shortcuts (`⌘⇧H` hide/show)
  - `preload.js` — contextBridge exposing IPC to renderer
  - `renderer/index.html` + `renderer.js` + `style.css` — overlay UI
- Overlay shows: live transcript bar, up to 3 stacked hint cards (newest = bright, older = dim), status chip (connecting/live/reconnecting/offline), click-through ghost mode

---

## Phase 2 history (Phases 1–2.3)

### `transcriber.py`
- BlackHole 2ch capture at 16 kHz mono int16
- Decoupled threads: capture → `pcm_queue` → transcription
- 1.5 s sliding window, 0.5 s overlap; energy VAD; hallucination regex filter
- `AUDIO_PROFILE=youtube|call` env switch

### `answer_engine.py`
- OpenAI-compatible client (points at Ollama)
- `@retry` with `_is_retryable()` — never retries auth/quota errors
- `DEBOUNCE_SECONDS=3` gate; fatal errors → `_paused=True`; optional local llama-cpp fallback

### `run_live_assistant.py`
- `ASSISTANT_OUTPUT_MODE=off|compact|full` (default `off`)
- `assistant_context_queue` deque for Phase 3 handoff
- Consumer thread: transcript → LLM → overlay via WebSocket
- Session log (JSONL) + replay mode

### `asr_quality.py`
- Quality scoring for transcripts; `UNCERTAIN_THRESHOLD` gate
- Low-quality transcripts: show "unclear, ask repeat" in overlay instead of querying LLM

### `prompts.py`
- System prompt + `build_messages()` for LLM context assembly
- `ANSWER_STYLE=strict_bullets|legacy`, `INTERVIEW_MODE=auto|behavioral|programming`

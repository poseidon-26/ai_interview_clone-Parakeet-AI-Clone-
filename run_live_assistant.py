"""
Phase 2+3 orchestrator — transcription → LLM → Electron overlay via WebSocket.

Usage:
    .venv/bin/python run_live_assistant.py

Output modes (ASSISTANT_OUTPUT_MODE):
    off      — silent; pipeline active, all output goes to overlay
    compact  — one-line summary printed at most every 10 s
    full     — detailed boxed output with confidence + latency

Session logging (JSONL file with transcript + hint events):
    SESSION_LOG=session.jsonl .venv/bin/python run_live_assistant.py

Offline replay from a saved session log:
    REPLAY_FILE=session.jsonl .venv/bin/python run_live_assistant.py
    REPLAY_SPEED=2.0 REPLAY_FILE=session.jsonl .venv/bin/python run_live_assistant.py
"""

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque

from dotenv import load_dotenv

from answer_engine import Answer, AnswerEngine
from asr_quality import assess as _asr_assess, UNCERTAIN_THRESHOLD as _ASR_THRESHOLD
from transcriber import run_with_callback, report_quality as _asr_report_quality
from ws_bridge import WsBridge

load_dotenv()

logging.basicConfig(
    level=logging.ERROR,
    format="%(levelname)s  %(name)s: %(message)s",
)

# ── Output mode ───────────────────────────────────────────────────────────────

OUTPUT_MODE           = os.getenv("ASSISTANT_OUTPUT_MODE", "off").lower()
COMPACT_INTERVAL      = 10.0
ASR_UNCERTAIN_MESSAGE = os.getenv("ASR_UNCERTAIN_MESSAGE", "unclear, ask repeat")

# ── Phase 3.1: turn state machine + answer freeze ─────────────────────────────

TURN_SILENCE_MS            = int(os.getenv("TURN_SILENCE_MS",            "1100"))
ANSWER_FREEZE_SECONDS      = float(os.getenv("ANSWER_FREEZE_SECONDS",    "8.0"))
QUESTION_STABILITY_WINDOWS = int(os.getenv("QUESTION_STABILITY_WINDOWS", "2"))
QUESTION_SIMILARITY_THRESHOLD = float(os.getenv("QUESTION_SIMILARITY_THRESHOLD", "0.78"))
FILLER_CLEANUP             = os.getenv("FILLER_CLEANUP", "true").lower() == "true"

_MAX_TURN_BUF = 50  # cap on buffered chunks per turn (memory safety)

# Higher-level fillers not caught by transcriber's _FILLER_RE
_DISFLUENCY_RE = re.compile(
    r"\b(you\s+know|I\s+mean|sort\s+of|kind\s+of|basically)\b[,;]?\s*",
    re.IGNORECASE,
)
_STUTTER_RE = re.compile(r"\b(\w{2,})\s+\1\b", re.IGNORECASE)


def _clean_disfluencies(text: str) -> str:
    """Remove higher-level disfluencies and stutter patterns from transcript."""
    if not FILLER_CLEANUP:
        return text
    text = _DISFLUENCY_RE.sub(" ", text)
    text = _STUTTER_RE.sub(r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _semantic_sim(a: str, b: str) -> float:
    """Jaccard similarity on word sets — symmetric version of _word_overlap."""
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa and not wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

# ── Rolling assistant context (Phase 3 handoff) ───────────────────────────────

ASSISTANT_CONTEXT_SIZE = int(os.getenv("ASSISTANT_CONTEXT_SIZE", "5"))

assistant_context_queue: deque[Answer] = deque(maxlen=ASSISTANT_CONTEXT_SIZE)
_context_lock = threading.Lock()


def get_assistant_context() -> list[Answer]:
    """Return a snapshot of the latest assistant suggestions (thread-safe)."""
    with _context_lock:
        return list(assistant_context_queue)


# ── Dedup helpers ─────────────────────────────────────────────────────────────

CONTEXT_WINDOW             = 10
SIMILARITY_THRESHOLD       = 0.75   # hint dedup: suppress if >75% word overlap
TRANSCRIPT_DEDUP_THRESHOLD = float(os.getenv("TRANSCRIPT_DEDUP_THRESHOLD", "0.80"))


def _word_overlap(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wb:
        return 0.0
    return len(wa & wb) / len(wb)


def _is_new_content(prev: str, curr: str) -> bool:
    return not prev or _word_overlap(prev, curr) < SIMILARITY_THRESHOLD


# ── Session logging ────────────────────────────────────────────────────────────

SESSION_LOG = os.getenv("SESSION_LOG", "")


def _open_log():
    """Open SESSION_LOG for appending. Returns (file, lock) or (None, None)."""
    if not SESSION_LOG:
        return None, None
    f = open(SESSION_LOG, "a", buffering=1)   # line-buffered — flush on each newline
    print(f"[logger] Session log → {SESSION_LOG}")
    return f, threading.Lock()


def _write_log(f, lock: threading.Lock, record: dict) -> None:
    with lock:
        f.write(json.dumps(record) + "\n")


# ── Replay mode ────────────────────────────────────────────────────────────────

REPLAY_FILE  = os.getenv("REPLAY_FILE", "")
REPLAY_SPEED = float(os.getenv("REPLAY_SPEED", "1.0"))


def _replay_from_file(on_transcript, path: str) -> None:
    """Feed transcript events from a JSONL session log back through the pipeline."""
    events = []
    with open(path) as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

    chunks = [e for e in events if e.get("type") == "transcript"]
    if not chunks:
        print(f"[replay] No transcript events found in {path}")
        return

    print(f"[replay] {len(chunks)} chunks  speed={REPLAY_SPEED}x  source={path}")

    t0_real = time.time()
    t0_log  = chunks[0]["ts"]

    try:
        for ev in chunks:
            if REPLAY_SPEED > 0:
                target = t0_real + (ev["ts"] - t0_log) / REPLAY_SPEED
                gap = target - time.time()
                if gap > 0.01:
                    time.sleep(gap)
            on_transcript(ev["text"])

        print("[replay] Done — LLM may still be processing  (Ctrl+C to stop)")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    mode_label = {
        "off": "silent (pipeline active)", "compact": "compact", "full": "full"
    }.get(OUTPUT_MODE, OUTPUT_MODE)

    print("=" * 60)
    print("  AI Interview Assistant — Phase 2+3")
    if REPLAY_FILE:
        print(f"  Mode        : replay  ({REPLAY_FILE})")
    else:
        print(f"  Output mode : {mode_label}")
    print("  Ctrl+C to stop")
    print("=" * 60 + "\n")

    bridge = WsBridge()
    bridge.start()

    try:
        engine = AnswerEngine()
    except Exception as exc:
        print(f"[ERROR] Failed to initialise answer engine: {exc}")
        sys.exit(1)

    context: deque[str]         = deque(maxlen=CONTEXT_WINDOW)
    transcript_queue: queue.Queue[str | None] = queue.Queue()

    last_printed    = ""
    last_compact_at = 0.0
    last_transcript = ""           # for transcript-level sliding-window dedup
    log_file, log_lock = _open_log()

    # ── Turn state machine ────────────────────────────────────────────────────
    turn_buf:      list        = []            # recent ASR chunks for this turn
    turn_timer:    list        = [None]        # [Optional[threading.Timer]]
    turn_gen:      list        = [0]           # generation counter → detects stale timers
    committed_q:   list        = [""]          # last question sent to LLM
    freeze_until:  list        = [0.0]         # wall-clock expiry of answer freeze
    stability_buf: deque       = deque(maxlen=QUESTION_STABILITY_WINDOWS)
    turn_lock = threading.Lock()

    def _commit_turn(gen: int) -> None:
        with turn_lock:
            if gen != turn_gen[0] or not turn_buf:
                return
            candidate = turn_buf[-1]           # most complete chunk = last
            turn_buf.clear()
            turn_timer[0] = None

        candidate = _clean_disfluencies(candidate)
        if not candidate.strip():
            return

        prev      = committed_q[0]
        sim       = _semantic_sim(prev, candidate) if prev else 0.0
        is_frozen = time.time() < freeze_until[0]

        # Same question rehash during active freeze → suppress
        if is_frozen and sim >= QUESTION_SIMILARITY_THRESHOLD:
            return
        # Same question when not frozen → suppress (already answered)
        if not is_frozen and prev and sim >= QUESTION_SIMILARITY_THRESHOLD:
            return

        committed_q[0] = candidate
        transcript_queue.put(candidate)

    # ------------------------------------------------------------------ #
    # Consumer thread — LLM calls; never blocks audio capture             #
    # ------------------------------------------------------------------ #
    def consumer() -> None:
        nonlocal last_printed, last_compact_at

        try:
            while True:
                chunk = transcript_queue.get()
                if chunk is None:
                    break

                prev_context = list(context)
                context.append(chunk)

                answer = engine.ask(prev_context, chunk)
                if answer is None:
                    continue

                text = answer.answer_text
                if not text or not _is_new_content(last_printed, text):
                    continue

                with _context_lock:
                    assistant_context_queue.append(answer)
                bridge.send_hint(answer)
                bridge.send_freeze(ANSWER_FREEZE_SECONDS)
                freeze_until[0] = time.time() + ANSWER_FREEZE_SECONDS

                if log_file:
                    _write_log(log_file, log_lock, {
                        "ts": time.time(), "type": "hint",
                        "text": answer.answer_text,
                        "confidence": answer.confidence_hint,
                        "latency_ms": round(answer.latency_ms, 1),
                    })

                last_printed = text

                if OUTPUT_MODE == "off":
                    pass

                elif OUTPUT_MODE == "compact":
                    now = time.time()
                    if now - last_compact_at >= COMPACT_INTERVAL:
                        last_compact_at = now
                        summary = text[:80] + ("…" if len(text) > 80 else "")
                        print(f"[AI] {summary}")

                else:
                    tag = f"{answer.confidence_hint} · {answer.latency_ms:.0f} ms"
                    print(
                        f"\n╔══ Assistant [{tag}]\n"
                        f"║  {text.replace(chr(10), chr(10) + '║  ')}\n"
                        f"╚{'═' * 56}\n"
                    )

        except Exception:
            pass  # suppress teardown noise from daemon thread

    consumer_thread = threading.Thread(target=consumer, daemon=True)
    consumer_thread.start()

    # ------------------------------------------------------------------ #
    # Main thread — audio capture or file replay                          #
    # ------------------------------------------------------------------ #
    def on_transcript(text: str) -> None:
        nonlocal last_transcript
        # Skip near-duplicate chunks produced by the sliding-window overlap
        if last_transcript and _word_overlap(last_transcript, text) >= TRANSCRIPT_DEDUP_THRESHOLD:
            return
        last_transcript = text

        # ── ASR quality gate ──────────────────────────────────────────────────
        quality = _asr_assess(text)
        _asr_report_quality(quality)

        if quality < _ASR_THRESHOLD:
            print(f"[Heard?] {text}  [uncertain: {quality:.2f}]")
            bridge.send_transcript(text)
            bridge.send_hint(Answer(
                answer_text=ASR_UNCERTAIN_MESSAGE,
                confidence_hint="low",
                latency_ms=0.0,
            ))
            if log_file:
                _write_log(log_file, log_lock, {
                    "ts": time.time(), "type": "transcript",
                    "text": text, "uncertain": True,
                })
            return
        # ─────────────────────────────────────────────────────────────────────

        cleaned = _clean_disfluencies(text)
        print(f"[Heard] {text}")
        bridge.send_transcript(text)
        if log_file:
            _write_log(log_file, log_lock, {
                "ts": time.time(), "type": "transcript", "text": text,
            })

        # ── Turn state machine: buffer → silence → commit ─────────────────────
        with turn_lock:
            turn_buf.append(cleaned or text)
            if len(turn_buf) > _MAX_TURN_BUF:
                turn_buf.pop(0)
            stability_buf.append(text)

            if turn_timer[0] is not None:
                turn_timer[0].cancel()

            turn_gen[0] += 1
            gen = turn_gen[0]

            # Early-commit triggers: question mark OR transcript stability
            ends_q = (cleaned or text).rstrip().endswith("?")
            stable = (
                len(stability_buf) >= QUESTION_STABILITY_WINDOWS and
                _word_overlap(stability_buf[-2], stability_buf[-1]) >= 0.70
            ) if len(stability_buf) >= 2 else False

            delay = 0.05 if (ends_q or stable) else TURN_SILENCE_MS / 1000.0
            t = threading.Timer(delay, _commit_turn, args=[gen])
            t.daemon = True
            t.start()
            turn_timer[0] = t

    # ── Force-refresh handler (incoming WS message from overlay) ─────────────
    def _on_ws_message(msg: dict) -> None:
        if msg.get("type") == "force_refresh":
            freeze_until[0] = 0.0
            q = committed_q[0]
            if q:
                transcript_queue.put(q)
                print(f"[refresh] forced: {q[:60]}")

    bridge.set_message_handler(_on_ws_message)

    try:
        if REPLAY_FILE:
            _replay_from_file(on_transcript, REPLAY_FILE)
        else:
            run_with_callback(on_transcript)
    except RuntimeError as exc:
        print(f"\n[ERROR] {exc}")
    except KeyboardInterrupt:
        pass
    finally:
        transcript_queue.put(None)
        consumer_thread.join(timeout=10)
        if log_file:
            log_file.close()
        print("\nSession ended.")


if __name__ == "__main__":
    main()

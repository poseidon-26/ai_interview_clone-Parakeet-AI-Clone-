"""
Audio capture + Whisper transcription — Phase 2.2 hardened.

Architecture:
  capture_thread  →  pcm_queue  →  transcribe_thread  →  on_transcript()

Key improvements over Phase 1:
  - Decoupled threads: capture never blocks on inference
  - Sliding window with configurable overlap (~1–2 s latency vs 5 s before)
  - Energy-based VAD gate to skip silent frames
  - Overlap deduplication so words aren't repeated across windows
  - ASR hallucination scrubbing (Whisper often outputs filler on silence)
  - AUDIO_PROFILE switch for YouTube vs Zoom/Meet call audio

Env knobs (all optional — sensible defaults work out of the box):
  WHISPER_MODEL     tiny.en (default) | base.en
  CHUNK_SECONDS     1.5
  OVERLAP_SECONDS   0.5
  VAD_ENABLED       true
  AUDIO_PROFILE     youtube | call
"""

import os
import queue
import re
import threading
import time
from collections import deque
from typing import Callable

import numpy as np
import pyaudio
from dotenv import load_dotenv
from faster_whisper import WhisperModel

load_dotenv()

# ── Configuration ────────────────────────────────────────────────────────────

TARGET_DEVICE_NAME = "BlackHole 2ch"
RATE = 16_000
FORMAT = pyaudio.paInt16
CHANNELS = 1
PYAUDIO_CHUNK = 1024          # ~64 ms per read — small keeps capture responsive

CHUNK_SECONDS   = float(os.getenv("CHUNK_SECONDS",   "1.5"))
OVERLAP_SECONDS = float(os.getenv("OVERLAP_SECONDS", "0.5"))
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "tiny.en")
VAD_ENABLED     = os.getenv("VAD_ENABLED", "true").lower() == "true"
AUDIO_PROFILE   = os.getenv("AUDIO_PROFILE", "youtube").lower()

# Profile presets ─────────────────────────────────────────────────────────────
# youtube: clean system audio, low compression → fast greedy decode
# call   : Zoom/Meet compression lowers amplitude + adds artifacts →
#          more sensitive VAD, slightly more careful beam search,
#          interview-domain initial prompt to steer Whisper vocabulary
_PROFILES: dict[str, dict] = {
    "youtube": {
        "_name": "youtube",
        "vad_threshold": 0.008,
        "beam_size": 1,
        "initial_prompt": None,
        "no_speech_threshold": 0.65,
    },
    "call": {
        "_name": "call",
        "vad_threshold": 0.005,
        "beam_size": 3,
        "initial_prompt": "Technical interview conversation about software engineering.",
        "no_speech_threshold": 0.55,
    },
}

# High-quality variants: slower but more accurate — activated adaptively
_HQ_PROFILES: dict[str, dict] = {
    "youtube": {
        "_name": "youtube_hq",
        "vad_threshold": 0.006,
        "beam_size": 5,
        "initial_prompt": "Software engineering interview conversation.",
        "no_speech_threshold": 0.55,
    },
    "call": {
        "_name": "call_hq",
        "vad_threshold": 0.003,
        "beam_size": 5,
        "initial_prompt": "Precise technical software engineering interview.",
        "no_speech_threshold": 0.42,
    },
}

# ── Adaptive ASR profile switching ────────────────────────────────────────────

ASR_ADAPTIVE_MODE = os.getenv("ASR_ADAPTIVE_MODE", "true").lower() == "true"

_QUALITY_WINDOW         = 5     # rolling window of recent quality scores
_QUALITY_LOW_THRESHOLD  = 0.40  # avg below this → switch to HQ
_QUALITY_HIGH_THRESHOLD = 0.60  # avg above this → switch back to balanced

# Module-level mutable profile: _transcribe_loop reads this each window so
# adaptive switches take effect without restarting the thread.
_current_profile: dict  = {}
_quality_history: deque = deque(maxlen=_QUALITY_WINDOW)
_base_profile_name: str = ""


def report_quality(score: float) -> None:
    """
    Called by the orchestrator after assessing each transcript chunk.
    Switches _current_profile in-place based on recent quality history.
    No-op if ASR_ADAPTIVE_MODE is False or profile not yet initialised.
    """
    if not ASR_ADAPTIVE_MODE or not _current_profile:
        return

    _quality_history.append(score)
    if len(_quality_history) < _QUALITY_WINDOW:
        return

    avg        = sum(_quality_history) / len(_quality_history)
    is_hq      = _current_profile.get("_name", "").endswith("_hq")
    base_name  = _base_profile_name

    if not is_hq and avg < _QUALITY_LOW_THRESHOLD:
        hq = _HQ_PROFILES.get(base_name)
        if hq:
            _current_profile.update(hq)
            print(f"[ASR] Quality low ({avg:.2f}) → switched to HQ profile")

    elif is_hq and avg > _QUALITY_HIGH_THRESHOLD:
        base = _PROFILES.get(base_name)
        if base:
            _current_profile.update(base)
            print(f"[ASR] Quality recovered ({avg:.2f}) → switched back to balanced profile")

# ASR hallucination patterns Whisper produces on near-silence ─────────────────
_HALLUCINATION_RE = re.compile(
    r"^[\s.,!?()\[\]\"'\-]+$"           # only punctuation / whitespace
    r"|thanks?\s+for\s+watching"
    r"|please\s+subscribe"
    r"|like\s+and\s+subscribe"
    r"|transcribed\s+by"
    r"|subtitles\s+by"
    r"|\[music\]|\[applause\]|\[laughter\]",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(r"\b(um+|uh+|hmm+|hm+|ah+|er+)\b\s*", re.IGNORECASE)


# ── Pure helpers ─────────────────────────────────────────────────────────────

def find_device_index(p: pyaudio.PyAudio, name: str):
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if name.lower() in info["name"].lower() and info["maxInputChannels"] > 0:
            return i, info
    return None, None


def _energy_vad(audio: np.ndarray, threshold: float) -> bool:
    return float(np.sqrt(np.mean(audio ** 2))) > threshold


def _clean_text(text: str) -> str:
    text = text.strip()
    if not text or _HALLUCINATION_RE.search(text):
        return ""
    text = _FILLER_RE.sub(" ", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if not re.search(r"[a-zA-Z]{2,}", text):
        return ""
    return text


def _dedup_prefix(prev: str, curr: str, max_words: int = 8) -> str:
    """
    Strip words from the front of `curr` that duplicate a trailing suffix of
    `prev`.  This removes repeated words that appear in both the overlap region
    of the previous window and the start of the current one.
    """
    if not prev or not curr:
        return curr
    pw, cw = prev.split(), curr.split()
    for n in range(min(len(pw), len(cw), max_words), 0, -1):
        if pw[-n:] == cw[:n]:
            rest = " ".join(cw[n:]).strip()
            return rest if rest else curr   # never return empty
    return curr


def _load_model() -> WhisperModel:
    print(f"Loading Whisper {WHISPER_MODEL_SIZE} model...")
    model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    print("Whisper model ready.\n")
    return model


# ── Worker threads ────────────────────────────────────────────────────────────

def _capture_loop(
    stream: pyaudio.Stream,
    pcm_queue: "queue.Queue[bytes | None]",
    stop_event: threading.Event,
) -> None:
    """Read raw PCM from PyAudio and push to queue.  Never blocks on inference."""
    try:
        while not stop_event.is_set():
            data = stream.read(PYAUDIO_CHUNK, exception_on_overflow=False)
            try:
                pcm_queue.put_nowait(data)
            except queue.Full:
                pass    # drop frame rather than stall capture
    except OSError:
        pass
    finally:
        stop_event.set()    # wake main thread if device disconnects


def _transcribe_loop(
    model: WhisperModel,
    pcm_queue: "queue.Queue[bytes | None]",
    on_transcript: Callable[[str], None],
    stop_event: threading.Event,
) -> None:
    """
    Sliding-window Whisper inference.

    Accumulates PCM frames until window_samples are ready, runs Whisper, then
    keeps only the overlap tail before waiting for the next step worth of audio.
    If the queue builds up (inference slower than capture), the oldest audio is
    discarded so latency stays bounded.
    """
    window_samples = int(CHUNK_SECONDS * RATE)
    step_samples   = int((CHUNK_SECONDS - OVERLAP_SECONDS) * RATE)

    chunks: list[np.ndarray] = []
    chunk_total = 0
    last_text = ""

    while not stop_event.is_set():
        try:
            raw = pcm_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if raw is None:
            break

        chunk_f32 = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        chunks.append(chunk_f32)
        chunk_total += len(chunk_f32)

        if chunk_total < window_samples:
            continue

        audio = np.concatenate(chunks)

        # If we've fallen behind, skip to the most recent window
        if len(audio) > window_samples + step_samples:
            audio = audio[-window_samples:]

        window = audio[:window_samples]

        # Slide accumulator forward: keep only the overlap tail
        remaining = audio[step_samples:]
        chunks = [remaining]
        chunk_total = len(remaining)

        # VAD gate — skip silent windows entirely
        if VAD_ENABLED and not _energy_vad(window, _current_profile["vad_threshold"]):
            continue

        # Whisper inference — reads _current_profile each window for adaptive switching
        segments, _ = model.transcribe(
            window,
            language="en",
            beam_size=_current_profile["beam_size"],
            temperature=0.0,
            initial_prompt=_current_profile.get("initial_prompt"),
            no_speech_threshold=_current_profile["no_speech_threshold"],
            condition_on_previous_text=False,   # independent windows; prevents runaway context
            vad_filter=False,                   # we gate with energy VAD above
        )
        raw_text = " ".join(s.text.strip() for s in segments).strip()
        text = _clean_text(raw_text)

        if not text:
            continue

        # Strip words duplicated from the previous window's overlap
        text = _dedup_prefix(last_text, text)
        if not text:
            continue

        last_text = text
        on_transcript(text)


# ── Public API ────────────────────────────────────────────────────────────────

def run_with_callback(
    on_transcript: Callable[[str], None],
    device_name: str = TARGET_DEVICE_NAME,
) -> None:
    """
    Open the BlackHole audio stream and call on_transcript(text) for each
    recognised speech chunk.  Blocks the calling thread until KeyboardInterrupt
    or the audio device disconnects; cleans up all threads and PyAudio on exit.
    """
    global _current_profile, _base_profile_name, _quality_history

    base = _PROFILES.get(AUDIO_PROFILE, _PROFILES["youtube"])
    _current_profile.update(base)
    _base_profile_name = AUDIO_PROFILE
    _quality_history.clear()

    model = _load_model()

    p = pyaudio.PyAudio()
    device_index, device_info = find_device_index(p, device_name)
    if device_index is None:
        p.terminate()
        raise RuntimeError(
            f"Audio device '{device_name}' not found.  Run check_audio.py."
        )

    adaptive_tag = "adaptive" if ASR_ADAPTIVE_MODE else "fixed"
    print(
        f"Profile: {AUDIO_PROFILE} ({adaptive_tag})  |  model: {WHISPER_MODEL_SIZE}  |  "
        f"window: {CHUNK_SECONDS}s  |  overlap: {OVERLAP_SECONDS}s  |  "
        f"VAD: {'on' if VAD_ENABLED else 'off'}"
    )
    print(f"Capturing from [{device_index}]: {device_info['name']}")
    print("Listening... (Ctrl+C to stop)\n")

    stream = p.open(
        format=FORMAT,
        channels=CHANNELS,
        rate=RATE,
        input=True,
        input_device_index=device_index,
        frames_per_buffer=PYAUDIO_CHUNK,
    )

    stop_event = threading.Event()
    # Queue sized for ~10 s of audio at 64 ms/chunk; enough headroom for a slow
    # inference pass without unbounded memory growth.
    pcm_queue: queue.Queue = queue.Queue(maxsize=160)

    capture_thread = threading.Thread(
        target=_capture_loop,
        args=(stream, pcm_queue, stop_event),
        daemon=True,
        name="capture",
    )
    transcribe_thread = threading.Thread(
        target=_transcribe_loop,
        args=(model, pcm_queue, on_transcript, stop_event),
        daemon=True,
        name="transcribe",
    )
    capture_thread.start()
    transcribe_thread.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        try:
            pcm_queue.put_nowait(None)  # unblock transcription thread if waiting
        except queue.Full:
            pass
        capture_thread.join(timeout=2)
        transcribe_thread.join(timeout=5)
        stream.stop_stream()
        stream.close()
        p.terminate()


def main() -> None:
    def _print(text: str) -> None:
        print(f"[Transcript] {text}")

    try:
        run_with_callback(_print)
    except RuntimeError as e:
        print(f"ERROR: {e}")
    print("\nStopped.")


if __name__ == "__main__":
    main()

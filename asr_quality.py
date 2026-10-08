"""
ASR transcript quality heuristics.

assess(text)       → float  quality score 0.0 (garbage) to 1.0 (clean)
is_uncertain(text) → bool   True when quality is below the uncertainty gate

Design contract:
  Input text has ALREADY been through transcriber._clean_text() — filler words
  stripped, hallucination patterns removed, minimum 2-character alpha content
  enforced.  We focus here on higher-level artifact patterns that slip through:
    - Whisper stutter repetition:  "the the the the algorithm the the"
    - Whisper looping artifacts:   "hash hash hash hash hash hash"
    - Insufficient semantic content: 1–2 real words, no question/statement
"""

import os
from collections import Counter

# Score below which we treat the transcript as too noisy to pass to the LLM.
# Range 0.0–1.0.  Lower = more permissive (fewer gates).  Default 0.35.
UNCERTAIN_THRESHOLD = float(os.getenv("ASR_QUALITY_THRESHOLD", "0.35"))

_REP_WORD_MIN_LEN = 3    # include short words (the, is, it, …) in repetition check
_REPEAT_LIMIT     = 3    # appearances needed to trigger repetition penalty
_MIN_WORDS        = 3    # meaningful words required to attempt an answer


# ── Signal functions ──────────────────────────────────────────────────────────

def _alpha_ratio(text: str) -> float:
    """Fraction of alphabetic characters.  Low → gibberish."""
    alpha = sum(1 for c in text if c.isalpha())
    return alpha / len(text) if text else 0.0


def _has_consecutive_repeat(words: list[str]) -> bool:
    """True if any word of length > 2 immediately follows itself."""
    for i in range(1, len(words)):
        if len(words[i]) > 2 and words[i].lower() == words[i - 1].lower():
            return True
    return False


def _dominant_word_ratio(words: list[str]) -> float:
    """
    Fraction of the transcript occupied by the single most-repeated word.
    High value (> 0.5) indicates a looping artifact.
    """
    if not words:
        return 0.0
    counts = Counter(w.lower() for w in words if len(w) >= _REP_WORD_MIN_LEN)
    if not counts:
        return 0.0
    return max(counts.values()) / len(words)


def _length_score(words: list[str]) -> float:
    """Reward transcripts with enough distinct words to form a statement."""
    n = len(words)
    if n >= _MIN_WORDS + 2:
        return 1.0
    if n >= _MIN_WORDS:
        return 0.7
    if n == 2:
        return 0.4
    return 0.1


# ── Public API ────────────────────────────────────────────────────────────────

def assess(text: str) -> float:
    """
    Return a quality score 0.0–1.0 (higher = cleaner, more confident).

    Hard cap at 0.30 when obvious Whisper artifacts are present:
      - Any word immediately repeats (stutter)
      - A single word dominates > 50 % of the tokens (looping)

    Otherwise uses a weighted combination:
      0.40 × alpha_ratio       — non-gibberish characters
      0.30 × (1 − dom_ratio)  — absence of looping artifact
      0.30 × length_score     — sufficient meaningful content
    """
    if not text or not text.strip():
        return 0.0

    words = text.split()

    # ── Hard cap for obvious Whisper artifacts ────────────────────────────────
    dom = _dominant_word_ratio(words)
    if _has_consecutive_repeat(words) or dom > 0.50:
        # Still allow alpha_ratio to distinguish total garbage from looped text
        return round(min(0.28, _alpha_ratio(text) * 0.35), 3)

    # ── Normal weighted score ─────────────────────────────────────────────────
    alpha  = _alpha_ratio(text)
    length = _length_score(words)

    score = (
        0.40 * alpha
        + 0.30 * (1.0 - dom)
        + 0.30 * length
    )
    return round(max(0.0, min(1.0, score)), 3)


def is_uncertain(text: str) -> bool:
    """True when transcript quality is below UNCERTAIN_THRESHOLD."""
    return assess(text) < UNCERTAIN_THRESHOLD

"""
Interview prompt templates and lightweight question-type classifier.

classify_mode(text) → "behavioral" | "programming"
build_messages(context, new_chunk, mode="auto") → OpenAI messages list

Mode is controlled by INTERVIEW_MODE env var (auto | behavioral | programming).
Answer format is controlled by ANSWER_STYLE (strict_bullets | legacy).
"""

import os
import re
from typing import Literal

# ── Interview mode classifier ─────────────────────────────────────────────────

INTERVIEW_MODE = os.getenv("INTERVIEW_MODE", "auto").lower()

_CODING_RE = re.compile(
    # No trailing \b — allows prefix matches (sort → sorting, implement → implementing)
    r"\b(algorithm|implement|code|function|class|method|complexit|"
    r"O\s*\(|[Bb]ig[\s\-]*O|log\s*n|data\s*struct|array|tree|graph|hash|sort|"
    r"search|recursion|recursive|optimi[sz]|pointer|binary|stack|"
    r"queue|linked\s*list|runtime|iterate|loop|debug|write\s+a|"
    r"design\s+a|pseudocode|time\s+complex|space\s+complex|"
    r"backtrack|dynamic\s+program|memoiz|two[\s\-]pointer|"
    r"sliding\s+window|BFS|DFS|trie|heap|palindrome|substring|"
    r"integer|string|reverse|rotate|merge|partition)",
    re.IGNORECASE,
)


def classify_mode(text: str) -> Literal["behavioral", "programming"]:
    """
    Lightweight classifier: returns 'programming' on coding keyword signals,
    'behavioral' otherwise.  Respects INTERVIEW_MODE override.
    """
    if INTERVIEW_MODE == "behavioral":
        return "behavioral"
    if INTERVIEW_MODE == "programming":
        return "programming"
    return "programming" if _CODING_RE.search(text) else "behavioral"


# ── System prompts ────────────────────────────────────────────────────────────

ANSWER_STYLE = os.getenv("ANSWER_STYLE", "strict_bullets")

_BEHAVIORAL_SYSTEM = """\
You are a real-time interview answer coach. Output ONLY bullet points — never prose, never narration.

STRICT RULES (no exceptions):
• 2–3 bullets maximum. Each bullet ≤ 20 words.
• Bullets must be directly speakable: first person, active voice, concrete and specific.
• NEVER restate or summarise the question.
• NEVER open with "Sure", "Great question", "Of course", "Certainly", "Absolutely", or any filler.
• No trailing explanation, no preamble, no meta-commentary. Pure answer substance only.
• If the latest transcript is ambient speech with no clear question, output nothing at all.

EXAMPLE OUTPUT (format only — do not copy this content):
• Led backend migration to microservices, cutting deploy time by 40 percent.
• Coordinated across three teams under a two-week hard deadline.
• Used feature flags for iterative rollout to minimise production risk.
"""

_PROGRAMMING_SYSTEM = """\
You are a real-time coding interview coach. Output ONLY the four structured sections below — nothing else.

REQUIRED FORMAT:
Approach:
• <high-level idea — 1–2 bullets, ≤ 15 words each>

Complexity: O(...) time, O(...) space

Code:
```
<3–10 line snippet or clean pseudocode — compact>
```

Edge cases:
• <1–2 bullets>

RULES (no exceptions):
• No preamble, no prose outside these four sections, no trailing explanation.
• Pseudocode is preferred over verbose code if it's more compact.
• NEVER restate the question.
• Keep total output under 200 tokens.
"""

# Legacy format kept for ANSWER_STYLE != strict_bullets
_LEGACY_SYSTEM = """\
You are a real-time technical interview assistant. Provide a concise, technically accurate
answer in 2-4 sentences. No preamble. No filler. Output only the substance.
"""


def _get_system(mode: str) -> str:
    if ANSWER_STYLE != "strict_bullets":
        return _LEGACY_SYSTEM
    return _PROGRAMMING_SYSTEM if mode == "programming" else _BEHAVIORAL_SYSTEM


# ── Message builder ───────────────────────────────────────────────────────────

def build_messages(
    context: list[str],
    new_chunk: str,
    mode: str = "auto",
) -> list[dict]:
    """
    Return an OpenAI-compatible messages list.

    context   — previous transcript chunks (oldest → newest)
    new_chunk — the latest transcript chunk that triggered this call
    mode      — "behavioral" | "programming" | "auto" (auto-detect from new_chunk)
    """
    if mode == "auto":
        mode = classify_mode(new_chunk)

    system       = _get_system(mode)
    history_text = "\n".join(context) if context else "(no prior context)"

    user_content = (
        f"[Conversation so far]\n{history_text}\n\n"
        f"[Latest heard]\n{new_chunk}\n\n"
        "Give your answer now. Follow the format exactly. No extras."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

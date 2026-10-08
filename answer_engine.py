import os
import time
import logging
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from prompts import build_messages

load_dotenv()
logger = logging.getLogger(__name__)


def _is_retryable(exc: Exception) -> bool:
    """Return False for fatal API errors that must never be retried."""
    try:
        from openai import AuthenticationError, RateLimitError
    except ImportError:
        return True
    if isinstance(exc, AuthenticationError):
        return False
    if isinstance(exc, RateLimitError) and "insufficient_quota" in str(exc).lower():
        return False
    return True


@dataclass
class Answer:
    answer_text: str
    confidence_hint: str  # "high" | "medium" | "low"
    latency_ms: float


class AnswerEngine:
    """
    Wraps a local (llama-cpp-python) or API (OpenAI / Ollama) backend.

    Local path  — set USE_LOCAL_MODEL=true and LOCAL_MODEL_PATH=/path/to/model.gguf
    API path    — set OPENAI_API_KEY (OpenAI) or OPENAI_BASE_URL (Ollama, no key needed)
    """

    def __init__(self) -> None:
        self.debounce_seconds = float(os.getenv("DEBOUNCE_SECONDS", "3.0"))
        self._last_called: float = 0.0
        self._last_error_at: float = 0.0
        self._last_error_kind: str = ""
        self._paused: bool = False
        self._pause_reason: str = ""
        self._last_status_print: float = 0.0
        self._backend_type, *self._backend_args = self._init_backend()

    # ------------------------------------------------------------------
    # Backend initialisation
    # ------------------------------------------------------------------

    def _init_backend(self) -> tuple:
        if os.getenv("USE_LOCAL_MODEL", "").lower() == "true":
            model_path = os.getenv("LOCAL_MODEL_PATH", "")
            if model_path:
                try:
                    from llama_cpp import Llama  # type: ignore

                    llm = Llama(model_path=model_path, n_ctx=2048, n_gpu_layers=-1, verbose=False)
                    logger.info("Local llama-cpp model loaded: %s", model_path)
                    return ("local", llm)
                except Exception as exc:
                    logger.warning("Local model unavailable (%s). Falling back to API.", exc)
            else:
                logger.warning("USE_LOCAL_MODEL=true but LOCAL_MODEL_PATH not set. Falling back to API.")

        from openai import OpenAI

        base_url = os.getenv("OPENAI_BASE_URL") or None  # None → default OpenAI endpoint
        api_key = os.getenv("OPENAI_API_KEY", "no-key")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

        client = OpenAI(api_key=api_key, base_url=base_url)
        backend_label = f"Ollama ({base_url})" if base_url else f"OpenAI ({model})"
        logger.info("API backend: %s", backend_label)
        print(f"[AnswerEngine] Using {backend_label}")
        return ("openai", client, model)

    # ------------------------------------------------------------------
    # Raw LLM calls
    # ------------------------------------------------------------------

    def _call_local(self, llm, messages: list[dict]) -> str:
        prompt = "\n".join(
            f"{'SYSTEM' if m['role'] == 'system' else m['role'].upper()}: {m['content']}"
            for m in messages
        )
        prompt += "\nASSISTANT:"
        result = llm(prompt, max_tokens=300, stop=["\nUSER:", "\nHUMAN:"])
        return result["choices"][0]["text"].strip()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _call_openai(self, client, model: str, messages: list[dict]) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=300,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ask(self, context: list[str], new_chunk: str) -> Optional[Answer]:
        """
        Generate a suggestion for `new_chunk` given rolling `context`.

        Returns None if the debounce period has not elapsed since the last call.
        Returns None on unrecoverable backend error (error is logged).
        """
        now = time.time()
        if self._paused:
            if now - self._last_status_print >= 30.0:
                print(f"[LLM paused: {self._pause_reason}]")
                self._last_status_print = now
            return None
        if now - self._last_called < self.debounce_seconds:
            return None
        self._last_called = now

        messages = build_messages(context, new_chunk)
        t0 = time.time()

        try:
            if self._backend_type == "local":
                text = self._call_local(self._backend_args[0], messages)
            else:
                text = self._call_openai(self._backend_args[0], self._backend_args[1], messages)
        except Exception as exc:
            reason = self._classify_fatal(exc)
            if reason:
                self._enter_pause(reason)
            else:
                self._log_brief_error("transient", exc)
            return None

        latency_ms = (time.time() - t0) * 1000

        if len(text) > 120 and latency_ms < 8000:
            confidence = "high"
        elif len(text) > 40:
            confidence = "medium"
        else:
            confidence = "low"

        return Answer(answer_text=text, confidence_hint=confidence, latency_ms=latency_ms)

    def _classify_fatal(self, exc: Exception) -> str:
        """Return a human-readable reason string for fatal API errors, or ''."""
        try:
            from openai import AuthenticationError, RateLimitError
        except ImportError:
            return ""
        if isinstance(exc, AuthenticationError):
            return "auth"
        if isinstance(exc, RateLimitError) and "insufficient_quota" in str(exc).lower():
            return "quota"
        return ""

    def _try_init_local(self) -> Optional[tuple]:
        model_path = os.getenv("LOCAL_MODEL_PATH", "")
        if not model_path:
            return None
        try:
            from llama_cpp import Llama  # type: ignore
            llm = Llama(model_path=model_path, n_ctx=2048, n_gpu_layers=-1, verbose=False)
            return ("local", llm)
        except Exception as exc:
            logger.warning("Auto-switch to local model failed: %s", exc)
            return None

    def _enter_pause(self, reason: str) -> None:
        if os.getenv("USE_LOCAL_MODEL", "").lower() == "true":
            local = self._try_init_local()
            if local:
                self._backend_type, *self._backend_args = local
                print(f"[AnswerEngine] Cloud failed ({reason}). Auto-switched to local model.")
                return
        self._paused = True
        self._pause_reason = reason
        self._last_status_print = 0.0
        print(f"[LLM paused: {reason}]")

    def _log_brief_error(self, kind: str, exc: Exception) -> None:
        """
        Prevent terminal spam by rate-limiting repeated backend errors.
        """
        now = time.time()
        should_log = kind != self._last_error_kind or (now - self._last_error_at) >= 15
        if should_log:
            logger.error("LLM unavailable (%s): %s", kind, exc)
            self._last_error_kind = kind
            self._last_error_at = now

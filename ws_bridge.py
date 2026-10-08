"""
Phase 3 WebSocket bridge.

Starts a ws:// server in a background daemon thread and lets any thread push
transcript + assistant-hint events to all connected Electron clients.

Usage:
    from ws_bridge import WsBridge
    bridge = WsBridge()
    bridge.start()                  # blocks briefly until port is bound
    bridge.send_transcript(text)
    bridge.send_hint(answer)        # answer: Answer dataclass from answer_engine

Port: WS_PORT env var (default 8765)
"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Optional, Set

logger = logging.getLogger(__name__)

WS_PORT = int(os.getenv("WS_PORT", "8765"))


class WsBridge:
    def __init__(self, port: int = WS_PORT) -> None:
        self._port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: Set = set()
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._message_handler = None

    # ── public API (callable from any thread) ─────────────────────────────────

    def start(self) -> None:
        """Spawn daemon thread with WS server; returns once port is bound (max 3 s)."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-bridge")
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            logger.warning("WS bridge did not bind within 3 s")

    def send_transcript(self, text: str) -> None:
        self._broadcast({"type": "transcript", "text": text, "ts": time.time()})

    def send_hint(self, answer: object) -> None:
        self._broadcast({
            "type": "hint",
            "text": answer.answer_text,
            "confidence": answer.confidence_hint,
            "latency_ms": round(answer.latency_ms, 1),
            "ts": time.time(),
        })

    def send_freeze(self, seconds: float) -> None:
        self._broadcast({"type": "freeze", "seconds": seconds, "ts": time.time()})

    def set_message_handler(self, callback) -> None:
        """Register a callback(msg: dict) for messages sent by any connected client."""
        self._message_handler = callback

    # ── internal ──────────────────────────────────────────────────────────────

    def _broadcast(self, msg: dict) -> None:
        if self._loop is None or not self._clients:
            return
        data = json.dumps(msg)
        asyncio.run_coroutine_threadsafe(self._async_broadcast(data), self._loop)

    async def _async_broadcast(self, data: str) -> None:
        dead: Set = set()
        for ws in list(self._clients):
            try:
                await ws.send(data)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("websockets not installed — run: pip install websockets")
            self._ready.set()
            return

        async def handler(websocket):
            self._clients.add(websocket)
            try:
                async for raw in websocket:
                    if self._message_handler:
                        try:
                            self._message_handler(json.loads(raw))
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                self._clients.discard(websocket)

        async with websockets.serve(handler, "127.0.0.1", self._port):
            self._ready.set()
            print(f"[WsBridge] listening on ws://127.0.0.1:{self._port}")
            await asyncio.Future()  # run until daemon thread is killed

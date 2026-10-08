Phase 1 (Audio & Transcription)
Objective: Enable the AI to "hear" the system audio on a Mac using BlackHole and transcribe it in real-time.

Architecture:

Environment: Use a Python 3.11 virtual environment (.venv).

Dependencies: pyaudio, faster-whisper, python-dotenv.

Task 1 (Diagnostic): Create check_audio.py to list all PyAudio device indices.

Task 2 (The Ears): Create transcriber.py.

It must use pyaudio to open a stream from the "BlackHole 2ch" device.

It must pass that stream to faster-whisper (Tiny/Base model for speed).

It must print transcription to the console immediately.

Execution: Provide a single terminal command to run the transcriber.

3. Trigger the Coder (Talk to Claude in Cursor):

Open the Claude Code Sidebar (click that orange Spark icon ✱).

Type this into the Claude chat:

"Read the _PLAN.md file in my root directory. Implement Task 1 and Task 2. Create the necessary files, install any missing libraries in my .venv, and make sure it works on a Mac with BlackHole. Do not ask me for permission for each file; just build the foundation."

Phase 2 (Live AI Answer Engine)
Objective: Convert live transcription into concise interview answers in real-time, locally first, with optional cloud fallback.

Architecture:

Input Source: Reuse transcript chunks from transcriber.py (confirmed audio source: BlackHole 2ch, device index 0).

LLM Layer: Start with a local model path using transformers (or llama-cpp-python if simpler), and keep an OpenAI-compatible fallback behind .env flags.

Output: Print a clean "Assistant Suggestion" stream to terminal with low latency and no repeated lines.

Environment: Continue using Python 3.11 virtual environment (.venv) and run commands with python3.

Dependencies: transformers, torch, sentencepiece, accelerate, openai, tenacity.

Task 1 (Prompt Brain): Create prompts.py.

It must contain:
- A system prompt for interview assistance (concise, technically accurate, no hallucinated claims).
- A formatter that takes rolling transcript context and generates a compact prompt payload.

Task 2 (LLM Adapter): Create answer_engine.py.

It must:
- Read transcript text chunks from stdin or a callable interface.
- Generate responses with debounce/throttle (avoid spamming every token).
- Support local model first; fallback to API model if local model fails.
- Return structured output: answer_text, confidence_hint, latency_ms.

Task 3 (Orchestration): Create run_live_assistant.py.

It must:
- Connect transcriber output to answer_engine in a loop.
- Keep a rolling context window (last ~30-60 seconds equivalent text).
- Print only materially new suggestions.
- Handle Ctrl+C gracefully.

Execution: Provide one terminal command to run the full Phase 2 pipeline.

4. Trigger the Coder (Talk to Claude in Cursor):

Open the Claude Code Sidebar (click the orange Spark icon ✱).

Type this into Claude chat:

"Read _PLAN.md and implement Phase 2 completely. Reuse the Phase 1 transcriber output, build prompts.py, answer_engine.py, and run_live_assistant.py, install missing dependencies in .venv, and make it run with python3 on macOS. Use BlackHole 2ch (device index 0) as default input. Keep code modular, production-clean, and runnable end-to-end without asking me for per-file approval."
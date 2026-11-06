# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mumble — hotkey-activated speech-to-text for Windows. Single-file Python app (`mumble.py`) that records audio, transcribes via faster-whisper, and pastes the result into the active window.

## Install & Run

```bash
pip install .                  # install from local clone
pip install git+https://github.com/thompler/mumble.git  # install from GitHub
mumble                         # run (console, installed)
mumble-gui                     # run (no console window, installed)
python mumble.py               # run directly from repo (dev mode)
```

> **Note for Claude Code:** The `mumble` and `mumble-gui` commands are not on the bash PATH. To start mumble from within this repo, use `python mumble.py`.

Build system is hatchling (configured in `pyproject.toml`). Tests live in `tests/` and run via `python -m pytest`. Linting is not configured.

## Architecture

Everything lives in `mumble.py` (~400 lines). Key flow:

1. **Config** — `_find_config()` searches `~/.mumble/mumble.toml` then script-dir then falls back to `_DEFAULTS` dict. Dev mode (pyproject.toml next to script) keeps logs local; installed mode uses `~/.mumble/`. `mumble.toml` in the repo root is gitignored personal config; `mumble.toml.example` is the tracked template.
2. **Audio** — `sounddevice.InputStream` records to `audio_chunks` list. Targets Anker C200 mic by name substring, falls back to system default. In hold mode, a `_watch_release` thread polls `GetAsyncKeyState` on the main key and calls `_stop_and_transcribe()` when it lifts after `HOLD_TAP_SECONDS`.
3. **Transcription** — `faster_whisper.WhisperModel` (small.en, CPU, int8). Audio is resampled to 16kHz via `scipy.signal.resample_poly` if needed.
4. **Post-processing** — Digit collapse (`1 2 3 4` → `1234`), double-space collapse. Voice commands: the trigger word (`claude`, plus homophone aliases) + one word becomes a slash command (`/clear`); "show help" opens the README. Applied in `stop_recording_and_transcribe()`.
5. **Paste** — Copies to clipboard via `pyperclip`, then simulates Ctrl+V via `pynput`.
6. **Tray** — `pystray` icon (green=idle, red=recording) with hotkey info and quit menu.

Hotkeys are registered globally through the Win32 `RegisterHotKey` API via ctypes (`HotkeyManager`, with `MOD_NOREPEAT`); callbacks run on its message-loop thread. Modifier-only chords such as `ctrl+alt+win` cannot be expressed with `RegisterHotKey` (Windows accepts `VK_LWIN` as a main key but never delivers it), so `HotkeyManager` detects those by polling `GetAsyncKeyState` and firing on the rising edge. The modifier-release wait before pasting reads live key state via `GetAsyncKeyState`. In hold mode the release watcher also reads live key state via `GetAsyncKeyState`. All recording/transcription runs on background threads; `shutdown_event` coordinates clean exit. A recording auto-cancels after `audio.max_duration` seconds (default 90) via a `threading.Timer`. A `transcribing` flag guards against starting a new recording while a transcription is still in flight; `decide_toggle_action()` encodes the start/stop/ignore state machine.

## Model usage

- **Planning (Opus):** Use Opus for writing implementation plans. Plans must be self-contained and explicit enough that Sonnet or Haiku can execute them without judgment calls — every step includes exact file paths, complete before/after code, and exact commands with expected output. No "implement X" steps; show the code.
- **Implementation (Sonnet or Haiku):** Use Sonnet for multi-file or integration tasks. Use Haiku for single-file mechanical changes with a clear spec. Implementers follow the plan exactly and do not make architectural decisions.
- **Reviews (Opus):** Use Opus for spec-compliance and code-quality review passes.

## Key decisions

- Spoken punctuation replacement was tried and removed — Whisper inserts commas around spoken words like "period", producing ugly output like `,:,`.
- Windows-only: uses `winsound` for beeps, `pystray` for tray, Win32 `RegisterHotKey` for global hotkeys.
- The `keyboard` module was replaced with `RegisterHotKey`: its low-level hook could be silently removed by Windows under load and its in-process pressed-key table could go stale, which left every hotkey dead until restart. It also re-fired on key auto-repeat.
- Concurrency: `recording`, `transcribing`, and `audio_chunks` are module globals shared across the hotkey, audio-callback, transcription, timeout, and release-watcher threads. `_stop_and_transcribe()` begins with `if not recording`, so the watcher and the hotkey thread cannot both stop the same recording. Rather than locking, the `transcribing` flag makes the record hotkey a no-op while a transcription runs, preventing a new recording from clearing `audio_chunks` mid-transcription.

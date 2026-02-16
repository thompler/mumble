# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mumble — hotkey-activated speech-to-text for Windows. Single-file Python app (`mumble.py`) that records audio, transcribes via faster-whisper, and pastes the result into the active window.

## Install & Run

```bash
pip install .                  # install from local clone
pip install git+https://github.com/thompler/mumble.git  # install from GitHub
mumble                         # run (console)
mumble-gui                     # run (no console window)
```

Build system is hatchling (configured in `pyproject.toml`). No tests or linting are configured.

## Architecture

Everything lives in `mumble.py` (~400 lines). Key flow:

1. **Config** — `_find_config()` searches `~/.mumble/mumble.toml` then script-dir then falls back to `_DEFAULTS` dict. Dev mode (mumble.toml next to script) keeps logs local; installed mode uses `~/.mumble/`.
2. **Audio** — `sounddevice.InputStream` records to `audio_chunks` list. Targets Anker C200 mic by name substring, falls back to system default.
3. **Transcription** — `faster_whisper.WhisperModel` (small.en, CPU, int8). Audio is resampled to 16kHz via `scipy.signal.resample_poly` if needed.
4. **Post-processing** — Digit collapse (`1 2 3 4` → `1234`), double-space collapse. Applied in `stop_recording_and_transcribe()`.
5. **Paste** — Copies to clipboard via `pyperclip`, then simulates Ctrl+V via `pynput`.
6. **Tray** — `pystray` icon (green=idle, red=recording) with hotkey info and quit menu.

Hotkeys are registered globally via the `keyboard` module. All recording/transcription runs on background threads; `shutdown_event` coordinates clean exit.

## Key decisions

- Spoken punctuation replacement was tried and removed — Whisper inserts commas around spoken words like "period", producing ugly output like `,:,`.
- Windows-only: uses `winsound` for beeps, `pystray` for tray, `keyboard` for global hotkeys.

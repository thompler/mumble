# Mumble

Hotkey-activated speech-to-text for Windows. Press a key combo, talk, and the transcription is pasted directly into whatever window has focus.

Runs silently in the system tray with no console window.

## Hotkeys

| Shortcut | Action |
|---|---|
| `Ctrl+Alt+Space` | Start / stop recording |
| `Ctrl+Alt+X` | Cancel recording (discard audio) |
| `Ctrl+Alt+V` | Re-paste last transcription |
| `Ctrl+Alt+Q` | Quit |

## How it works

1. **Record** — Audio is captured from your microphone when you press the hotkey. The tray icon turns red while recording.
2. **Transcribe** — When you press the hotkey again, the audio is resampled to 16 kHz and fed to [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`small.en` model, CPU, int8).
3. **Paste** — The transcribed text is copied to your clipboard and pasted into the active window via `Ctrl+V`.

### Post-processing

Before pasting, Mumble cleans up the transcription:

- **Digit collapse** — Spaces between individual digits are removed (`1 2 3 4` becomes `1234`), useful for phone numbers, zip codes, etc.
- **Double-space collapse** — Runs of multiple spaces are reduced to a single space.

## Requirements

- Windows (uses `winsound` for audio feedback and `pystray` for the system tray)
- Python 3.11+

## Installation

Install directly from GitHub:

```
pip install git+https://github.com/thompler/mumble.git
```

Or install from a local clone:

```
git clone https://github.com/thompler/mumble.git
cd mumble
pip install .
```

The first run will download the Whisper model (~500 MB).

### Microphone

By default, Mumble looks for an **Anker C200** mic by name. If not found, it falls back to the system default input device. To change the target mic, edit `device_name` in your config file (see [Configuration](#configuration)).

## Running

### Console (with log output)

```
mumble
```

### Background (no console window)

```
mumble-gui
```

### Start on login

```
mumble --install-startup
```

This creates a shortcut to `mumble-gui` in your Windows Startup folder. To remove it:

```
mumble --remove-startup
```

## System tray

Mumble adds a microphone icon to the system tray:

- **Green** — Idle, ready to record
- **Red** — Recording in progress

Right-click the icon for options:

- **Hotkeys** — Shows the current key bindings
- **Open Log** — Opens `mumble.log` in VS Code
- **Quit** — Shuts down Mumble

## Logging

Logs are written to `mumble.log`. When installed via pip, logs go to `~/.mumble/mumble.log`. When running from a source checkout (where `mumble.toml` sits next to `mumble.py`), logs go to that same directory. The log rotates at 1 MB with one backup file.

## Configuration

Settings live in `mumble.toml`. Mumble checks for config in this order:

1. `~/.mumble/mumble.toml` (user config — recommended for pip installs)
2. Same directory as `mumble.py` (dev / source checkout mode)
3. Built-in defaults

To customize, create `~/.mumble/mumble.toml`:

```toml
[hotkeys]
record = "ctrl+alt+space"
cancel = "ctrl+alt+x"
quit = "ctrl+alt+q"
repaste = "ctrl+alt+v"

[whisper]
model = "small.en"
sample_rate = 16000

[audio]
device_name = "C200"
```

| Key | Default | Description |
|---|---|---|
| `hotkeys.record` | `ctrl+alt+space` | Toggle recording |
| `hotkeys.cancel` | `ctrl+alt+x` | Cancel recording |
| `hotkeys.quit` | `ctrl+alt+q` | Quit |
| `hotkeys.repaste` | `ctrl+alt+v` | Re-paste last result |
| `whisper.model` | `small.en` | Whisper model size |
| `whisper.sample_rate` | `16000` | Target sample rate for Whisper |
| `audio.device_name` | `C200` | Mic name substring to search for |

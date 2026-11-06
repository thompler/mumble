"""
Mumble — Hotkey-activated speech-to-text that pastes into the active window.

Ctrl+Alt+Space  — start/stop recording
Ctrl+Alt+X      — cancel recording
Ctrl+Alt+V      — re-paste last transcription
Ctrl+Alt+Q      — quit

Runs silently in the background (no console window needed).
Logs to mumble.log (in ~/.mumble/ when installed, or same directory in dev mode).
"""

import argparse
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import winsound
from math import gcd

import keyboard
import numpy as np
import pyperclip
import sounddevice as sd
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw
from scipy.signal import resample_poly
from pynput.keyboard import Controller as KBController, Key

import pystray

# --- Paths ---
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_USER_DIR = os.path.join(os.path.expanduser("~"), ".mumble")


def _is_dev_mode():
    """True when running from a source checkout (mumble.toml sits next to the script)."""
    return os.path.isfile(os.path.join(_SCRIPT_DIR, "mumble.toml"))


def _find_config():
    """Return the path to mumble.toml, or None if not found."""
    # 1. User config dir (~/.mumble/)
    user_cfg = os.path.join(_USER_DIR, "mumble.toml")
    if os.path.isfile(user_cfg):
        return user_cfg
    # 2. Same directory as the script (dev / clone mode)
    script_cfg = os.path.join(_SCRIPT_DIR, "mumble.toml")
    if os.path.isfile(script_cfg):
        return script_cfg
    return None


def _log_dir():
    """Return the directory for mumble.log."""
    if _is_dev_mode():
        return _SCRIPT_DIR
    os.makedirs(_USER_DIR, exist_ok=True)
    return _USER_DIR


# --- Logging ---
LOG_FILE = os.path.join(_log_dir(), "mumble.log")

log = logging.getLogger("mumble")
log.setLevel(logging.INFO)


def _setup_logging(log_file):
    """Attach a rotating file handler to the mumble logger. Returns the handler.

    Called from main() rather than at import time so that importing the module
    (e.g. from tests) never touches the real log file.
    """
    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=1,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    return handler


# --- Config ---
_DEFAULTS = {
    "hotkeys": {
        "record": "ctrl+alt+space",
        "cancel": "ctrl+alt+x",
        "quit": "ctrl+alt+q",
        "repaste": "ctrl+alt+v",
    },
    "whisper": {
        "model": "small.en",
        "sample_rate": 16000,
    },
    "audio": {
        "device_name": "C200",
        "max_duration": 90,
    },
    "commands": {
        "trigger": "claude",
    },
}

CONFIG_PATH = _find_config()


def _load_config():
    """Load config from mumble.toml, falling back to defaults if missing."""
    if CONFIG_PATH:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
    else:
        cfg = {}

    def _get(section, key):
        return cfg.get(section, {}).get(key, _DEFAULTS[section][key])

    return _get

_get = _load_config()

HOTKEY = _get("hotkeys", "record")
CANCEL_HOTKEY = _get("hotkeys", "cancel")
QUIT_HOTKEY = _get("hotkeys", "quit")
REPASTE_HOTKEY = _get("hotkeys", "repaste")
MODEL = _get("whisper", "model")
WHISPER_RATE = _get("whisper", "sample_rate")
DEVICE_NAME_SUBSTRING = _get("audio", "device_name")
MAX_DURATION = _get("audio", "max_duration")
COMMAND_TRIGGER = _get("commands", "trigger")

# --- Slash command trigger aliases ---
_CLAUDE_ALIASES = {"clod", "clawed", "cloud", "claud", "clogged", "klaud", "klaude"}

def _trigger_variants(trigger):
    """Return a set of lowercase strings that should match the trigger word."""
    variants = {trigger.lower()}
    if trigger.lower() == "claude":
        variants |= _CLAUDE_ALIASES
    return variants

# --- Globals ---
recording = False
transcribing = False
audio_chunks = []
last_transcription = None
kb = KBController()
model = None
device_index = None
record_rate = 48000
input_stream = None
tray_icon = None
shutdown_event = threading.Event()
recording_timer = None


# --- Tray icon ---

def make_mic_icon(color, bg=(40, 40, 40)):
    """Draw a simple microphone icon."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background circle
    d.ellipse([0, 0, 63, 63], fill=bg)

    # Mic head (rounded rect as ellipse)
    d.rounded_rectangle([22, 10, 42, 36], radius=10, fill=color)

    # Mic stem
    d.line([32, 36, 32, 48], fill=color, width=3)

    # Mic base arc
    d.arc([18, 28, 46, 50], start=0, end=180, fill=color, width=3)

    # Base stand
    d.line([24, 48, 40, 48], fill=color, width=3)

    return img


def update_tray(is_recording):
    """Update the tray icon to reflect recording state."""
    if tray_icon is None:
        return
    if is_recording:
        tray_icon.icon = make_mic_icon(color=(255, 80, 80))
        tray_icon.title = "Mumble — Recording..."
    else:
        tray_icon.icon = make_mic_icon(color=(120, 220, 120))
        tray_icon.title = "Mumble — Idle"


def open_log(icon, item):
    """Open the log file in VS Code."""
    subprocess.Popen(f'code "{LOG_FILE}"', shell=True)


def show_help():
    """Open the README on GitHub in the default browser."""
    import webbrowser
    log.info("Voice command: show help")
    webbrowser.open("https://github.com/thompler/mumble#readme")


def quit_from_tray(icon, item):
    """Quit from tray right-click menu."""
    log.info("Quit from tray")
    shutdown_event.set()


def setup_tray():
    """Create and start the system tray icon in a background thread."""
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("Hotkeys", pystray.Menu(
            pystray.MenuItem(f"Record:  {HOTKEY}", None, enabled=False),
            pystray.MenuItem(f"Cancel:  {CANCEL_HOTKEY}", None, enabled=False),
            pystray.MenuItem(f"Repaste: {REPASTE_HOTKEY}", None, enabled=False),
            pystray.MenuItem(f"Quit:    {QUIT_HOTKEY}", None, enabled=False),
        )),
        pystray.MenuItem("Open Log", open_log),
        pystray.MenuItem("Quit", quit_from_tray),
    )
    tray_icon = pystray.Icon(
        "mumble",
        make_mic_icon(color=(120, 220, 120)),
        "Mumble — Idle",
        menu,
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()


# --- Audio ---

def find_c200_device():
    """Find the Anker C200 mic by name. Returns (index, sample_rate) or (None, None)."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and DEVICE_NAME_SUBSTRING in d["name"]:
            rate = int(d["default_samplerate"])
            log.info(f"Found C200: [{i}] {d['name']} (rate: {rate} Hz)")
            return i, rate
    return None, None


def audio_callback(indata, frames, time_info, status):
    """Called by sounddevice for each audio block during recording."""
    if status:
        log.warning(f"Audio status: {status}")
    if recording:
        audio_chunks.append(indata.copy())


def start_recording():
    """Open the mic stream via sounddevice. Returns True on success, False on failure."""
    global audio_chunks, input_stream
    audio_chunks = []

    kwargs = {
        "samplerate": record_rate,
        "channels": 1,
        "dtype": "float32",
        "callback": audio_callback,
        "blocksize": 1024,
    }
    if device_index is not None:
        kwargs["device"] = device_index

    try:
        input_stream = sd.InputStream(**kwargs)
        input_stream.start()
    except Exception:
        log.exception("Failed to start audio stream")
        input_stream = None
        return False
    log.info(f"Recording started ({record_rate} Hz)")
    return True


def stop_recording_and_transcribe():
    """Stop the mic stream, transcribe collected audio, paste the result."""
    global input_stream, last_transcription, transcribing

    try:
        if input_stream:
            input_stream.stop()
            input_stream.close()
            input_stream = None

        update_tray(False)

        if not audio_chunks:
            log.info("No audio captured")
            return

        audio_np = np.concatenate(audio_chunks, axis=0).flatten()
        duration = len(audio_np) / record_rate
        log.info(f"Audio: {duration:.1f}s ({len(audio_chunks)} chunks)")

        # Resample to 16kHz if needed
        if record_rate != WHISPER_RATE:
            g = gcd(record_rate, WHISPER_RATE)
            audio_np = resample_poly(audio_np, up=WHISPER_RATE // g, down=record_rate // g)

        log.info("Transcribing...")
        segments, _info = model.transcribe(audio_np, language="en", beam_size=5)
        text = " ".join(seg.text for seg in segments).strip()

        if not text:
            log.info("Empty transcription")
            return

        # Collapse spaces between individual digits: "1 2 3 4" → "1234"
        text = re.sub(r'(?<=\d) (?=\d)', '', text)

        # Collapse runs of multiple spaces into one
        text = re.sub(r' {2,}', ' ', text)

        # Slash commands: "claude clear" → "/clear"
        trigger_variants = _trigger_variants(COMMAND_TRIGGER)
        pattern = r'(?i)^(?:' + '|'.join(re.escape(v) for v in trigger_variants) + r')\s+(\S+)$'
        cmd_match = re.match(pattern, text.strip("., "))
        if cmd_match:
            command = cmd_match.group(1).lower()
            text = f"/{command}"
            log.info(f"Voice command: {text}")

        log.info(f"Transcribed: {text}")

        # Voice commands
        if text.strip("., ").lower() == "show help":
            show_help()
            return

        last_transcription = text
        paste_text(text)
    except Exception:
        log.exception("Transcription failed")
        winsound.Beep(300, 200)
    finally:
        transcribing = False


def paste_text(text):
    """Copy text to clipboard and paste into the active window."""
    # Wait for modifier keys to be released (e.g. from the hotkey that triggered this)
    while keyboard.is_pressed('alt') or keyboard.is_pressed('ctrl'):
        time.sleep(0.05)
    time.sleep(0.1)
    pyperclip.copy(text)
    kb.press(Key.ctrl)
    kb.press('v')
    kb.release('v')
    kb.release(Key.ctrl)


def repaste():
    """Re-paste the last transcription."""
    if last_transcription is None:
        winsound.Beep(300, 200)
        log.info("Repaste: no previous transcription")
        return
    log.info(f"Repasting: {last_transcription}")
    paste_text(last_transcription)


def stop_stream():
    """Stop the mic stream without transcribing."""
    global input_stream, recording
    recording = False
    if input_stream:
        input_stream.stop()
        input_stream.close()
        input_stream = None


def cancel_recording():
    """Cancel recording and discard audio."""
    global audio_chunks, recording_timer
    if not recording:
        return
    if recording_timer is not None:
        recording_timer.cancel()
        recording_timer = None
    stop_stream()
    audio_chunks = []
    update_tray(False)
    log.info("Recording cancelled")
    winsound.Beep(300, 200)


def _timeout_cancel():
    """Auto-cancel recording after MAX_DURATION seconds."""
    log.info(f"Recording timeout ({MAX_DURATION}s) — auto-cancelling")
    cancel_recording()


def decide_toggle_action(recording, transcribing):
    """Decide what a record-hotkey press should do given current state.

    Returns "ignore" while a transcription is still running (pressing record
    then would clear audio_chunks out from under it), "stop" if currently
    recording, else "start".
    """
    if transcribing:
        return "ignore"
    if recording:
        return "stop"
    return "start"


def toggle_recording():
    """Toggle recording on/off on hotkey press."""
    global recording, recording_timer, transcribing

    action = decide_toggle_action(recording, transcribing)

    if action == "ignore":
        log.info("Record hotkey ignored — transcription in progress")
        winsound.Beep(300, 200)
        return

    if action == "start":
        recording = True
        if not start_recording():
            recording = False
            update_tray(False)
            winsound.Beep(300, 200)
            return
        log.info("Recording...")
        winsound.Beep(1000, 150)
        update_tray(True)
        recording_timer = threading.Timer(MAX_DURATION, _timeout_cancel)
        recording_timer.daemon = True
        recording_timer.start()
    else:  # "stop"
        if recording_timer is not None:
            recording_timer.cancel()
            recording_timer = None
        recording = False
        transcribing = True
        log.info("Stopped, transcribing...")
        winsound.Beep(500, 150)
        threading.Thread(target=stop_recording_and_transcribe, daemon=True).start()


def quit_app():
    """Quit via hotkey."""
    log.info("Quit hotkey pressed")
    shutdown_event.set()


_STARTUP_DIR = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
_SHORTCUT_NAME = "Mumble.lnk"


def _install_startup():
    """Create a Start Menu shortcut so Mumble launches on login."""
    target = shutil.which("mumble-gui")
    if target is None:
        print("Error: mumble-gui not found on PATH. Is Mumble installed via pip?")
        sys.exit(1)
    shortcut_path = os.path.join(_STARTUP_DIR, _SHORTCUT_NAME)
    ps_script = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{shortcut_path}"); '
        f'$s.TargetPath = "{target}"; '
        f'$s.Save()'
    )
    subprocess.run(["powershell", "-Command", ps_script], check=True)
    print(f"Startup shortcut created: {shortcut_path}")


def _remove_startup():
    """Remove the Mumble startup shortcut."""
    shortcut_path = os.path.join(_STARTUP_DIR, _SHORTCUT_NAME)
    if os.path.exists(shortcut_path):
        os.remove(shortcut_path)
        print(f"Startup shortcut removed: {shortcut_path}")
    else:
        print("No startup shortcut found.")


def main():
    parser = argparse.ArgumentParser(description="Mumble — hotkey-activated speech-to-text")
    parser.add_argument("--install-startup", action="store_true", help="Add Mumble to Windows startup")
    parser.add_argument("--remove-startup", action="store_true", help="Remove Mumble from Windows startup")
    args = parser.parse_args()

    if args.install_startup:
        _install_startup()
        return
    if args.remove_startup:
        _remove_startup()
        return

    _setup_logging(LOG_FILE)
    log.info("=" * 40)
    log.info("Mumble starting")
    if CONFIG_PATH:
        log.info(f"Config loaded from {CONFIG_PATH}")
    else:
        log.warning("No mumble.toml found, using defaults")

    # mumble-gui has no console, so an unhandled exception here would be
    # invisible. Log it, beep, and exit non-zero instead.
    try:
        _run()
    except Exception:
        log.exception("Fatal error during startup")
        winsound.Beep(300, 500)
        sys.exit(1)


def _run():
    """Initialise audio, model, tray and hotkeys, then block until quit."""
    global model, device_index, record_rate

    # Find C200 mic
    device_index, detected_rate = find_c200_device()
    if device_index is not None:
        record_rate = detected_rate
    else:
        log.info("C200 not found — using system default mic")

    # Load Whisper model
    log.info(f"Loading Whisper model ({MODEL})...")
    model = WhisperModel(MODEL, device="cpu", compute_type="int8")
    log.info("Model loaded")

    # System tray
    setup_tray()

    # Register hotkeys
    keyboard.add_hotkey(HOTKEY, toggle_recording)
    keyboard.add_hotkey(CANCEL_HOTKEY, cancel_recording)
    keyboard.add_hotkey(QUIT_HOTKEY, quit_app)
    keyboard.add_hotkey(REPASTE_HOTKEY, repaste)
    log.info(f"Hotkeys: {HOTKEY} (record), {CANCEL_HOTKEY} (cancel), {REPASTE_HOTKEY} (repaste), {QUIT_HOTKEY} (quit)")

    # Startup beep
    winsound.Beep(800, 100)
    winsound.Beep(1000, 100)
    log.info("Ready")

    # Keep alive until quit
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass

    keyboard.unhook_all()
    if tray_icon:
        tray_icon.stop()
    log.info("Goodbye")


if __name__ == "__main__":
    main()

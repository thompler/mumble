"""
Mumble — Hotkey-activated speech-to-text that pastes into the active window.

Ctrl+Alt+Space  — start/stop recording
Ctrl+Alt+X      — cancel recording
Ctrl+Alt+V      — re-paste last transcription
Ctrl+Alt+Q      — quit

Runs silently in the background (no console window needed).
Logs to mumble.log in the same directory.
"""

import logging
import logging.handlers
import os
import re
import threading
import time
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

# --- Config ---
HOTKEY = "ctrl+alt+space"
CANCEL_HOTKEY = "ctrl+alt+x"
QUIT_HOTKEY = "ctrl+alt+q"
REPASTE_HOTKEY = "ctrl+alt+v"
MODEL = "small.en"
DEVICE_NAME_SUBSTRING = "C200"
WHISPER_RATE = 16000

# --- Logging ---
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mumble.log")
log = logging.getLogger("mumble")
log.setLevel(logging.INFO)
handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=1,
)
handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(handler)

# --- Globals ---
recording = False
audio_chunks = []
last_transcription = None
kb = KBController()
model = None
device_index = None
record_rate = 48000
input_stream = None
tray_icon = None
shutdown_event = threading.Event()


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
    import subprocess
    subprocess.Popen(f'code "{LOG_FILE}"', shell=True)


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
    """Open the mic stream via sounddevice."""
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

    input_stream = sd.InputStream(**kwargs)
    input_stream.start()
    log.info(f"Recording started ({record_rate} Hz)")


def stop_recording_and_transcribe():
    """Stop the mic stream, transcribe collected audio, paste the result."""
    global input_stream

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

    log.info(f"Transcribed: {text}")

    global last_transcription
    last_transcription = text
    paste_text(text)


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
    global audio_chunks
    if not recording:
        return
    stop_stream()
    audio_chunks = []
    update_tray(False)
    log.info("Recording cancelled")
    winsound.Beep(300, 200)


def toggle_recording():
    """Toggle recording on/off on hotkey press."""
    global recording

    if not recording:
        recording = True
        log.info("Recording...")
        winsound.Beep(1000, 150)
        update_tray(True)
        start_recording()
    else:
        recording = False
        log.info("Stopped, transcribing...")
        winsound.Beep(500, 150)
        threading.Thread(target=stop_recording_and_transcribe, daemon=True).start()


def quit_app():
    """Quit via hotkey."""
    log.info("Quit hotkey pressed")
    shutdown_event.set()


def main():
    global model, device_index, record_rate

    log.info("=" * 40)
    log.info("Mumble starting")

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

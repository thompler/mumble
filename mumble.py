"""
Mumble — Hotkey-activated speech-to-text that pastes into the active window.

Ctrl+Alt+Space  — hold to record, release to stop (tap to toggle; see hotkeys.record_mode)
Ctrl+Alt+X      — cancel recording
Ctrl+Alt+V      — re-paste last transcription
Ctrl+Alt+Q      — quit

Runs silently in the background (no console window needed).
Logs to mumble.log (in ~/.mumble/ when installed, or same directory in dev mode).
"""

import argparse
import ctypes
import ctypes.wintypes as wintypes
import itertools
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
from datetime import datetime
from math import gcd

import numpy as np
import pyperclip
import sounddevice as sd
from faster_whisper import WhisperModel
from PIL import Image, ImageDraw
from scipy.io import wavfile
from scipy.signal import resample_poly
from pynput.keyboard import Controller as KBController, Key

import pystray

# --- Paths ---
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_USER_DIR = os.path.join(os.path.expanduser("~"), ".mumble")


def _is_dev_mode():
    """True when running from a source checkout (pyproject.toml sits next to the script).

    pyproject.toml is never installed into site-packages, so its presence is a
    reliable checkout marker. mumble.toml is not used because it is also a
    legitimate (untracked) user config file in a checkout.
    """
    return os.path.isfile(os.path.join(_SCRIPT_DIR, "pyproject.toml"))


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


RECORDINGS_DIR = os.path.join(_log_dir(), "recordings")
RECORDINGS_KEEP = 5


def _save_recording(audio, rate, directory, keep=RECORDINGS_KEEP):
    """Write float audio in [-1, 1] as a 16-bit mono WAV in directory, then prune.

    Files are named rec-YYYYMMDD-HHMMSS-mmm.wav so lexical order is
    chronological; only the newest `keep` rec-*.wav files survive. Returns the
    path written.
    """
    os.makedirs(directory, exist_ok=True)
    name = "rec-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3] + ".wav"
    path = os.path.join(directory, name)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    wavfile.write(path, rate, pcm)
    recs = sorted(f for f in os.listdir(directory) if f.startswith("rec-") and f.endswith(".wav"))
    for old in recs[:-keep]:
        try:
            os.remove(os.path.join(directory, old))
        except OSError:
            log.warning(f"Could not delete old recording {old}")
    return path


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
        "record_mode": "hold",
    },
    "whisper": {
        "model": "small.en",
        "sample_rate": 16000,
        "vocabulary": [],
    },
    "audio": {
        "device_name": "C200",
        "max_duration": 90,
    },
    "commands": {
        "trigger": "claude",
    },
    "debug": {
        "save_recordings": False,
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
SAVE_RECORDINGS = bool(_get("debug", "save_recordings"))


_MODIFIER_NAMES = {"ctrl", "control", "alt", "shift", "win", "windows", "cmd", "meta", "super"}


def _hotkey_modifiers(hotkeys):
    """Return the set of modifier key names used by any of the given hotkey strings.

    A '+'-separated part counts as a modifier if its base name (ignoring a
    "left"/"right" prefix) is a known modifier, so "ctrl+alt+win" contributes
    {"ctrl", "alt", "win"} and "f9" contributes nothing.
    """
    mods = set()
    for hk in hotkeys:
        for part in hk.split("+"):
            part = part.strip().lower()
            if part.split()[-1] in _MODIFIER_NAMES:
                mods.add(part)
    return mods


HOTKEY_MODIFIERS = _hotkey_modifiers([HOTKEY, CANCEL_HOTKEY, QUIT_HOTKEY, REPASTE_HOTKEY])

RECORD_MODES = ("hold", "toggle")


def _validate_record_mode(mode: str) -> str:
    """Return the lowercased record mode, or raise ValueError if it is not supported."""
    normalised = str(mode).strip().lower()
    if normalised not in RECORD_MODES:
        raise ValueError(
            f"Invalid hotkeys.record_mode {mode!r}: expected one of {', '.join(RECORD_MODES)}"
        )
    return normalised


# Raw config value; validated at the top of _run() so a bad value is logged
# by main()'s fatal-error path instead of crashing at import.
RECORD_MODE = _get("hotkeys", "record_mode")


def _vocabulary_prompt(words):
    """Turn the whisper.vocabulary list into an initial_prompt for Whisper.

    ["CPI", "Jira"] -> "CPI, Jira." Entries are stripped and blanks dropped;
    an empty result returns None (no prompt). Raises ValueError unless words
    is a list of strings.
    """
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise ValueError(f"Invalid whisper.vocabulary {words!r}: expected a list of strings")
    terms = [w.strip() for w in words if w.strip()]
    if not terms:
        return None
    return ", ".join(terms) + "."


# Raw config value; validated at the top of _run() like RECORD_MODE.
VOCABULARY = _get("whisper", "vocabulary")
INITIAL_PROMPT = None  # set by _run() from VOCABULARY

# --- Slash command trigger aliases ---
_CLAUDE_ALIASES = {"clod", "clawed", "cloud", "claud", "clogged", "klaud", "klaude"}

def _trigger_variants(trigger):
    """Return a set of lowercase strings that should match the trigger word."""
    variants = {trigger.lower()}
    if trigger.lower() == "claude":
        variants |= _CLAUDE_ALIASES
    return variants

# --- Global hotkeys (Win32 RegisterHotKey) ---
#
# The OS matches the key combination and posts WM_HOTKEY to the registering
# thread. Unlike a low-level keyboard hook there is no callback that Windows
# can time out and silently remove, no in-process "pressed keys" table that
# can go stale, and MOD_NOREPEAT stops key auto-repeat from re-firing it.

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
_user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
_user32.VkKeyScanW.restype = ctypes.c_short

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP = 0x8000
WM_SHIELD_PUSH = WM_APP + 1     # posted to the hotkey thread: register the shield combos
WM_SHIELD_POP = WM_APP + 2      # posted to the hotkey thread: unregister them
SHIELD_ID_BASE = 100            # hotkey ids below this are the four real bindings
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000

_MOD_FLAGS = {
    "alt": MOD_ALT, "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "cmd": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
}
_NAMED_VKS = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "windows": 0x5B, "cmd": 0x5B, "meta": 0x5B, "super": 0x5B,
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "page up": 0x21, "pageup": 0x21, "page down": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "caps lock": 0x14, "num lock": 0x90, "scroll lock": 0x91, "pause": 0x13, "print screen": 0x2C,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
}
# Virtual-key codes consulted when waiting for a modifier to be released.
_MODIFIER_VKS = {
    "ctrl": (0x11,), "control": (0x11,), "left ctrl": (0xA2,), "right ctrl": (0xA3,),
    "alt": (0x12,), "left alt": (0xA4,), "right alt": (0xA5,),
    "shift": (0x10,), "left shift": (0xA0,), "right shift": (0xA1,),
    "win": (0x5B, 0x5C), "windows": (0x5B, 0x5C), "cmd": (0x5B, 0x5C), "meta": (0x5B, 0x5C), "super": (0x5B, 0x5C),
    "left win": (0x5B,), "left windows": (0x5B,), "right win": (0x5C,), "right windows": (0x5C,),
}


CHORD_POLL_SECONDS = 0.02
# Virtual keys to poll for each modifier flag when a chord has no main key.
_FLAG_VKS = {MOD_CONTROL: (0x11,), MOD_ALT: (0x12,), MOD_SHIFT: (0x10,), MOD_WIN: (0x5B, 0x5C)}


def _parse_hotkey(hotkey):
    """Turn "ctrl+alt+x" into (modifier_flags, virtual_key) for RegisterHotKey.

    The last '+'-separated part is normally the main key. If it is itself a
    modifier (e.g. "ctrl+alt+win") the result is (all_modifier_flags, None):
    Windows accepts such a registration but never delivers it, so these
    modifier-only chords are detected by polling instead (see HotkeyManager).
    """
    parts = [p.strip().lower() for p in hotkey.split("+")]
    *modifiers, key = parts
    flags = 0
    for m in modifiers:
        if m not in _MOD_FLAGS:
            raise ValueError(f"Unknown modifier {m!r} in hotkey {hotkey!r}")
        flags |= _MOD_FLAGS[m]
    if key in _MOD_FLAGS:
        return flags | _MOD_FLAGS[key], None
    if key in _NAMED_VKS:
        vk = _NAMED_VKS[key]
    elif len(key) == 1:
        scan = _user32.VkKeyScanW(key)
        if scan == -1:
            raise ValueError(f"Unknown key {key!r} in hotkey {hotkey!r}")
        vk = scan & 0xFF
    else:
        raise ValueError(f"Unknown key {key!r} in hotkey {hotkey!r}")
    return flags, vk


def _is_vk_held(vk):
    """True if virtual key `vk` is currently down (live OS state; ignores the was-pressed low bit)."""
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _shield_hotkeys(hotkey):
    """Combos to capture while a recording runs so the main key cannot leak.

    RegisterHotKey consumes a keystroke only when it matches a registered
    combo. Holding ctrl+alt+space auto-repeats Space; once Ctrl or Alt is
    released those repeats match nothing and land in the foreground app as a
    run of spaces. Registering the main key with every smaller modifier subset
    ("space", "ctrl+space", "alt+space") for the duration of the recording
    makes Mumble swallow them instead. Chords with no main key get nothing.
    """
    _flags, vk = _parse_hotkey(hotkey)
    if vk is None:
        return []
    *modifiers, key = [p.strip().lower() for p in hotkey.split("+")]
    return [
        "+".join(subset + (key,))
        for r in range(len(modifiers))
        for subset in itertools.combinations(modifiers, r)
    ]


def _probe_owned_hotkeys(hotkeys):
    """Return the combos in `hotkeys` that some other program has already registered.

    Ownership is global, so a register-then-unregister from any thread is a
    valid probe. Used at startup to warn when a shield combo (see
    _shield_hotkeys) cannot be captured during recording.
    """
    owned = []
    for hotkey in hotkeys:
        flags, vk = _parse_hotkey(hotkey)
        if vk is None:
            continue
        if _user32.RegisterHotKey(None, 0x7FFF, flags | MOD_NOREPEAT, vk):
            _user32.UnregisterHotKey(None, 0x7FFF)
        else:
            owned.append(hotkey)
    return owned


def _is_key_held(name):
    """True if any physical key for the modifier `name` is currently down (live OS state)."""
    return any(_is_vk_held(vk) for vk in _MODIFIER_VKS.get(name, ()))


def _release_vks(hotkey):
    """Virtual keys whose release ends a hold: the main key, or for a modifier-only chord its last part."""
    _, vk = _parse_hotkey(hotkey)
    if vk is not None:
        return (vk,)
    last = hotkey.split("+")[-1].strip().lower()
    return _FLAG_VKS[_MOD_FLAGS[last]]


class HotkeyManager:
    """Global hotkeys: RegisterHotKey on a message-loop thread, plus a poller for modifier-only chords.

    Callbacks run on whichever of the two threads detected the hotkey.
    """

    def __init__(self):
        self._bindings = []        # RegisterHotKey: (hotkey_str, (flags, vk), callback)
        self._chords = []          # polled:        (hotkey_str, [vk_alternatives...], callback)
        self._thread = None
        self._chord_thread = None
        self.thread_id = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._error = None
        self._shield = []          # [(hotkey_str, (flags, vk))] requested by push_shield
        self._shield_ids = []      # ids currently registered on the loop thread

    def push_shield(self, hotkeys):
        """Ask the loop thread to register `hotkeys` as swallowed combos (see _shield_hotkeys)."""
        self._shield = [(hk, _parse_hotkey(hk)) for hk in hotkeys]
        if self.thread_id is not None:
            _user32.PostThreadMessageW(self.thread_id, WM_SHIELD_PUSH, 0, 0)

    def pop_shield(self):
        """Ask the loop thread to unregister the shield combos. No-op if none are active."""
        if self.thread_id is not None:
            _user32.PostThreadMessageW(self.thread_id, WM_SHIELD_POP, 0, 0)

    def register(self, hotkey, callback):
        """Queue a hotkey for registration. Parses eagerly so bad strings fail before start()."""
        flags, vk = _parse_hotkey(hotkey)
        if vk is None:
            vks = [_FLAG_VKS[f] for f in _FLAG_VKS if flags & f]
            self._chords.append((hotkey, vks, callback))
        else:
            self._bindings.append((hotkey, (flags, vk), callback))

    def start(self):
        """Start the threads and block until every hotkey is registered. Raises on failure."""
        self._thread = threading.Thread(target=self._loop, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._error is not None:
            self._thread.join(timeout=2)
            raise self._error
        if self._chords:
            self._chord_thread = threading.Thread(target=self._poll_chords, name="hotkey-chords", daemon=True)
            self._chord_thread.start()

    def stop(self):
        self._stop.set()
        if self.thread_id is not None:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        for t in (self._thread, self._chord_thread):
            if t is not None:
                t.join(timeout=2)

    def is_alive(self):
        return any(t is not None and t.is_alive() for t in (self._thread, self._chord_thread))

    def _poll_chords(self):
        """Fire each chord once per hold: on the transition from "not all held" to "all held"."""
        def chord_held(vks):
            return all(any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in alts) for alts in vks)

        was_held = [chord_held(vks) for _, vks, _ in self._chords]
        while not self._stop.wait(CHORD_POLL_SECONDS):
            for i, (hotkey, vks, callback) in enumerate(self._chords):
                now_held = chord_held(vks)
                if now_held and not was_held[i]:
                    try:
                        callback()
                    except Exception:
                        log.exception(f"Hotkey handler for {hotkey!r} failed")
                was_held[i] = now_held

    def _loop(self):
        self.thread_id = _kernel32.GetCurrentThreadId()
        msg = wintypes.MSG()
        # Force creation of this thread's message queue so PostThreadMessage works immediately.
        _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        registered = 0
        try:
            for i, (hotkey, (flags, vk), _cb) in enumerate(self._bindings, start=1):
                if not _user32.RegisterHotKey(None, i, flags | MOD_NOREPEAT, vk):
                    err = ctypes.get_last_error()
                    raise OSError(err, f"RegisterHotKey failed for {hotkey!r}: {ctypes.FormatError(err).strip()}")
                registered = i
        except Exception as e:
            self._error = e
            self._unregister(registered)
            self._ready.set()
            return
        self._ready.set()

        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                idx = int(msg.wParam) - 1
                if 0 <= idx < len(self._bindings):
                    hotkey, _, callback = self._bindings[idx]
                    try:
                        callback()
                    except Exception:
                        log.exception(f"Hotkey handler for {hotkey!r} failed")
                # ids >= SHIELD_ID_BASE are shield combos: consumed, deliberately ignored
            elif msg.message == WM_SHIELD_PUSH:
                self._register_shield()
            elif msg.message == WM_SHIELD_POP:
                self._unregister_shield()
        self._unregister_shield()
        self._unregister(registered)

    def _register_shield(self):
        # RegisterHotKey/UnregisterHotKey are per-thread, so this runs on the loop thread only.
        self._unregister_shield()
        for i, (hotkey, (flags, vk)) in enumerate(self._shield, start=SHIELD_ID_BASE):
            if _user32.RegisterHotKey(None, i, flags | MOD_NOREPEAT, vk):
                self._shield_ids.append(i)
            else:
                log.warning(f"Shield hotkey {hotkey!r} unavailable: {ctypes.FormatError(ctypes.get_last_error()).strip()}")

    def _unregister_shield(self):
        for i in self._shield_ids:
            _user32.UnregisterHotKey(None, i)
        self._shield_ids = []

    def _unregister(self, count):
        for i in range(1, count + 1):
            _user32.UnregisterHotKey(None, i)


# --- Globals ---
recording = False
transcribing = False
audio_chunks = []
last_transcription = None
kb = KBController()
model = None
device_index = None
record_rate = 48000
record_release_vks = ()
record_shield_hotkeys = []
hotkeys = None                  # HotkeyManager, created in _run()
input_stream = None
tray_icon = None
shutdown_event = threading.Event()
recording_timer = None
_last_record_trigger = 0.0
_main_key_released = True       # False from a record press until the watcher sees the main key up
RECORD_DEBOUNCE_SECONDS = 0.3
HOLD_TAP_SECONDS = 0.3
RELEASE_POLL_SECONDS = 0.02


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
            pystray.MenuItem(f"Record:  {HOTKEY} ({RECORD_MODE})", None, enabled=False),
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

        if SAVE_RECORDINGS:
            try:
                path = _save_recording(audio_np, WHISPER_RATE, RECORDINGS_DIR)
                log.info(f"Saved recording: {os.path.basename(path)}")
            except Exception:
                log.exception("Failed to save recording")

        log.info("Transcribing...")
        segments, _info = model.transcribe(
            audio_np, language="en", beam_size=5, initial_prompt=INITIAL_PROMPT,
        )
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
        if hotkeys is not None:
            hotkeys.pop_shield()


def _wait_for_modifier_release(modifiers, timeout=2.0):
    """Block until none of `modifiers` is held, or `timeout` seconds pass.

    Returns True if the keys were released, False on timeout. Key state comes
    from GetAsyncKeyState, so it cannot go stale; the timeout only guards
    against a user who simply keeps holding the keys, since this can run on
    the hotkey thread and would otherwise block every other hotkey.
    """
    deadline = time.monotonic() + timeout
    while any(_is_key_held(m) for m in modifiers):
        if time.monotonic() >= deadline:
            log.warning(f"Modifier keys still held after {timeout}s — pasting anyway")
            return False
        time.sleep(0.05)
    return True


def paste_text(text):
    """Copy text to clipboard and paste into the active window.

    Returns True if Ctrl+V was sent. If the hotkey's modifiers are still held
    after the wait, the paste is skipped (False): a synthetic Ctrl+V with Alt
    physically down reaches Windows as Ctrl+Alt+V, which is our own repaste
    hotkey, so pasting anyway re-triggers ourselves. The text stays on the
    clipboard and in last_transcription, so a manual Ctrl+V or the repaste
    hotkey recovers it.
    """
    pyperclip.copy(text)
    if not _wait_for_modifier_release(HOTKEY_MODIFIERS):
        log.warning("Paste skipped: modifier keys still held (text is on the clipboard)")
        winsound.Beep(300, 200)
        return False
    time.sleep(0.1)
    kb.press(Key.ctrl)
    kb.press('v')
    kb.release('v')
    kb.release(Key.ctrl)
    return True


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
    if hotkeys is not None:
        hotkeys.pop_shield()
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


def _stop_and_transcribe():
    """End the current recording and transcribe it. No-op if not recording."""
    global recording, recording_timer, transcribing
    if not recording:
        return
    if recording_timer is not None:
        recording_timer.cancel()
        recording_timer = None
    recording = False
    transcribing = True
    log.info("Stopped, transcribing...")
    winsound.Beep(500, 150)
    threading.Thread(target=stop_recording_and_transcribe, daemon=True).start()


def _watch_release(press_time, vks):
    """Watch the hotkey's main key after a record press; in hold mode, stop on release.

    Runs in both modes. It records the first observed release in
    _main_key_released so toggle_recording() can tell a genuine second press
    from an auto-repeat re-fire (Windows drops MOD_NOREPEAT suppression once
    whenever hotkeys are registered mid-hold, which the shield push does).

    Hold mode: a release within HOLD_TAP_SECONDS of the press is a tap and
    recording keeps running until the next press; a later release stops it.
    Does nothing if the recording ended first.

    The watcher starts only after the stream is open and the start beep has
    played, so elapsed time at the first observed key-up is unreliable. A key
    already up on the first poll (quick tap, or an elevated window where the
    key always reads up) is therefore treated as a tap whatever the clock says.
    """
    global _main_key_released
    if not any(_is_vk_held(vk) for vk in vks):
        _main_key_released = True
        return
    while recording and not shutdown_event.is_set():
        if not any(_is_vk_held(vk) for vk in vks):
            _main_key_released = True
            break
        time.sleep(RELEASE_POLL_SECONDS)
    if not recording or shutdown_event.is_set():
        return
    if RECORD_MODE != "hold":
        return
    if time.monotonic() - press_time >= HOLD_TAP_SECONDS:
        _stop_and_transcribe()


def toggle_recording():
    """Toggle recording on/off on hotkey press."""
    global recording, recording_timer, _last_record_trigger, _main_key_released

    # Holding the combo makes the OS auto-repeat the final key (~30/s), which
    # fires this hotkey over and over. Treat triggers arriving closer than the
    # debounce window as the same press; each repeat extends the window, so a
    # held key fires exactly once until it is released.
    now = time.monotonic()
    is_repeat = (now - _last_record_trigger) < RECORD_DEBOUNCE_SECONDS
    _last_record_trigger = now
    if is_repeat:
        return
    if recording and not _main_key_released:
        # The main key has not been let go since the press that started this
        # recording, so this trigger is auto-repeat, not a second press.
        return

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
        log.info("Recording (hold)..." if RECORD_MODE == "hold" else "Recording...")
        winsound.Beep(1000, 150)
        update_tray(True)
        recording_timer = threading.Timer(MAX_DURATION, _timeout_cancel)
        recording_timer.daemon = True
        recording_timer.start()
        if hotkeys is not None and record_shield_hotkeys:
            hotkeys.push_shield(record_shield_hotkeys)
        _main_key_released = False
        threading.Thread(target=_watch_release, args=(now, record_release_vks), daemon=True).start()
    else:  # "stop"
        _stop_and_transcribe()


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
    global model, device_index, record_rate, RECORD_MODE, INITIAL_PROMPT, record_release_vks, record_shield_hotkeys, hotkeys

    RECORD_MODE = _validate_record_mode(RECORD_MODE)
    INITIAL_PROMPT = _vocabulary_prompt(VOCABULARY)
    if INITIAL_PROMPT:
        log.info(f"Vocabulary hint: {INITIAL_PROMPT}")

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
    record_release_vks = _release_vks(HOTKEY)
    record_shield_hotkeys = _shield_hotkeys(HOTKEY)
    hotkeys = HotkeyManager()
    hotkeys.register(HOTKEY, toggle_recording)
    hotkeys.register(CANCEL_HOTKEY, cancel_recording)
    hotkeys.register(QUIT_HOTKEY, quit_app)
    hotkeys.register(REPASTE_HOTKEY, repaste)
    hotkeys.start()
    log.info(f"Hotkeys: {HOTKEY} (record), {CANCEL_HOTKEY} (cancel), {REPASTE_HOTKEY} (repaste), {QUIT_HOTKEY} (quit)")
    owned = _probe_owned_hotkeys(record_shield_hotkeys)
    if owned:
        log.warning(
            f"Another program owns {', '.join(owned)}. Releasing the other modifier(s) of "
            f"{HOTKEY} first while recording will trigger that program instead; release "
            f"{HOTKEY.split('+')[-1]} first, or pick a record hotkey whose subsets are free."
        )

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

    hotkeys.stop()
    if tray_icon:
        tray_icon.stop()
    log.info("Goodbye")


if __name__ == "__main__":
    main()

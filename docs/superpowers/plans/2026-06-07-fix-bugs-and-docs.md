# Mumble Bug Fixes + Documentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three bugs in `mumble.py` (lost config-load log line, transcription-vs-new-recording race, missing error handling) and bring `README.md` / `CLAUDE.md` in line with the current feature set.

**Architecture:** `mumble.py` is a single-file Windows app with module-level initialization (paths → config → logging → globals) and background threads (hotkey, audio callback, transcription, timeout timer) sharing module globals. Fixes are surgical: reorder the logging init so the handler exists before the first log call; add a `transcribing` flag plus a pure `decide_toggle_action()` state-machine function to serialize record/transcribe; wrap stream-open and transcription in error handling that resets state and beeps. A minimal `pytest` setup is added solely to host the state-machine and error-path tests.

**Tech Stack:** Python 3.11+, faster-whisper, sounddevice, pystray, keyboard, pynput, pytest (new dev dependency).

---

## Background for the implementing engineer

You have **no prior context** on this repo. Key facts:

- The entire app is one file: `C:\repos\mumble\mumble.py` (~500 lines).
- It runs **only on Windows** (uses `winsound`, `pystray`, `keyboard`). You are executing on Windows; the shell is **PowerShell**.
- There is currently **no test framework**. This plan adds `pytest` and a `tests/` directory.
- Run tests with: `python -m pytest` (from `C:\repos\mumble`).
- "Dev mode" means `mumble.toml` sits next to `mumble.py` (it does, in this repo), so `mumble.log` is written to the repo root, not `~/.mumble/`.
- Do **not** restructure module-level init into functions. The bug-1 fix is a *reorder only*. A larger refactor is explicitly out of scope.
- Importing the module (`python -c "import mumble"`) is safe — `main()` is guarded by `if __name__ == "__main__"` — but it does run all module-level init (loads config, attaches the log handler, instantiates a `pynput` controller). This is intended and is used by the tests and verification step.

The three bugs, precisely:

1. **Lost config-load log line.** `_load_config()` calls `logging.getLogger("mumble").info(...)`/`.warning(...)` at the `_get = _load_config()` line, but the `RotatingFileHandler` is attached *later* in the file. So the "Config loaded from ..." line never reaches `mumble.log`. Fix: move the logging setup (and the `LOG_FILE` definition it depends on) **above** the config load. `LOG_FILE` depends only on `_log_dir()` (no config), so the reorder is safe.

2. **Race: new recording started while a transcription is still running.** CPU Whisper transcription takes seconds. If the record hotkey is pressed again before `stop_recording_and_transcribe()` finishes, `start_recording()` resets the shared global `audio_chunks = []` out from under the in-flight transcription. Fix: add a `transcribing` flag; ignore the record hotkey (busy beep) while it is set; reset it in a `finally`.

3. **No error handling around stream open / transcription.** If `sd.InputStream(...).start()` throws, the app is wedged in a fake "recording" state (tray red, no audio). If `model.transcribe(...)` throws, the daemon thread dies silently with no feedback. Fix: try/except that logs, resets state, and beeps.

---

## File Structure

- `C:\repos\mumble\mumble.py` — modify (all three bug fixes).
- `C:\repos\mumble\pyproject.toml` — modify (add `pytest` dev dependency + pytest config).
- `C:\repos\mumble\tests\test_mumble.py` — create (state-machine + error-path tests).
- `C:\repos\mumble\README.md` — modify (document `max_duration`, `[commands]` trigger, voice commands).
- `C:\repos\mumble\CLAUDE.md` — modify (tests, voice commands, timeout, concurrency model).

---

## Task 1: Add minimal pytest setup

**Files:**
- Modify: `C:\repos\mumble\pyproject.toml`

- [ ] **Step 1: Add a `dev` optional-dependency group**

In `pyproject.toml`, find this block (ends at line ~22):

```toml
dependencies = [
    "faster-whisper",
    "keyboard",
    "pynput",
    "pystray",
    "Pillow",
    "numpy",
    "sounddevice",
    "scipy",
    "pyperclip",
]
```

Immediately **after** that closing `]`, add:

```toml

[project.optional-dependencies]
dev = ["pytest"]
```

- [ ] **Step 2: Add pytest config pinning collection to `tests/`**

At the **end** of `pyproject.toml`, after the existing `[tool.hatch.build.targets.wheel]` block, add:

```toml

[tool.pytest.ini_options]
testpaths = ["tests"]
```

> Why `testpaths`: the repo root contains `test_mic.py`, an interactive hardware script that matches pytest's `test_*.py` collection pattern. Pinning collection to `tests/` prevents pytest from importing it.

- [ ] **Step 3: Install pytest**

Run: `python -m pip install pytest`
Expected: pytest installs (or "Requirement already satisfied").

- [ ] **Step 4: Verify pytest runs (no tests yet)**

Run: `python -m pytest`
Expected: exit code 5 / "no tests ran" — this is fine; it confirms pytest is installed and `testpaths` is honored (no error about `test_mic.py`).

- [ ] **Step 5: Commit**

```powershell
git add pyproject.toml
git commit -m "build: add pytest dev dependency and config"
```

---

## Task 2: Bug 1 — reorder logging init so the config-load line is captured

**Files:**
- Modify: `C:\repos\mumble\mumble.py`

This task **moves** the `LOG_FILE` definition and the logging-handler setup so they run **before** `_get = _load_config()`. Three edits: insert the logging block before `# --- Config ---`, then delete the two old locations.

- [ ] **Step 1: Insert the logging block before the Config section**

Find:

```python
# --- Config ---
_DEFAULTS = {
```

Replace with:

```python
# --- Logging ---
LOG_FILE = os.path.join(_log_dir(), "mumble.log")

log = logging.getLogger("mumble")
log.setLevel(logging.INFO)
handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=1,
)
handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(handler)


# --- Config ---
_DEFAULTS = {
```

- [ ] **Step 2: Delete the now-duplicate `LOG_FILE` line**

Find:

```python
COMMAND_TRIGGER = _get("commands", "trigger")

LOG_FILE = os.path.join(_log_dir(), "mumble.log")

# --- Slash command trigger aliases ---
```

Replace with:

```python
COMMAND_TRIGGER = _get("commands", "trigger")

# --- Slash command trigger aliases ---
```

- [ ] **Step 3: Delete the old logging block**

Find:

```python
    return variants

# --- Logging ---
log = logging.getLogger("mumble")
log.setLevel(logging.INFO)
handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=1_000_000, backupCount=1,
)
handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(handler)

# --- Globals ---
```

Replace with:

```python
    return variants

# --- Globals ---
```

- [ ] **Step 4: Verify the file still imports and the log line now lands**

Run:

```powershell
Remove-Item -ErrorAction SilentlyContinue C:\repos\mumble\mumble.log
python -c "import mumble"
```

Expected: import completes with no traceback.

Then check the log:

Run: `Select-String -Path C:\repos\mumble\mumble.log -Pattern "Config loaded from"`
Expected: one matching line is printed (this is the bug-1 proof — before the fix this line was lost).

- [ ] **Step 5: Commit**

```powershell
git add mumble.py
git commit -m "fix: attach log handler before config load so config-load line is recorded"
```

---

## Task 3: Bugs 2 & 3 — serialize record/transcribe and add error handling

**Files:**
- Modify: `C:\repos\mumble\mumble.py`
- Create: `C:\repos\mumble\tests\test_mumble.py`

These two bugs touch the same functions (`start_recording`, `toggle_recording`, `stop_recording_and_transcribe`), so they are done together to avoid conflicting edits. The steps below show the **final** state of each function.

- [ ] **Step 1: Write the failing test for the toggle state machine**

Create `C:\repos\mumble\tests\test_mumble.py`:

```python
"""Tests for mumble's pure decision logic and error-path state resets."""

import mumble


def test_decide_toggle_action_idle_starts():
    assert mumble.decide_toggle_action(recording=False, transcribing=False) == "start"


def test_decide_toggle_action_recording_stops():
    assert mumble.decide_toggle_action(recording=True, transcribing=False) == "stop"


def test_decide_toggle_action_transcribing_ignores():
    assert mumble.decide_toggle_action(recording=False, transcribing=True) == "ignore"


def test_start_recording_returns_false_and_clears_stream_on_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(mumble.sd, "InputStream", boom)
    mumble.input_stream = "sentinel"  # ensure it gets cleared back to None
    assert mumble.start_recording() is False
    assert mumble.input_stream is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_mumble.py -v`
Expected: FAIL — `AttributeError: module 'mumble' has no attribute 'decide_toggle_action'` (and `start_recording` currently returns `None`, not `False`).

- [ ] **Step 3: Add the `transcribing` global**

Find:

```python
# --- Globals ---
recording = False
audio_chunks = []
last_transcription = None
```

Replace with:

```python
# --- Globals ---
recording = False
transcribing = False
audio_chunks = []
last_transcription = None
```

- [ ] **Step 4: Make `start_recording` return success/failure and handle stream errors**

Find:

```python
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
```

Replace with:

```python
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
```

- [ ] **Step 5: Wrap `stop_recording_and_transcribe` in try/except/finally**

Find the entire current function:

```python
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

    global last_transcription
    last_transcription = text
    paste_text(text)
```

Replace with:

```python
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
```

> Note: `last_transcription` and `transcribing` are now declared in the single `global` statement at the top of the function; the old inline `global last_transcription` before the assignment has been removed.

- [ ] **Step 6: Add `decide_toggle_action` and rewrite `toggle_recording`**

Find:

```python
def toggle_recording():
    """Toggle recording on/off on hotkey press."""
    global recording, recording_timer

    if not recording:
        recording = True
        log.info("Recording...")
        winsound.Beep(1000, 150)
        update_tray(True)
        start_recording()
        recording_timer = threading.Timer(MAX_DURATION, _timeout_cancel)
        recording_timer.daemon = True
        recording_timer.start()
    else:
        if recording_timer is not None:
            recording_timer.cancel()
            recording_timer = None
        recording = False
        log.info("Stopped, transcribing...")
        winsound.Beep(500, 150)
        threading.Thread(target=stop_recording_and_transcribe, daemon=True).start()
```

Replace with:

```python
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
```

> Behavior note for reviewers: `cancel_recording()` returns early when `recording` is False, so it remains a no-op during transcription (you cannot cancel an in-flight transcription) — this matches the pre-existing behavior and is intentional. The `transcribing` flag is set in the stop branch *before* the thread is spawned and cleared in the `finally` of `stop_recording_and_transcribe`, so the no-op window is fully covered.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_mumble.py -v`
Expected: 4 passed.

- [ ] **Step 8: Verify the module still imports cleanly**

Run: `python -c "import mumble"`
Expected: no traceback.

- [ ] **Step 9: Commit**

```powershell
git add mumble.py tests/test_mumble.py
git commit -m "fix: serialize record/transcribe with transcribing flag and handle audio/transcription errors"
```

---

## Task 4: Update README.md

**Files:**
- Modify: `C:\repos\mumble\README.md`

The README omits `audio.max_duration`, the `[commands]` trigger, and the voice-command features entirely.

- [ ] **Step 1: Document the recording timeout under "How it works"**

Find:

```markdown
1. **Record** — Audio is captured from your microphone when you press the hotkey. The tray icon turns red while recording.
```

Replace with:

```markdown
1. **Record** — Audio is captured from your microphone when you press the hotkey. The tray icon turns red while recording. A recording automatically cancels after `max_duration` seconds (default 90) so a forgotten recording can't run forever.
```

- [ ] **Step 2: Add a "Voice commands" section after the Post-processing section**

Find:

```markdown
- **Double-space collapse** — Runs of multiple spaces are reduced to a single space.

## Requirements
```

Replace with:

```markdown
- **Double-space collapse** — Runs of multiple spaces are reduced to a single space.

## Voice commands

Some spoken phrases are interpreted as commands instead of being pasted as text:

- **Slash commands** — Say the trigger word (`claude` by default) followed by a single word, and Mumble pastes a slash command instead of the literal words. For example, saying "claude clear" pastes `/clear`. Common mishearings of "claude" (clod, clawed, cloud, claud, clogged, klaud, klaude) are also accepted. Change the trigger word with `commands.trigger` in your config.
- **show help** — Saying "show help" opens this README on GitHub instead of pasting anything.

## Requirements
```

- [ ] **Step 3: Add `max_duration` and `[commands]` to the config example**

Find:

```toml
[audio]
device_name = "C200"
```

Replace with:

```toml
[audio]
device_name = "C200"
max_duration = 90

[commands]
trigger = "claude"
```

- [ ] **Step 4: Add the two new keys to the config table**

Find:

```markdown
| `audio.device_name` | `C200` | Mic name substring to search for |
```

Replace with:

```markdown
| `audio.device_name` | `C200` | Mic name substring to search for |
| `audio.max_duration` | `90` | Auto-cancel a recording after this many seconds |
| `commands.trigger` | `claude` | Spoken word that prefixes a voice slash command |
```

- [ ] **Step 5: Commit**

```powershell
git add README.md
git commit -m "docs: document recording timeout, voice commands, and new config keys"
```

---

## Task 5: Update CLAUDE.md

**Files:**
- Modify: `C:\repos\mumble\CLAUDE.md`

- [ ] **Step 1: Update the tests/linting line**

Find:

```markdown
Build system is hatchling (configured in `pyproject.toml`). No tests or linting are configured.
```

Replace with:

```markdown
Build system is hatchling (configured in `pyproject.toml`). Tests live in `tests/` and run via `python -m pytest`. Linting is not configured.
```

- [ ] **Step 2: Expand the post-processing architecture bullet to mention voice commands**

Find:

```markdown
4. **Post-processing** — Digit collapse (`1 2 3 4` → `1234`), double-space collapse. Applied in `stop_recording_and_transcribe()`.
```

Replace with:

```markdown
4. **Post-processing** — Digit collapse (`1 2 3 4` → `1234`), double-space collapse. Voice commands: the trigger word (`claude`, plus homophone aliases) + one word becomes a slash command (`/clear`); "show help" opens the README. Applied in `stop_recording_and_transcribe()`.
```

- [ ] **Step 3: Document the timeout and concurrency model**

Find:

```markdown
Hotkeys are registered globally via the `keyboard` module. All recording/transcription runs on background threads; `shutdown_event` coordinates clean exit.
```

Replace with:

```markdown
Hotkeys are registered globally via the `keyboard` module. All recording/transcription runs on background threads; `shutdown_event` coordinates clean exit. A recording auto-cancels after `audio.max_duration` seconds (default 90) via a `threading.Timer`. A `transcribing` flag guards against starting a new recording while a transcription is still in flight; `decide_toggle_action()` encodes the start/stop/ignore state machine.
```

- [ ] **Step 4: Add a key-decision bullet about concurrency**

Find:

```markdown
- Windows-only: uses `winsound` for beeps, `pystray` for tray, `keyboard` for global hotkeys.
```

Replace with:

```markdown
- Windows-only: uses `winsound` for beeps, `pystray` for tray, `keyboard` for global hotkeys.
- Concurrency: `recording`, `transcribing`, and `audio_chunks` are module globals shared across the hotkey, audio-callback, transcription, and timeout threads. Rather than locking, the `transcribing` flag makes the record hotkey a no-op while a transcription runs, preventing a new recording from clearing `audio_chunks` mid-transcription.
```

- [ ] **Step 5: Commit**

```powershell
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for tests, voice commands, timeout, and concurrency model"
```

---

## Task 6: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Full test run**

Run: `python -m pytest`
Expected: 4 passed.

- [ ] **Step 2: Bug-1 end-to-end proof**

Run:

```powershell
Remove-Item -ErrorAction SilentlyContinue C:\repos\mumble\mumble.log
python -c "import mumble"
Select-String -Path C:\repos\mumble\mumble.log -Pattern "Config loaded from"
```

Expected: the "Config loaded from ..." line is printed — confirming the handler is now attached before the config load.

- [ ] **Step 3: Confirm clean git state**

Run: `git status`
Expected: working tree clean (all changes committed across Tasks 1–5).

---

## Self-Review Notes (author)

- **Spec coverage:** Bug 1 → Task 2 + Task 6 Step 2. Bug 2 → Task 3 (Steps 3, 6) + test Steps 1–2,7. Bug 3 → Task 3 (Steps 4, 5) + test Step 1 case 4. Docs → Tasks 4 (README) and 5 (CLAUDE.md), covering all four gaps: `max_duration`, `[commands]`/`trigger`, voice slash commands, `show help`.
- **No placeholders:** every code step shows complete before/after text.
- **Type/name consistency:** `decide_toggle_action(recording, transcribing)` is defined in Task 3 Step 6 and called there and tested in Task 3 Step 1 with matching keyword args. `transcribing` global added in Step 3, set in `toggle_recording` (Step 6), cleared in `stop_recording_and_transcribe` `finally` (Step 5). `start_recording` returns `bool` (Step 4) and its caller checks it (Step 6).
- **Out of scope (intentional):** module-level init is *not* refactored into functions; clipboard is not preserved/restored; the PowerShell-interpolation note in `_install_startup` is not addressed. These were lower-severity review findings, not part of this plan.

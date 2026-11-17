# Mic Reconnect on Failure — Implementation Plan

**Goal:** When the mic fails to open (e.g. after sleep/wake, a dock reconnect or a USB replug re-enumerated audio devices), Mumble re-scans audio devices, finds the configured mic again by name, and retries once. If the retry also fails, show a Windows tray notification instead of failing silently.

**Evidence:** 2026-11-17 08:24/08:25 — two record presses logged `Failed to start audio stream` (`PortAudioError ... DirectSound error -2005401480`, 0x88780078). Mumble had picked the C200 as device **#9** at startup on 2026-11-16 11:47; a fresh scan now lists it as **#4**. PortAudio (via sounddevice) enumerates devices only at initialisation, so the running process kept a stale device.

**Design:**
- `_refresh_audio_devices()`: re-initialise PortAudio (`sd._terminate()` then `sd._initialize()` — private sounddevice functions, the documented way to force re-enumeration), then call `find_c200_device()` and update module globals `device_index` / `record_rate`. If the mic is not found, set `device_index = None` (system default mic, same as startup) and leave `record_rate` unchanged.
- Re-initialising is only safe with no stream open. `start_recording()` is the only caller and runs only when not recording, after `input_stream` has been closed, so this holds.
- `start_recording()`: try to open the stream; on failure log a warning, call `_refresh_audio_devices()`, try once more. On second failure log the exception, call `_notify(...)`, clear `input_stream`, return False (the caller already beeps low).
- `_notify(message)`: show a tray balloon via `tray_icon.notify(message, "Mumble")` (pystray supports this on Windows). No-op when there is no tray icon; any exception from the notification is logged and swallowed — it must never break recording.
- A successful retry is not notified (the start beep already says it worked); `find_c200_device()` logs `Found C200: [N] …` so the log records the reconnect.
- Notification text: `Microphone unavailable. Check that it's connected, then try again.`

**Branch:** `fix/mic-reconnect` off `master`. Merge locally to `master` at the end. Mumble is run by the user from their own shell: tell them to restart it; do not restart it from this session.

**Interpreter:** `"$LOCALAPPDATA/Programs/Python/Python313/python.exe"` (PATH `python` is a broken stub). All commands run from `C:\repos\mumble` in Git Bash. Current suite: 63 passed.

---

## Task 1: `_notify` and `_refresh_audio_devices` helpers

**Files:** `mumble.py`, `tests/test_mumble.py`

- [ ] **Step 1: Write failing tests.** Append to the end of `tests/test_mumble.py`:

```python
# --- Mic reconnect ---

class _FakeTray:
    def __init__(self, fail=False):
        self.notes = []
        self.fail = fail

    def notify(self, message, title=None):
        if self.fail:
            raise OSError("shell busy")
        self.notes.append((message, title))


def test_notify_shows_tray_balloon(monkeypatch):
    tray = _FakeTray()
    monkeypatch.setattr(mumble, "tray_icon", tray)
    mumble._notify("hello")
    assert tray.notes == [("hello", "Mumble")]


def test_notify_without_tray_is_noop(monkeypatch):
    monkeypatch.setattr(mumble, "tray_icon", None)
    mumble._notify("hello")  # must not raise


def test_notify_swallows_tray_errors(monkeypatch):
    monkeypatch.setattr(mumble, "tray_icon", _FakeTray(fail=True))
    mumble._notify("hello")  # must not raise


def test_refresh_audio_devices_reinitialises_and_finds_mic(monkeypatch):
    calls = []
    monkeypatch.setattr(mumble.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(mumble.sd, "_initialize", lambda: calls.append("initialize"))
    monkeypatch.setattr(mumble, "find_c200_device", lambda: (calls.append("find"), (4, 44100))[1])
    monkeypatch.setattr(mumble, "device_index", 9)
    monkeypatch.setattr(mumble, "record_rate", 48000)
    mumble._refresh_audio_devices()
    assert calls == ["terminate", "initialize", "find"]
    assert mumble.device_index == 4
    assert mumble.record_rate == 44100


def test_refresh_audio_devices_falls_back_to_default_mic(monkeypatch):
    monkeypatch.setattr(mumble.sd, "_terminate", lambda: None)
    monkeypatch.setattr(mumble.sd, "_initialize", lambda: None)
    monkeypatch.setattr(mumble, "find_c200_device", lambda: (None, None))
    monkeypatch.setattr(mumble, "device_index", 9)
    monkeypatch.setattr(mumble, "record_rate", 48000)
    mumble._refresh_audio_devices()
    assert mumble.device_index is None
    assert mumble.record_rate == 48000
```

- [ ] **Step 2: Run, expect failure.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q tests/test_mumble.py -k "notify or refresh_audio_devices"
```
Expected: 5 failed, `AttributeError: module 'mumble' has no attribute '_notify'` / `'_refresh_audio_devices'`.

- [ ] **Step 3: Implement.** In `mumble.py`:

(a) Directly after the `update_tray` function (before `def open_log`), insert:
```python
def _notify(message):
    """Show a tray balloon notification. Never raises."""
    if tray_icon is None:
        return
    try:
        tray_icon.notify(message, "Mumble")
    except Exception:
        log.exception("Failed to show notification")


```

(b) Directly after the `find_c200_device` function (before `def audio_callback`), insert:
```python
def _refresh_audio_devices():
    """Re-scan audio devices and re-select the configured mic.

    PortAudio enumerates devices only when initialised, so after sleep/wake or
    a USB replug the saved device index can point at a device that no longer
    exists. Re-initialising forces a fresh scan. Only call with no stream open.
    """
    global device_index, record_rate
    sd._terminate()
    sd._initialize()
    index, rate = find_c200_device()
    if index is not None:
        device_index, record_rate = index, rate
    else:
        device_index = None
        log.info("C200 not found — using system default mic")


```

- [ ] **Step 4: Run, expect pass.** Same command as Step 2. Expected: 5 passed.

- [ ] **Step 5: Full suite.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q
```
Expected: 68 passed.

- [ ] **Step 6: Commit** (message ends with a blank line and a `Co-Authored-By:` trailer naming the implementing model).
```bash
git add mumble.py tests/test_mumble.py
git commit -m "feat: add helpers to re-scan audio devices and show tray notifications"
```

---

## Task 2: Retry a failed mic open once after re-scanning; notify if it still fails

**Files:** `mumble.py`, `tests/test_mumble.py`

Existing items this task uses: `_FakeTray` (added in Task 1, `tests/test_mumble.py`); `_refresh_audio_devices()` and `_notify(message)` (Task 1, `mumble.py`).

- [ ] **Step 1: Update the existing test and write failing tests.**

(a) In `tests/test_mumble.py`, replace the existing test
```python
def test_start_recording_returns_false_and_clears_stream_on_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(mumble.sd, "InputStream", boom)
    mumble.input_stream = "sentinel"  # ensure it gets cleared back to None
    assert mumble.start_recording() is False
    assert mumble.input_stream is None
```
with
```python
def test_start_recording_returns_false_and_clears_stream_on_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no audio device")

    monkeypatch.setattr(mumble.sd, "InputStream", boom)
    monkeypatch.setattr(mumble, "_refresh_audio_devices", lambda: None)
    monkeypatch.setattr(mumble, "_notify", lambda message: None)
    mumble.input_stream = "sentinel"  # ensure it gets cleared back to None
    assert mumble.start_recording() is False
    assert mumble.input_stream is None
```

(b) Append to the end of `tests/test_mumble.py`:
```python
class _FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def start(self):
        pass


def _stream_factory(failures):
    """sd.InputStream stand-in that raises for the first `failures` calls."""
    opened = []

    def make(**kwargs):
        opened.append(kwargs)
        if len(opened) <= failures:
            raise RuntimeError("DirectSound error")
        return _FakeStream(**kwargs)

    return make, opened


def test_start_recording_no_refresh_when_mic_opens(monkeypatch):
    make, opened = _stream_factory(failures=0)
    refreshes = []
    monkeypatch.setattr(mumble.sd, "InputStream", make)
    monkeypatch.setattr(mumble, "_refresh_audio_devices", lambda: refreshes.append(1))
    monkeypatch.setattr(mumble, "input_stream", None)
    assert mumble.start_recording() is True
    assert len(opened) == 1
    assert refreshes == []


def test_start_recording_retries_after_refresh(monkeypatch):
    make, opened = _stream_factory(failures=1)
    notes = []
    monkeypatch.setattr(mumble.sd, "InputStream", make)
    monkeypatch.setattr(mumble, "device_index", 9)
    monkeypatch.setattr(mumble, "record_rate", 48000)

    def refresh():
        mumble.device_index, mumble.record_rate = 4, 44100

    monkeypatch.setattr(mumble, "_refresh_audio_devices", refresh)
    monkeypatch.setattr(mumble, "_notify", notes.append)
    monkeypatch.setattr(mumble, "input_stream", None)
    assert mumble.start_recording() is True
    assert [o["device"] for o in opened] == [9, 4], "retry uses the re-scanned device"
    assert opened[1]["samplerate"] == 44100
    assert notes == [], "a successful retry is not notified"


def test_start_recording_notifies_when_retry_fails(monkeypatch):
    make, opened = _stream_factory(failures=2)
    refreshes, notes = [], []
    monkeypatch.setattr(mumble.sd, "InputStream", make)
    monkeypatch.setattr(mumble, "_refresh_audio_devices", lambda: refreshes.append(1))
    monkeypatch.setattr(mumble, "_notify", notes.append)
    monkeypatch.setattr(mumble, "input_stream", "sentinel")
    assert mumble.start_recording() is False
    assert len(opened) == 2, "exactly one retry"
    assert refreshes == [1]
    assert notes == ["Microphone unavailable. Check that it's connected, then try again."]
    assert mumble.input_stream is None
```

- [ ] **Step 2: Run, expect failure.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q tests/test_mumble.py -k start_recording
```
Expected: `test_start_recording_retries_after_refresh` and `test_start_recording_notifies_when_retry_fails` fail (only one open attempt, no refresh); `test_start_recording_no_refresh_when_mic_opens` and the updated `test_start_recording_returns_false_and_clears_stream_on_error` pass.

- [ ] **Step 3: Implement.** In `mumble.py`, replace the whole `start_recording` function:
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
with
```python
MIC_UNAVAILABLE_MESSAGE = "Microphone unavailable. Check that it's connected, then try again."


def _open_input_stream():
    """Open and start a mic stream on the current device_index / record_rate."""
    kwargs = {
        "samplerate": record_rate,
        "channels": 1,
        "dtype": "float32",
        "callback": audio_callback,
        "blocksize": 1024,
    }
    if device_index is not None:
        kwargs["device"] = device_index
    stream = sd.InputStream(**kwargs)
    stream.start()
    return stream


def start_recording():
    """Open the mic stream via sounddevice. Returns True on success, False on failure.

    If the first open fails (typically a stale device after sleep/wake or a
    replug), re-scan audio devices and retry once before giving up.
    """
    global audio_chunks, input_stream
    audio_chunks = []

    try:
        input_stream = _open_input_stream()
    except Exception as first_error:
        log.warning(f"Failed to open mic ({first_error}); re-scanning audio devices and retrying")
        try:
            _refresh_audio_devices()
            input_stream = _open_input_stream()
        except Exception:
            log.exception("Failed to start audio stream")
            input_stream = None
            _notify(MIC_UNAVAILABLE_MESSAGE)
            return False
    log.info(f"Recording started ({record_rate} Hz)")
    return True
```

- [ ] **Step 4: Run, expect pass.** Same command as Step 2. Expected: 4 passed.

- [ ] **Step 5: Full suite.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q
```
Expected: 71 passed.

- [ ] **Step 6: Commit** (with `Co-Authored-By:` trailer).
```bash
git add mumble.py tests/test_mumble.py
git commit -m "fix: re-scan audio devices and retry once when the mic fails to open"
```

---

## Task 3: Docs

**Files:** `README.md`, `CLAUDE.md`

- [ ] **Step 1: `README.md`** — in `## How it works`, item 1 (**Record**), append this sentence to the end of the paragraph: `If the microphone fails to open (for example after sleep or reconnecting it), Mumble re-scans audio devices and tries once more; if that also fails it beeps low and shows a tray notification.`

- [ ] **Step 2: `CLAUDE.md`** — in Architecture item 2 (**Audio**), insert after the sentence `Targets Anker C200 mic by name substring, falls back to system default.` the sentence: `PortAudio enumerates devices only at initialisation, so if opening the stream fails (stale device after sleep/wake or a replug), `start_recording()` calls `_refresh_audio_devices()` (re-initialises sounddevice and re-finds the mic) and retries once, then shows a tray notification via `_notify()` if it still fails.`

- [ ] **Step 3: Commit** (with `Co-Authored-By:` trailer).
```bash
git add README.md CLAUDE.md
git commit -m "docs: describe mic re-scan and retry"
```

---

## Finish

- [ ] Full suite passes (71).
- [ ] Merge `fix/mic-reconnect` into `master` locally (`git checkout master && git merge --ff-only fix/mic-reconnect`), delete the branch.
- [ ] Tell the user to restart their Mumble (it runs from their own shell).

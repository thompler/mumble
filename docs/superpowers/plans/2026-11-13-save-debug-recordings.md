# Save Debug Recordings — Implementation Plan

**Goal:** When `[debug] save_recordings = true`, write the audio Whisper sees for each recording to a WAV file, and keep only the 10 newest, so a bad transcription can be replayed and compared across models.

**Design:**
- Config key `debug.save_recordings` (bool, default `false`).
- Files go in `<log dir>/recordings/` (repo dir in dev mode, `~/.mumble/` when installed). `*.wav` is already gitignored; add `recordings/` too.
- Saved audio is the 16 kHz, post-resample array passed to `model.transcribe` — exactly what the model heard. Written as 16-bit PCM mono.
- Filename `rec-YYYYMMDD-HHMMSS-mmm.wav` (lexically sortable). After each save, delete the oldest `rec-*.wav` files beyond 10. Other files in the folder are never touched.
- Saving happens *before* transcription, so a recording that crashes the transcriber is still kept. The filename is logged (`Saved recording: rec-….wav`) right before `Transcribed: …`, so log lines and files can be matched.
- A save failure is logged and swallowed; it never blocks the transcription or paste.

**Branch:** `feat/save-debug-recordings` off `master`. Merge locally to `master` at the end, then restart the running `python mumble.py`.

**Interpreter:** `"$LOCALAPPDATA/Programs/Python/Python313/python.exe"` (PATH `python` is a broken stub). All commands run from `C:\repos\mumble` in Git Bash.

---

## Task 1: `_save_recording` helper (write + prune)

**Files:** `mumble.py`, `tests/test_mumble.py`

- [ ] **Step 1: Write failing tests.** Append to the end of `tests/test_mumble.py`:

```python
# --- Debug recordings ---

def test_save_recording_writes_16bit_wav(tmp_path):
    from scipy.io import wavfile
    audio = np.linspace(-0.5, 0.5, 16000, dtype=np.float32)
    path = mumble._save_recording(audio, 16000, str(tmp_path / "recordings"))
    rate, data = wavfile.read(path)
    assert rate == 16000
    assert data.dtype == np.int16
    assert len(data) == 16000
    assert os.path.basename(path).startswith("rec-") and path.endswith(".wav")


def test_save_recording_keeps_only_newest(tmp_path):
    d = tmp_path / "recordings"
    d.mkdir()
    for i in range(12):
        (d / f"rec-20000101-0000{i:02d}-000.wav").write_bytes(b"old")
    (d / "notes.txt").write_text("keep me")
    path = mumble._save_recording(np.zeros(160, dtype=np.float32), 16000, str(d), keep=10)
    recs = sorted(p.name for p in d.glob("rec-*.wav"))
    assert len(recs) == 10
    assert recs[-1] == os.path.basename(path), "the new recording is kept"
    assert "rec-20000101-000000-000.wav" not in recs, "oldest pruned first"
    assert (d / "notes.txt").exists(), "non-recording files are left alone"
```

Add `import os` and `import numpy as np` to the imports at the top of `tests/test_mumble.py`, so the import block reads:

```python
import ctypes
import os
import threading
import time

import numpy as np
import pytest

import mumble
```

- [ ] **Step 2: Run, expect failure.**

```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q tests/test_mumble.py -k save_recording
```
Expected: 2 failed, `AttributeError: module 'mumble' has no attribute '_save_recording'`.

- [ ] **Step 3: Implement.** In `mumble.py`:

Add to the imports (after `from math import gcd`):
```python
from datetime import datetime
```
and after `from scipy.signal import resample_poly`:
```python
from scipy.io import wavfile
```

Insert directly after the `_log_dir()` function (before `# --- Logging ---`):
```python
RECORDINGS_DIR = os.path.join(_log_dir(), "recordings")
RECORDINGS_KEEP = 10


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
        os.remove(os.path.join(directory, old))
    return path
```

- [ ] **Step 4: Run, expect pass.** Same command as Step 2. Expected: 2 passed.

- [ ] **Step 5: Full suite.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q
```
Expected: 46 passed.

- [ ] **Step 6: Commit.**
```bash
git add mumble.py tests/test_mumble.py
git commit -m "feat: add _save_recording helper that writes a WAV and keeps the newest 10"
```

---

## Task 2: `debug.save_recordings` config key, wired into transcription

**Files:** `mumble.py`, `tests/test_mumble.py`

- [ ] **Step 1: Write failing tests.** Append to `tests/test_mumble.py`:

```python
class _Seg:
    def __init__(self, text):
        self.text = text


class _FakeModel:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        return [_Seg("hello there")], None


def _stub_transcription(monkeypatch):
    """Set up stop_recording_and_transcribe to run on 1 s of 16 kHz silence."""
    pasted = []
    monkeypatch.setattr(mumble, "hotkeys", None)
    monkeypatch.setattr(mumble, "input_stream", None)
    monkeypatch.setattr(mumble, "audio_chunks", [np.zeros((16000, 1), dtype=np.float32)])
    monkeypatch.setattr(mumble, "record_rate", 16000)
    monkeypatch.setattr(mumble, "update_tray", lambda s: None)
    monkeypatch.setattr(mumble, "model", _FakeModel())
    monkeypatch.setattr(mumble, "paste_text", pasted.append)
    return pasted


def test_default_save_recordings_is_off():
    assert mumble._DEFAULTS["debug"]["save_recordings"] is False


def test_transcription_saves_recording_when_enabled(monkeypatch, caplog):
    import logging
    pasted = _stub_transcription(monkeypatch)
    saved = []
    monkeypatch.setattr(mumble, "SAVE_RECORDINGS", True)
    monkeypatch.setattr(mumble, "_save_recording",
                        lambda audio, rate, directory: saved.append((len(audio), rate, directory)) or "x/rec-1.wav")
    with caplog.at_level(logging.INFO, logger="mumble"):
        mumble.stop_recording_and_transcribe()
    assert saved == [(16000, 16000, mumble.RECORDINGS_DIR)]
    assert "Saved recording: rec-1.wav" in caplog.text
    assert pasted == ["hello there"]


def test_transcription_skips_saving_when_disabled(monkeypatch):
    _stub_transcription(monkeypatch)
    saved = []
    monkeypatch.setattr(mumble, "SAVE_RECORDINGS", False)
    monkeypatch.setattr(mumble, "_save_recording", lambda *a, **k: saved.append(a))
    mumble.stop_recording_and_transcribe()
    assert saved == []


def test_save_failure_does_not_block_paste(monkeypatch):
    pasted = _stub_transcription(monkeypatch)
    monkeypatch.setattr(mumble, "SAVE_RECORDINGS", True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mumble, "_save_recording", boom)
    mumble.stop_recording_and_transcribe()
    assert pasted == ["hello there"]
```

- [ ] **Step 2: Run, expect failure.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q tests/test_mumble.py -k "save_recordings or saves_recording or saving or save_failure"
```
Expected: failures with `KeyError: 'debug'` and `AttributeError: ... no attribute 'SAVE_RECORDINGS'` (the disabled test may error the same way).

- [ ] **Step 3: Implement.** In `mumble.py`:

In `_DEFAULTS`, after the `"commands"` entry, add a `"debug"` section so the end of the dict reads:
```python
    "commands": {
        "trigger": "claude",
    },
    "debug": {
        "save_recordings": False,
    },
}
```

After the line `COMMAND_TRIGGER = _get("commands", "trigger")`, add:
```python
SAVE_RECORDINGS = bool(_get("debug", "save_recordings"))
```

In `stop_recording_and_transcribe()`, replace:
```python
        log.info("Transcribing...")
        segments, _info = model.transcribe(audio_np, language="en", beam_size=5)
```
with:
```python
        if SAVE_RECORDINGS:
            try:
                path = _save_recording(audio_np, WHISPER_RATE, RECORDINGS_DIR)
                log.info(f"Saved recording: {os.path.basename(path)}")
            except Exception:
                log.exception("Failed to save recording")

        log.info("Transcribing...")
        segments, _info = model.transcribe(audio_np, language="en", beam_size=5)
```

- [ ] **Step 4: Run, expect pass.** Same command as Step 2. Expected: 4 passed.

- [ ] **Step 5: Full suite.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q
```
Expected: 50 passed.

- [ ] **Step 6: Commit.**
```bash
git add mumble.py tests/test_mumble.py
git commit -m "feat: save the last 10 recordings as WAVs when debug.save_recordings is on"
```

---

## Task 3: Docs, example config, gitignore

**Files:** `README.md`, `mumble.toml.example`, `.gitignore`, `CLAUDE.md`

- [ ] **Step 1: `mumble.toml.example`** — append at the end of the file:
```toml

[debug]
save_recordings = false
```

- [ ] **Step 2: `README.md`** — in the example config block (the fenced `toml` block ending with `trigger = "claude"`), add two lines after `trigger = "claude"` and before the closing fence:
```toml

[debug]
save_recordings = false
```
and in the config table, after the `commands.trigger` row, add:
```markdown
| `debug.save_recordings` | `false` | Save the audio Whisper hears for the last 10 recordings as WAV files in `recordings/` next to `mumble.log`. The log line `Saved recording: rec-….wav` precedes each transcription |
```

- [ ] **Step 3: `.gitignore`** — add a line `recordings/` after `*.wav`.

- [ ] **Step 4: `CLAUDE.md`** — in Architecture item 3 (Transcription), append the sentence: `When debug.save_recordings is on, the resampled audio is written to recordings/rec-*.wav (newest 10 kept) before transcription.`

- [ ] **Step 5: Commit.**
```bash
git add README.md mumble.toml.example .gitignore CLAUDE.md
git commit -m "docs: describe debug.save_recordings"
```

---

## Finish

- [ ] Full suite passes (50).
- [ ] Merge `feat/save-debug-recordings` into `master` locally (`git checkout master && git merge --ff-only feat/save-debug-recordings`), delete the branch.
- [ ] Add `[debug]\nsave_recordings = true` to the user's local `mumble.toml` (gitignored).
- [ ] Restart mumble: stop the running `python mumble.py`, start it again in the background, confirm `Ready` in `mumble.log`.

# Whisper Vocabulary Hint — Implementation Plan

**Goal:** Let the user list terms Whisper tends to mishear (e.g. `CPI` heard as `CBI`) in `mumble.toml`; pass them to Whisper as `initial_prompt` on every transcription.

**Evidence:** On `recordings/rec-20261113-114625-842.wav`, `small.en` gave "CBI"; `small.en` with `initial_prompt="CPI, Jira, Claude, connector, kiosk."` gave "CPI" at the same speed (1.44 s vs 1.47 s).

**Design:**
- Config key `whisper.vocabulary`: a TOML list of strings, default `[]` (no hint; behaviour unchanged).
- `_vocabulary_prompt(words)` turns the list into the prompt: strip each entry, drop empty ones, join with `", "`, end with `"."` → `["CPI", "Jira"]` → `"CPI, Jira."`. Empty result → `None` (faster-whisper's default, meaning no prompt).
- A value that is not a list of strings raises `ValueError` naming `whisper.vocabulary`. Validation runs at the top of `_run()`, the same as `hotkeys.record_mode`, so a bad value is logged by `main()`'s fatal-error path and Mumble exits with code 1.
- `_run()` stores the result in module global `INITIAL_PROMPT` and logs `Vocabulary hint: <prompt>` when it is not `None`.
- `stop_recording_and_transcribe()` passes `initial_prompt=INITIAL_PROMPT` to `model.transcribe`.
- Config is read at startup only; changing the list needs a restart.

**Branch:** `feat/whisper-vocabulary` off `master`. Merge locally to `master` at the end, set `vocabulary = ["CPI"]` in the user's local `mumble.toml`, and restart the running `python mumble.py`.

**Interpreter:** `"$LOCALAPPDATA/Programs/Python/Python313/python.exe"` (PATH `python` is a broken stub). All commands run from `C:\repos\mumble` in Git Bash. Current suite: 51 passed.

---

## Task 1: `_vocabulary_prompt` helper

**Files:** `mumble.py`, `tests/test_mumble.py`

- [ ] **Step 1: Write failing tests.** Append to the end of `tests/test_mumble.py`:

```python
# --- Whisper vocabulary hint ---

def test_vocabulary_prompt_empty_is_none():
    assert mumble._vocabulary_prompt([]) is None


def test_vocabulary_prompt_single_term():
    assert mumble._vocabulary_prompt(["CPI"]) == "CPI."


def test_vocabulary_prompt_strips_and_drops_blanks():
    assert mumble._vocabulary_prompt(["CPI", " Jira ", "", "  "]) == "CPI, Jira."


def test_vocabulary_prompt_all_blank_is_none():
    assert mumble._vocabulary_prompt(["", " "]) is None


@pytest.mark.parametrize("bad", ["CPI", ["CPI", 3], None])
def test_vocabulary_prompt_rejects_non_list_of_strings(bad):
    with pytest.raises(ValueError, match="whisper.vocabulary"):
        mumble._vocabulary_prompt(bad)
```

- [ ] **Step 2: Run, expect failure.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q tests/test_mumble.py -k vocabulary_prompt
```
Expected: 7 failed, `AttributeError: module 'mumble' has no attribute '_vocabulary_prompt'`.

- [ ] **Step 3: Implement.** In `mumble.py`, insert directly after the line `RECORD_MODE = _get("hotkeys", "record_mode")` (and its preceding comment block, which stays unchanged):

```python


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
```

- [ ] **Step 4: Run, expect pass.** Same command as Step 2. Expected: 7 passed.

- [ ] **Step 5: Full suite.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q
```
Expected: 58 passed.

- [ ] **Step 6: Commit** (message ends with a blank line and a `Co-Authored-By:` trailer naming the implementing model).
```bash
git add mumble.py tests/test_mumble.py
git commit -m "feat: add _vocabulary_prompt to build a Whisper hint from a word list"
```

---

## Task 2: `whisper.vocabulary` config key, validated at startup, passed to transcribe

**Files:** `mumble.py`, `tests/test_mumble.py`

Existing test helpers this task uses (already in `tests/test_mumble.py`): `_stub_transcription(monkeypatch)` sets up `stop_recording_and_transcribe` on 1 s of 16 kHz silence with a fake model and returns the list of pasted texts; `_Seg(text)` is a fake segment.

- [ ] **Step 1: Write failing tests.** Append to `tests/test_mumble.py`:

```python
def test_default_vocabulary_is_empty():
    assert mumble._DEFAULTS["whisper"]["vocabulary"] == []


def test_transcription_passes_vocabulary_prompt(monkeypatch):
    _stub_transcription(monkeypatch)
    monkeypatch.setattr(mumble, "SAVE_RECORDINGS", False)
    monkeypatch.setattr(mumble, "INITIAL_PROMPT", "CPI.")
    seen = {}

    class _RecordingModel:
        def transcribe(self, audio, **kwargs):
            seen.update(kwargs)
            return [_Seg("hello")], None

    monkeypatch.setattr(mumble, "model", _RecordingModel())
    mumble.stop_recording_and_transcribe()
    assert seen["initial_prompt"] == "CPI."


def _remove_file_handlers():
    import logging
    for h in list(mumble.log.handlers):
        if isinstance(h, logging.FileHandler):
            mumble.log.removeHandler(h)
            h.close()


def test_main_rejects_invalid_vocabulary(monkeypatch, tmp_path, caplog):
    import logging
    monkeypatch.setattr("sys.argv", ["mumble"])
    monkeypatch.setattr(mumble, "LOG_FILE", str(tmp_path / "mumble.log"))
    monkeypatch.setattr(mumble.winsound, "Beep", lambda *a: None)
    monkeypatch.setattr(mumble, "VOCABULARY", "CPI")
    monkeypatch.setattr(mumble, "find_c200_device", lambda: pytest.fail("validation must run before device lookup"))
    try:
        with caplog.at_level(logging.ERROR, logger="mumble"), pytest.raises(SystemExit) as e:
            mumble.main()
        assert e.value.code == 1
        assert "whisper.vocabulary" in caplog.text
    finally:
        _remove_file_handlers()


def test_run_sets_initial_prompt_from_vocabulary(monkeypatch, tmp_path, caplog):
    import logging
    monkeypatch.setattr("sys.argv", ["mumble"])
    monkeypatch.setattr(mumble, "LOG_FILE", str(tmp_path / "mumble.log"))
    monkeypatch.setattr(mumble.winsound, "Beep", lambda *a: None)
    monkeypatch.setattr(mumble, "VOCABULARY", ["CPI"])
    monkeypatch.setattr(mumble, "INITIAL_PROMPT", None)

    def stop_after_validation():
        raise RuntimeError("stop here")

    monkeypatch.setattr(mumble, "find_c200_device", stop_after_validation)
    try:
        with caplog.at_level(logging.INFO, logger="mumble"), pytest.raises(SystemExit):
            mumble.main()
        assert mumble.INITIAL_PROMPT == "CPI."
        assert "Vocabulary hint: CPI." in caplog.text
    finally:
        _remove_file_handlers()
```

- [ ] **Step 2: Run, expect failure.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q tests/test_mumble.py -k "default_vocabulary or passes_vocabulary or invalid_vocabulary or initial_prompt_from_vocabulary"
```
Expected: 4 failed — `KeyError: 'vocabulary'` for the default test, `AttributeError` (no `INITIAL_PROMPT` / `VOCABULARY`) for the others.

- [ ] **Step 3: Implement.** In `mumble.py`:

(a) In `_DEFAULTS["whisper"]`, add the key so the section reads:
```python
    "whisper": {
        "model": "small.en",
        "sample_rate": 16000,
        "vocabulary": [],
    },
```

(b) Directly after the `_vocabulary_prompt` function added in Task 1, add:
```python


# Raw config value; validated at the top of _run() like RECORD_MODE.
VOCABULARY = _get("whisper", "vocabulary")
INITIAL_PROMPT = None  # set by _run() from VOCABULARY
```

(c) In `_run()`, change the `global` line from
```python
    global model, device_index, record_rate, RECORD_MODE, record_release_vks, record_shield_hotkeys, hotkeys
```
to
```python
    global model, device_index, record_rate, RECORD_MODE, INITIAL_PROMPT, record_release_vks, record_shield_hotkeys, hotkeys
```
and replace
```python
    RECORD_MODE = _validate_record_mode(RECORD_MODE)
```
with
```python
    RECORD_MODE = _validate_record_mode(RECORD_MODE)
    INITIAL_PROMPT = _vocabulary_prompt(VOCABULARY)
    if INITIAL_PROMPT:
        log.info(f"Vocabulary hint: {INITIAL_PROMPT}")
```

(d) In `stop_recording_and_transcribe()`, replace
```python
        segments, _info = model.transcribe(audio_np, language="en", beam_size=5)
```
with
```python
        segments, _info = model.transcribe(
            audio_np, language="en", beam_size=5, initial_prompt=INITIAL_PROMPT,
        )
```

- [ ] **Step 4: Run, expect pass.** Same command as Step 2. Expected: 4 passed.

- [ ] **Step 5: Full suite.**
```bash
"$LOCALAPPDATA/Programs/Python/Python313/python.exe" -m pytest -q
```
Expected: 62 passed.

- [ ] **Step 6: Commit** (with `Co-Authored-By:` trailer).
```bash
git add mumble.py tests/test_mumble.py
git commit -m "feat: pass whisper.vocabulary to Whisper as an initial prompt"
```

---

## Task 3: Docs

**Files:** `README.md`, `mumble.toml.example`, `CLAUDE.md`

- [ ] **Step 1: `mumble.toml.example`** — replace
```toml
sample_rate = 16000
```
with
```toml
sample_rate = 16000
vocabulary = []
```

- [ ] **Step 2: `README.md`** — in the example config block, replace the line `sample_rate = 16000` with the two lines:
```toml
sample_rate = 16000
vocabulary = []
```
and in the config table, after the `whisper.sample_rate` row, add:
```markdown
| `whisper.vocabulary` | `[]` | Terms Whisper tends to mishear, e.g. `["CPI", "Jira"]`. Passed to Whisper as a hint on every transcription. Keep it short; restart Mumble after changing it |
```

- [ ] **Step 3: `CLAUDE.md`** — in Architecture item 3 (Transcription), append the sentence: ``Terms in `whisper.vocabulary` are joined into an `initial_prompt` (built once in `_run()`) that biases Whisper toward them.``

- [ ] **Step 4: Commit** (with `Co-Authored-By:` trailer).
```bash
git add README.md mumble.toml.example CLAUDE.md
git commit -m "docs: describe whisper.vocabulary"
```

---

## Finish

- [ ] Full suite passes (62).
- [ ] Merge `feat/whisper-vocabulary` into `master` locally (`git checkout master && git merge --ff-only feat/whisper-vocabulary`), delete the branch.
- [ ] Add `vocabulary = ["CPI"]` under `[whisper]` in the user's local `mumble.toml` (gitignored).
- [ ] Restart mumble; confirm `Vocabulary hint: CPI.` and `Ready` in `mumble.log`.

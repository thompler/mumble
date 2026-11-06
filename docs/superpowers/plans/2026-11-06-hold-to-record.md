# Hold-to-Record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the record hotkey work as push-to-talk by default (record while held, stop on release), with a quick tap falling back to today's press-to-start / press-to-stop toggle, selectable via `hotkeys.record_mode`.

**Architecture:** `RegisterHotKey` reports presses only, so release is detected by a small watcher thread that polls `GetAsyncKeyState` on the hotkey's main key after each press. The existing stop branch of `toggle_recording()` is extracted into `_stop_and_transcribe()` so both the hotkey thread (toggle) and the watcher thread (release) can end a recording. Everything else (timeout, cancel, transcribing guard, debounce) is unchanged.

**Tech Stack:** Python 3.11+, ctypes `GetAsyncKeyState` (already wrapped as `_user32`), pytest.

**Spec:** No separate document. The decisions, from the conversation on 2026-11-06:

1. Default record mode is `hold`: recording runs while the combo is held and stops when it is released.
2. A tap (combo released within `HOLD_TAP_SECONDS = 0.3` of the press) does not stop the recording; the next press stops it. This is the toggle fallback.
3. "Released" means the **main key** of the combo is up (the last `+`-separated part, e.g. `space` in `ctrl+alt+space`, `win` in `ctrl+alt+win`). Modifiers may stay held.
4. `hotkeys.record_mode = "toggle"` restores today's behaviour exactly.
5. Cancel (`Ctrl+Alt+X`) and the `max_duration` timeout keep working during a hold.

## Global Constraints

- Windows-only, single file `mumble.py`; tests in `tests/test_mumble.py`; run with `python -m pytest` from `C:\repos\mumble`.
- On this machine the PATH `python` is a broken stub. Use `& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe"` in PowerShell for every python/pytest command below.
- No new dependencies.
- Keep the existing no-lock concurrency model: module globals `recording`, `transcribing`, `audio_chunks`; guard transitions with state checks, not locks.
- Do not start the app (`python mumble.py`) as part of any task; the user verifies live.
- Every commit message ends with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- `CLAUDE.md` currently has no uncommitted changes; edit it directly in Task 4.

## Review Focus

Inputs the spec implies that a user will hit:

1. **Elevated window in the foreground.** `GetAsyncKeyState` returns 0 there, so the watcher sees an instant "release". Expected: treated as a tap, recording continues until the next press. Test in Task 3 (`test_hold_instant_release_is_a_tap`).
2. **Cancel hotkey or timeout during a hold.** Expected: the watcher exits quietly and does not stop or transcribe anything. Test in Task 3 (`test_hold_watcher_does_nothing_after_cancel`).
3. **`record_mode` with odd casing, e.g. `"Hold"`.** Expected: accepted. Test in Task 1 (`test_record_mode_is_normalised`).
4. **Unknown `record_mode`.** Expected: logged fatal error and exit 1, not a silent fallback. Test in Task 1 (`test_main_rejects_unknown_record_mode`).
5. **Chord binding (`ctrl+alt+win`) in hold mode.** Expected: releasing Win ends the recording even while Ctrl and Alt stay down. Test in Task 2 (`test_release_vks_for_chord_is_main_modifier_keys`).

---

### Task 1: `record_mode` config key with validation and tray label

**Files:**
- Modify: `mumble.py:97-118` (`_DEFAULTS`), `mumble.py:135-139` (config globals), `mumble.py:439` (tray label), `mumble.py:764` (`_run()` top)
- Modify: `mumble.toml.example`
- Test: `tests/test_mumble.py`

**Interfaces:**
- Produces: module global `RECORD_MODE: str`, lowercase, one of `"hold"` or `"toggle"`, default `"hold"`; module constant `RECORD_MODES = ("hold", "toggle")`; function `_validate_record_mode(mode: str) -> str` that returns the lowercased mode or raises `ValueError` naming the bad value and the config key `hotkeys.record_mode`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_mumble.py`)

```python
def test_default_record_mode_is_hold():
    assert mumble._DEFAULTS["hotkeys"]["record_mode"] == "hold"
    assert mumble.RECORD_MODES == ("hold", "toggle")


def test_record_mode_is_normalised():
    assert mumble._validate_record_mode("Hold") == "hold"
    assert mumble._validate_record_mode("TOGGLE") == "toggle"


def test_validate_record_mode_rejects_unknown():
    with pytest.raises(ValueError, match="hotkeys.record_mode.*bogus"):
        mumble._validate_record_mode("bogus")


def test_main_rejects_unknown_record_mode(monkeypatch, tmp_path, caplog):
    import logging
    monkeypatch.setattr("sys.argv", ["mumble"])
    monkeypatch.setattr(mumble, "LOG_FILE", str(tmp_path / "mumble.log"))
    monkeypatch.setattr(mumble.winsound, "Beep", lambda *a: None)
    monkeypatch.setattr(mumble, "RECORD_MODE", "bogus")
    monkeypatch.setattr(mumble, "find_c200_device", lambda: pytest.fail("validation must run before device lookup"))
    with caplog.at_level(logging.ERROR, logger="mumble"), pytest.raises(SystemExit) as e:
        mumble.main()
    assert e.value.code == 1
    assert "hotkeys.record_mode" in caplog.text
    for h in list(mumble.log.handlers):
        if isinstance(h, logging.FileHandler):
            mumble.log.removeHandler(h)
            h.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest tests/test_mumble.py -q -k "record_mode"`
Expected: 4 failed, with `AttributeError` for `RECORD_MODES` / `_validate_record_mode` / missing `record_mode` key, and the `main` test failing because `find_c200_device` ran.

- [ ] **Step 3: Implement**

In `mumble.py`:
- Add `"record_mode": "hold",` to `_DEFAULTS["hotkeys"]` (after `"repaste"`).
- After `HOTKEY_MODIFIERS = ...` (line 165) add `RECORD_MODES = ("hold", "toggle")`, `def _validate_record_mode(mode: str) -> str`, and `RECORD_MODE = _get("hotkeys", "record_mode")` (raw value; validation happens in `_run()` so a bad config is logged by the existing fatal-error path rather than crashing at import).
- At the top of `_run()`, before `# Find C200 mic`, add `global RECORD_MODE` and `RECORD_MODE = _validate_record_mode(RECORD_MODE)`.
- Tray label at line 439 becomes `f"Record:  {HOTKEY} ({RECORD_MODE})"`.

In `mumble.toml.example`, add `record_mode = "hold"` as the last line of `[hotkeys]`.

- [ ] **Step 4: Run the full suite**

Run: `& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest -q`
Expected: all pass (25 passed).

- [ ] **Step 5: Commit**

```powershell
git add mumble.py mumble.toml.example tests/test_mumble.py
git commit -m "feat: add hotkeys.record_mode config key (hold|toggle), default hold"
```

---

### Task 2: Release-key resolution for a hotkey string

**Files:**
- Modify: `mumble.py` (after `_is_key_held`, line 258), `mumble.py:783-790` (`_run()` hotkeys block)
- Test: `tests/test_mumble.py`

**Interfaces:**
- Consumes: `_parse_hotkey(hotkey) -> (flags, vk | None)`, `_FLAG_VKS`, `_MOD_FLAGS`, `_user32.GetAsyncKeyState`.
- Produces: `_is_vk_held(vk: int) -> bool` (true when `GetAsyncKeyState(vk) & 0x8000`); `_release_vks(hotkey: str) -> tuple[int, ...]` (virtual keys whose release ends a hold: the main key's vk, or for a modifier-only chord the `_FLAG_VKS` entry of its last part); module global `record_release_vks: tuple[int, ...]`, assigned in `_run()` from `_release_vks(HOTKEY)` right before `hotkeys = HotkeyManager()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_release_vks_for_plain_hotkey_is_main_key():
    assert mumble._release_vks("ctrl+alt+space") == (VK_SPACE,)
    assert mumble._release_vks("shift+f8") == (VK_F8,)


def test_release_vks_for_chord_is_main_modifier_keys():
    assert mumble._release_vks("ctrl+alt+win") == (0x5B, 0x5C)
    assert mumble._release_vks("ctrl+alt") == (0x12,)


def test_is_vk_held_reads_high_bit(monkeypatch):
    monkeypatch.setattr(mumble._user32, "GetAsyncKeyState", lambda vk: 0x8000 if vk == VK_SPACE else 0x0001)
    assert mumble._is_vk_held(VK_SPACE) is True
    assert mumble._is_vk_held(VK_F8) is False   # low bit (was pressed since last call) must not count
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest tests/test_mumble.py -q -k "release_vks or is_vk_held"`
Expected: 3 failed with `AttributeError`.

- [ ] **Step 3: Implement**

Add `_is_vk_held` and `_release_vks` after `_is_key_held`; have `_is_key_held` call `_is_vk_held` instead of reading `_user32` itself. Add `record_release_vks = ()` to the `# --- Globals ---` block and assign it in `_run()` (declare it in that function's `global` line).

- [ ] **Step 4: Run the full suite**

Expected: all pass (28 passed).

- [ ] **Step 5: Commit**

```powershell
git add mumble.py tests/test_mumble.py
git commit -m "feat: resolve which key's release ends a hold for a hotkey string"
```

---

### Task 3: Release watcher and hold behaviour in `toggle_recording()`

**Files:**
- Modify: `mumble.py:651-693` (`toggle_recording`), new `_stop_and_transcribe()` and `_watch_release()` directly above it, `mumble.py:362-376` (globals: add `HOLD_TAP_SECONDS = 0.3`, `RELEASE_POLL_SECONDS = 0.02`)
- Test: `tests/test_mumble.py:111-125` (`_quiet_toggle_env`) and new tests

**Interfaces:**
- Consumes: `RECORD_MODE`, `record_release_vks`, `_is_vk_held`, `decide_toggle_action`, `shutdown_event`.
- Produces: `_stop_and_transcribe() -> None` (the former `else:  # "stop"` branch of `toggle_recording`, prefixed with `if not recording: return` so a second caller is a no-op); `_watch_release(press_time: float, vks: tuple[int, ...]) -> None`; `toggle_recording()` unchanged in signature.

- [ ] **Step 1: Replace `_quiet_toggle_env` so real threads can run**

Replace lines 111-125 with:

```python
def _quiet_toggle_env(monkeypatch, mode="toggle", held=None):
    """Stub audio, tray, beeps, timers and transcription so toggle_recording() can run in a test.

    `held` is a set of virtual-key codes the fake GetAsyncKeyState reports as down.
    """
    class FakeTimer:
        def __init__(self, *a, **k): pass
        def start(self): pass
        def cancel(self): pass
    held = set() if held is None else held
    monkeypatch.setattr(mumble.threading, "Timer", FakeTimer)
    monkeypatch.setattr(mumble.winsound, "Beep", lambda *a: None)
    monkeypatch.setattr(mumble, "start_recording", lambda: True)
    monkeypatch.setattr(mumble, "update_tray", lambda s: None)
    monkeypatch.setattr(mumble, "stop_recording_and_transcribe", lambda: setattr(mumble, "transcribing", False))
    monkeypatch.setattr(mumble._user32, "GetAsyncKeyState", lambda vk: 0x8000 if vk in held else 0)
    monkeypatch.setattr(mumble, "RECORD_MODE", mode)
    monkeypatch.setattr(mumble, "record_release_vks", (VK_SPACE,))
    monkeypatch.setattr(mumble, "HOLD_TAP_SECONDS", 0.05)
    monkeypatch.setattr(mumble, "RELEASE_POLL_SECONDS", 0.005)
    monkeypatch.setattr(mumble, "recording", False)
    monkeypatch.setattr(mumble, "transcribing", False)
    monkeypatch.setattr(mumble, "recording_timer", None)
    monkeypatch.setattr(mumble, "_last_record_trigger", 0.0)
    return held
```

`VK_SPACE` is defined further down the file in the RegisterHotKey section; move the `VK_LWIN, VK_SPACE, VK_F8, VK_F24 = ...` line and the `import time` up to the top-level imports so the helper can see them.

- [ ] **Step 2: Write the failing tests** (append)

```python
def _settle():
    time.sleep(0.05)   # let the watcher thread poll a few times


def test_hold_stops_when_main_key_released_after_threshold(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    clock[0] = 100.5          # held well past HOLD_TAP_SECONDS
    _settle()
    assert mumble.recording is True
    held.clear()              # main key up
    _settle()
    assert mumble.recording is False


def test_hold_tap_keeps_recording_until_next_press(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    held.clear()              # released at t=100.0, within the tap window
    _settle()
    assert mumble.recording is True
    clock[0] = 101.0
    mumble.toggle_recording()
    assert mumble.recording is False


def test_hold_instant_release_is_a_tap(monkeypatch):
    # GetAsyncKeyState reports nothing while an elevated window is focused.
    _quiet_toggle_env(monkeypatch, mode="hold", held=set())
    mumble.toggle_recording()
    _settle()
    assert mumble.recording is True


def test_hold_only_main_key_release_counts(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={0x11, 0x12, VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    clock[0] = 100.5
    held.discard(0x11); held.discard(0x12)    # modifiers up, space still down
    _settle()
    assert mumble.recording is True
    held.discard(VK_SPACE)
    _settle()
    assert mumble.recording is False


def test_hold_watcher_does_nothing_after_cancel(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    monkeypatch.setattr(mumble, "stop_stream", lambda: setattr(mumble, "recording", False))
    stops = []
    monkeypatch.setattr(mumble, "_stop_and_transcribe", lambda: stops.append(1))
    mumble.toggle_recording()
    mumble.cancel_recording()
    held.clear()
    _settle()
    assert stops == []
    assert mumble.recording is False


def test_toggle_mode_ignores_release(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="toggle", held={VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    clock[0] = 100.5
    held.clear()
    _settle()
    assert mumble.recording is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest tests/test_mumble.py -q -k "hold or toggle_mode"`
Expected: `test_hold_watcher_does_nothing_after_cancel` fails with `AttributeError: _stop_and_transcribe`; `test_hold_stops_when_main_key_released_after_threshold` and `test_hold_only_main_key_release_counts` fail on the final `recording is False` assertion; the other three pass already (they assert today's behaviour) and that is expected. Also confirm the two pre-existing debounce tests still pass with the new helper.

- [ ] **Step 4: Implement**

In `mumble.py`:
- Add `HOLD_TAP_SECONDS = 0.3` and `RELEASE_POLL_SECONDS = 0.02` after `RECORD_DEBOUNCE_SECONDS`.
- `_stop_and_transcribe()`: move the body of the `else:  # "stop"` branch here, declare the globals it touches, and start with `if not recording: return`. The branch in `toggle_recording` becomes a call.
- `_watch_release(press_time, vks)`: loop while `recording` is True and `shutdown_event` is not set; each iteration, if no vk in `vks` is held (via `_is_vk_held`) then break, else `time.sleep(RELEASE_POLL_SECONDS)`. If the loop ended because recording stopped, return. Otherwise, if `time.monotonic() - press_time >= HOLD_TAP_SECONDS`, call `_stop_and_transcribe()`; else return (tap).
- In the `"start"` branch of `toggle_recording`, after the timer starts: if `RECORD_MODE == "hold"`, start `threading.Thread(target=_watch_release, args=(now, record_release_vks), daemon=True)`, where `now` is the debounce timestamp already computed at the top of the function. Log `"Recording (hold)..."` instead of `"Recording..."` in that case.

- [ ] **Step 5: Run the full suite three times** (the watcher tests are timing-based; they must be stable)

Run: `& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest -q; & "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest -q; & "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m pytest -q`
Expected: 34 passed, three times.

- [ ] **Step 6: Commit**

```powershell
git add mumble.py tests/test_mumble.py
git commit -m "feat: hold-to-record with tap fallback, stop on main-key release"
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md:11` (hotkey table), `README.md:18` (How it works step 1), `README.md` config example block and table (~lines 118-140)
- Modify: `CLAUDE.md` Architecture list and the paragraph starting "Hotkeys are registered globally", and the Concurrency bullet under Key decisions
- Modify: `mumble.py:1-11` module docstring

- [ ] **Step 1: README**

- Hotkey table row: `| \`Ctrl+Alt+Space\` | Hold to record, release to stop. A quick tap starts a recording that the next press stops. |`
- How it works step 1: say recording runs while the hotkey is held and stops on release; a tap under 0.3 s toggles instead; `record_mode = "toggle"` makes every press a start/stop toggle.
- Config example: add `record_mode = "hold"` under `[hotkeys]`.
- Config table: add `| \`hotkeys.record_mode\` | \`hold\` | \`hold\`: record while held, tap to toggle. \`toggle\`: press to start, press to stop |` and change the `hotkeys.record` description to `Record hotkey`.

- [ ] **Step 2: CLAUDE.md**

- Architecture item 2 (Audio) or a new item after it: one sentence that in hold mode a `_watch_release` thread polls `GetAsyncKeyState` on the main key and calls `_stop_and_transcribe()` when it lifts after `HOLD_TAP_SECONDS`.
- Concurrency bullet: add the release-watcher thread to the list of threads sharing the globals, and note `_stop_and_transcribe()` is guarded by `if not recording` so the watcher and the hotkey thread cannot both stop the same recording.

- [ ] **Step 3: Module docstring**

Line 4 becomes `Ctrl+Alt+Space  — hold to record, release to stop (tap to toggle; see hotkeys.record_mode)`.

- [ ] **Step 4: Run the suite once more**

Expected: 34 passed.

- [ ] **Step 5: Commit**

```powershell
git add README.md CLAUDE.md mumble.py
git commit -m "docs: describe hold-to-record, tap fallback, and record_mode"
```

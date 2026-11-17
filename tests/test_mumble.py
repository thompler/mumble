"""Tests for mumble's pure decision logic and error-path state resets."""

import ctypes
import os
import threading
import time

import numpy as np
import pytest

import mumble

VK_LWIN, VK_SPACE, VK_F8, VK_F24 = 0x5B, 0x20, 0x77, 0x87


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
    monkeypatch.setattr(mumble, "_refresh_audio_devices", lambda: None)
    monkeypatch.setattr(mumble, "_notify", lambda message: None)
    mumble.input_stream = "sentinel"  # ensure it gets cleared back to None
    assert mumble.start_recording() is False
    assert mumble.input_stream is None


def test_import_does_not_attach_file_handler():
    import logging
    assert not any(isinstance(h, logging.FileHandler) for h in mumble.log.handlers)


def test_setup_logging_writes_to_given_file(tmp_path):
    log_file = tmp_path / "mumble.log"
    handler = mumble._setup_logging(str(log_file))
    try:
        mumble.log.info("hello from test")
        handler.flush()
        assert "hello from test" in log_file.read_text()
    finally:
        mumble.log.removeHandler(handler)
        handler.close()


def test_main_logs_and_exits_on_startup_failure(monkeypatch, tmp_path, caplog):
    import logging
    beeps = []
    monkeypatch.setattr("sys.argv", ["mumble"])
    monkeypatch.setattr(mumble, "LOG_FILE", str(tmp_path / "mumble.log"))
    monkeypatch.setattr(mumble.winsound, "Beep", lambda *a: beeps.append(a))

    def boom():
        raise RuntimeError("PortAudio exploded")

    monkeypatch.setattr(mumble, "find_c200_device", boom)

    with caplog.at_level(logging.ERROR, logger="mumble"):
        try:
            mumble.main()
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("main() should exit non-zero on startup failure")

    assert "PortAudio exploded" in caplog.text
    assert beeps, "expected an audible error beep"
    for h in list(mumble.log.handlers):
        if isinstance(h, logging.FileHandler):
            mumble.log.removeHandler(h)
            h.close()


def test_wait_for_modifier_release_gives_up_after_timeout(monkeypatch):
    monkeypatch.setattr(mumble, "_is_key_held", lambda key: True)  # stuck modifier
    monkeypatch.setattr(mumble.time, "sleep", lambda s: None)
    released = mumble._wait_for_modifier_release(("ctrl", "alt"), timeout=0.05)
    assert released is False


def test_wait_for_modifier_release_returns_true_when_nothing_held(monkeypatch):
    monkeypatch.setattr(mumble, "_is_key_held", lambda key: False)
    assert mumble._wait_for_modifier_release(("ctrl", "alt"), timeout=0.05) is True


def test_hotkey_modifiers_collects_known_modifier_keys():
    mods = mumble._hotkey_modifiers(["ctrl+alt+win", "ctrl+alt+x", "shift+f8", "f9"])
    assert mods == {"ctrl", "alt", "win", "shift"}


def test_paste_text_waits_on_configured_modifiers(monkeypatch):
    waited = []
    monkeypatch.setattr(mumble, "_wait_for_modifier_release",
                        lambda mods, timeout=2.0: waited.append(set(mods)) or True)
    monkeypatch.setattr(mumble.time, "sleep", lambda s: None)
    monkeypatch.setattr(mumble.pyperclip, "copy", lambda t: None)
    monkeypatch.setattr(mumble.kb, "press", lambda k: None)
    monkeypatch.setattr(mumble.kb, "release", lambda k: None)
    monkeypatch.setattr(mumble, "HOTKEY_MODIFIERS", {"ctrl", "alt", "win"})
    mumble.paste_text("hi")
    assert waited == [{"ctrl", "alt", "win"}]


def test_dev_mode_is_detected_by_pyproject_not_config(monkeypatch, tmp_path):
    monkeypatch.setattr(mumble, "_SCRIPT_DIR", str(tmp_path))
    (tmp_path / "mumble.toml").write_text("")
    assert mumble._is_dev_mode() is False, "a user config alone must not mean dev mode"
    (tmp_path / "pyproject.toml").write_text("")
    assert mumble._is_dev_mode() is True


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


def test_toggle_ignores_auto_repeat_triggers(monkeypatch):
    _quiet_toggle_env(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    assert mumble.recording is True
    for _ in range(10):          # key auto-repeat every ~30 ms while held
        clock[0] += 0.03
        mumble.toggle_recording()
    assert mumble.recording is True, "held key must not stop the recording"


def test_toggle_accepts_distinct_press_after_release(monkeypatch):
    _quiet_toggle_env(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    clock[0] += 1.0
    mumble.toggle_recording()
    assert mumble.recording is False


# --- Win32 RegisterHotKey layer ---

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 1, 2, 4, 8
WM_HOTKEY = 0x0312


def test_parse_hotkey_modifier_only_chord_has_no_main_key():
    # Windows accepts VK_LWIN as a RegisterHotKey main key but never delivers it,
    # so a chord made only of modifiers is flagged (vk=None) for the poller path.
    assert mumble._parse_hotkey("ctrl+alt+win") == (MOD_CONTROL | MOD_ALT | MOD_WIN, None)


def test_hotkey_manager_fires_modifier_chord_once_per_hold(monkeypatch):
    held = set()
    monkeypatch.setattr(mumble._user32, "GetAsyncKeyState", lambda vk: 0x8000 if vk in held else 0)
    monkeypatch.setattr(mumble, "CHORD_POLL_SECONDS", 0.005)
    fires = []
    mgr = mumble.HotkeyManager()
    mgr.register("ctrl+alt+win", lambda: fires.append(1))
    mgr.start()
    try:
        held.update({0x11, 0x12})          # ctrl + alt only: not yet
        time.sleep(0.05)
        assert fires == []
        held.add(0x5B)                      # left win joins: chord complete
        time.sleep(0.05)
        assert fires == [1]
        time.sleep(0.1)                     # still held: must not re-fire
        assert fires == [1]
        held.clear()
        time.sleep(0.05)
        held.update({0x11, 0x12, 0x5C})     # again, with right win this time
        time.sleep(0.05)
        assert fires == [1, 1]
    finally:
        mgr.stop()
    assert not mgr.is_alive()


def test_parse_hotkey_named_and_character_keys():
    assert mumble._parse_hotkey("ctrl+alt+space") == (MOD_CONTROL | MOD_ALT, VK_SPACE)
    assert mumble._parse_hotkey("Ctrl+Alt+X") == (MOD_CONTROL | MOD_ALT, ord("X"))
    assert mumble._parse_hotkey("shift+f8") == (MOD_SHIFT, VK_F8)
    assert mumble._parse_hotkey("win+v") == (MOD_WIN, ord("V"))


def test_parse_hotkey_rejects_unknown_key():
    with pytest.raises(ValueError, match="bogus"):
        mumble._parse_hotkey("ctrl+alt+bogus")


def test_hotkey_manager_dispatches_posted_hotkey_message():
    fired = threading.Event()
    mgr = mumble.HotkeyManager()
    mgr.register("ctrl+alt+shift+f24", fired.set)
    mgr.start()
    try:
        # Simulate what Windows does on a press: post WM_HOTKEY (id 1) to the loop thread.
        assert ctypes.windll.user32.PostThreadMessageW(mgr.thread_id, WM_HOTKEY, 1, 0)
        assert fired.wait(2.0), "callback not invoked for WM_HOTKEY"
    finally:
        mgr.stop()
    assert not mgr.is_alive()


def test_hotkey_manager_reports_registration_failure():
    first = mumble.HotkeyManager()
    first.register("ctrl+alt+shift+f24", lambda: None)
    first.start()
    try:
        second = mumble.HotkeyManager()
        second.register("ctrl+alt+shift+f24", lambda: None)
        with pytest.raises(OSError, match=r"ctrl\+alt\+shift\+f24"):
            second.start()
        assert not second.is_alive()
    finally:
        first.stop()


def test_is_key_held_checks_both_win_keys(monkeypatch):
    asked = []
    def fake_state(vk):
        asked.append(vk)
        return 0x8000 if vk == 0x5C else 0   # only Right Win held
    monkeypatch.setattr(mumble._user32, "GetAsyncKeyState", fake_state)
    assert mumble._is_key_held("win") is True
    assert set(asked) == {0x5B, 0x5C}
    assert mumble._is_key_held("ctrl") is False


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


# --- Hold-to-record release watcher ---

def _settle():
    time.sleep(0.05)   # let the watcher thread poll a few times; only for "nothing changed" checks


def _wait_until(predicate, timeout=1.0):
    """Poll every 5 ms until predicate() is true or timeout elapses; return its last value."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_hold_stops_when_main_key_released_after_threshold(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    clock[0] = 100.5          # held well past HOLD_TAP_SECONDS
    _settle()
    assert mumble.recording is True
    held.clear()              # main key up
    assert _wait_until(lambda: mumble.recording is False)


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
    assert _wait_until(lambda: mumble.recording is False)


def test_hold_watcher_does_nothing_after_cancel(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    monkeypatch.setattr(mumble, "stop_stream", lambda: setattr(mumble, "recording", False))
    stops = []
    monkeypatch.setattr(mumble, "_stop_and_transcribe", lambda: stops.append(1))
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    clock[0] = 100.5          # past the tap window, so only the post-loop guard prevents a stop
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


def test_hold_tap_survives_slow_startup(monkeypatch):
    # Stream open plus the start beep can outlast HOLD_TAP_SECONDS; a key already
    # up on the first poll must still be treated as a tap, not a hold.
    _quiet_toggle_env(monkeypatch, mode="hold", held=set())
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])

    def slow_start():
        clock[0] += 0.5
        return True
    monkeypatch.setattr(mumble, "start_recording", slow_start)
    mumble.toggle_recording()
    _settle()
    assert mumble.recording is True


# --- Shield hotkeys: stop the main key's auto-repeat leaking after a modifier is released ---

def test_shield_hotkeys_are_main_key_with_every_smaller_modifier_subset():
    assert mumble._shield_hotkeys("ctrl+alt+space") == ["space", "ctrl+space", "alt+space"]
    assert mumble._shield_hotkeys("shift+f8") == ["f8"]
    assert mumble._shield_hotkeys("f9") == []
    assert mumble._shield_hotkeys("ctrl+alt+win") == []     # chord: no registrable main key


def _f24_is_free():
    """Probe from this thread whether anyone owns a bare F24 hotkey."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    ok = u.RegisterHotKey(None, 999, 0x4000, VK_F24)
    if ok:
        u.UnregisterHotKey(None, 999)
    return bool(ok)


def test_hotkey_manager_shield_is_owned_only_while_pushed():
    mgr = mumble.HotkeyManager()
    mgr.register("ctrl+alt+shift+f24", lambda: None)
    mgr.start()
    try:
        assert _f24_is_free()
        mgr.push_shield(["f24", "shift+f24"])
        # Wait on the manager's own state before probing: a probe that lands first would
        # itself own F24 for an instant and make the manager's registration fail.
        assert _wait_until(lambda: len(mgr._shield_ids) == 2), "shield combos were not registered"
        assert not _f24_is_free(), "shield did not take ownership of F24"
        mgr.pop_shield()
        assert _wait_until(lambda: not mgr._shield_ids), "shield was not released"
        assert _f24_is_free()
        mgr.pop_shield()                                      # second pop is a harmless no-op
        assert _wait_until(lambda: not mgr._shield_ids)
        assert _f24_is_free()
    finally:
        mgr.stop()


class _FakeHotkeys:
    def __init__(self):
        self.calls = []
    def push_shield(self, hotkeys):
        self.calls.append(("push", list(hotkeys)))
    def pop_shield(self):
        self.calls.append(("pop",))


def test_recording_start_pushes_shield_and_cancel_pops_it(monkeypatch):
    _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    fake = _FakeHotkeys()
    monkeypatch.setattr(mumble, "hotkeys", fake)
    monkeypatch.setattr(mumble, "record_shield_hotkeys", ["space", "ctrl+space", "alt+space"])
    monkeypatch.setattr(mumble, "stop_stream", lambda: setattr(mumble, "recording", False))
    mumble.toggle_recording()
    assert fake.calls == [("push", ["space", "ctrl+space", "alt+space"])]
    mumble.cancel_recording()
    assert fake.calls[-1] == ("pop",)


def test_transcription_end_pops_shield(monkeypatch):
    fake = _FakeHotkeys()
    monkeypatch.setattr(mumble, "hotkeys", fake)
    monkeypatch.setattr(mumble, "input_stream", None)
    monkeypatch.setattr(mumble, "audio_chunks", [])
    monkeypatch.setattr(mumble, "update_tray", lambda s: None)
    mumble.stop_recording_and_transcribe()      # "No audio captured" path still ends with the pop
    assert fake.calls == [("pop",)]


# --- Pasting with modifiers held turns Ctrl+V into Ctrl+Alt+V (our own repaste hotkey) ---

def test_paste_is_skipped_when_modifiers_stay_held(monkeypatch):
    monkeypatch.setattr(mumble, "_wait_for_modifier_release", lambda mods, timeout=2.0: False)
    copied, pressed, beeps = [], [], []
    monkeypatch.setattr(mumble.pyperclip, "copy", copied.append)
    monkeypatch.setattr(mumble.kb, "press", pressed.append)
    monkeypatch.setattr(mumble.kb, "release", lambda k: None)
    monkeypatch.setattr(mumble.winsound, "Beep", lambda *a: beeps.append(a))
    monkeypatch.setattr(mumble.time, "sleep", lambda s: None)
    assert mumble.paste_text("hi") is False
    assert copied == ["hi"], "clipboard still gets the text so a manual Ctrl+V works"
    assert pressed == [], "no synthetic keystrokes while modifiers are down"
    assert beeps, "audible signal that the paste was skipped"


def test_paste_proceeds_when_modifiers_released(monkeypatch):
    monkeypatch.setattr(mumble, "_wait_for_modifier_release", lambda mods, timeout=2.0: True)
    pressed = []
    monkeypatch.setattr(mumble.pyperclip, "copy", lambda t: None)
    monkeypatch.setattr(mumble.kb, "press", pressed.append)
    monkeypatch.setattr(mumble.kb, "release", lambda k: None)
    monkeypatch.setattr(mumble.time, "sleep", lambda s: None)
    assert mumble.paste_text("hi") is True
    assert pressed == [mumble.Key.ctrl, "v"]


def test_probe_owned_hotkeys_reports_combos_held_by_others():
    mgr = mumble.HotkeyManager()
    mgr.register("ctrl+alt+shift+f24", lambda: None)
    mgr.start()
    try:
        assert mumble._probe_owned_hotkeys(["ctrl+alt+shift+f24", "f24"]) == ["ctrl+alt+shift+f24"]
    finally:
        mgr.stop()
    assert mumble._probe_owned_hotkeys(["ctrl+alt+shift+f24"]) == []


# --- A hotkey re-fire while the main key has never been released is auto-repeat, not a press ---
# (Windows forgets MOD_NOREPEAT suppression once when hotkeys are registered mid-hold, e.g. the shield.)

def test_hold_refire_while_main_key_still_held_is_ignored(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="hold", held={VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    _settle()
    clock[0] = 100.5
    mumble.toggle_recording()          # the re-fire, past the debounce window
    assert mumble.recording is True, "re-fire with the key still down must not stop the recording"
    held.clear()
    assert _wait_until(lambda: mumble.recording is False), "real release must still stop it"


def test_toggle_refire_while_main_key_still_held_is_ignored(monkeypatch):
    held = _quiet_toggle_env(monkeypatch, mode="toggle", held={VK_SPACE})
    clock = [100.0]
    monkeypatch.setattr(mumble.time, "monotonic", lambda: clock[0])
    mumble.toggle_recording()
    _settle()
    clock[0] = 100.5
    mumble.toggle_recording()          # the re-fire
    assert mumble.recording is True
    held.clear()                       # user lets go; toggle mode keeps recording
    _settle()
    assert mumble.recording is True
    clock[0] = 101.0
    held.add(VK_SPACE)                 # a genuine second press
    mumble.toggle_recording()
    assert mumble.recording is False

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


def test_save_recording_survives_locked_old_file(tmp_path, monkeypatch):
    d = tmp_path / "recordings"
    d.mkdir()
    for i in range(12):
        (d / f"rec-20000101-0000{i:02d}-000.wav").write_bytes(b"old")
    real_remove = os.remove

    def remove(path):
        if os.path.basename(path) == "rec-20000101-000000-000.wav":
            raise PermissionError("in use")
        real_remove(path)

    monkeypatch.setattr(mumble.os, "remove", remove)
    path = mumble._save_recording(np.zeros(160, dtype=np.float32), 16000, str(d), keep=10)
    names = {p.name for p in d.glob("rec-*.wav")}
    assert os.path.basename(path) in names, "new recording written and path returned"
    assert "rec-20000101-000000-000.wav" in names, "locked file left in place"
    assert "rec-20000101-000001-000.wav" not in names, "pruning continues past the locked file"
    assert "rec-20000101-000002-000.wav" not in names

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


def test_save_recording_keeps_five_by_default(tmp_path):
    d = tmp_path / "recordings"
    d.mkdir()
    for i in range(7):
        (d / f"rec-20000101-0000{i:02d}-000.wav").write_bytes(b"old")
    mumble._save_recording(np.zeros(160, dtype=np.float32), 16000, str(d))
    assert len(list(d.glob("rec-*.wav"))) == 5

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
    monkeypatch.setattr(mumble.sd, "_initialized", 1)
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
    monkeypatch.setattr(mumble.sd, "_initialized", 1)
    monkeypatch.setattr(mumble.sd, "_terminate", lambda: None)
    monkeypatch.setattr(mumble.sd, "_initialize", lambda: None)
    monkeypatch.setattr(mumble, "find_c200_device", lambda: (None, None))
    monkeypatch.setattr(mumble, "device_index", 9)
    monkeypatch.setattr(mumble, "record_rate", 48000)
    mumble._refresh_audio_devices()
    assert mumble.device_index is None
    assert mumble.record_rate == 48000


def test_refresh_audio_devices_recovers_after_failed_reinit(monkeypatch):
    calls = []
    monkeypatch.setattr(mumble.sd, "_initialized", 0)
    monkeypatch.setattr(mumble.sd, "_terminate", lambda: calls.append("terminate"))
    monkeypatch.setattr(mumble.sd, "_initialize", lambda: calls.append("initialize"))
    monkeypatch.setattr(mumble, "find_c200_device", lambda: (4, 44100))
    monkeypatch.setattr(mumble, "device_index", 9)
    monkeypatch.setattr(mumble, "record_rate", 48000)
    mumble._refresh_audio_devices()
    assert calls == ["initialize"], "no terminate when PortAudio is already down"


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

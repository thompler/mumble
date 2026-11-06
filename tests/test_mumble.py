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
    monkeypatch.setattr(mumble.keyboard, "is_pressed", lambda key: True)  # stuck modifier
    monkeypatch.setattr(mumble.time, "sleep", lambda s: None)
    released = mumble._wait_for_modifier_release(("ctrl", "alt"), timeout=0.05)
    assert released is False


def test_wait_for_modifier_release_returns_true_when_nothing_held(monkeypatch):
    monkeypatch.setattr(mumble.keyboard, "is_pressed", lambda key: False)
    assert mumble._wait_for_modifier_release(("ctrl", "alt"), timeout=0.05) is True

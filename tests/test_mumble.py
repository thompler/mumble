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

"""debug_log is observability only: never raises, writes nothing when
disabled, and never affects a caller's return value. No DB needed."""

from __future__ import annotations

from src import debug_log


def test_writes_labelled_entries_in_order(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_LOG_DIR", str(tmp_path))
    debug_log.log("run-1", "[A1] context:entities", input="hello", output={"symbols": ["Foo"]})
    debug_log.log("run-1", "[A2] context:stack_frames", output=[1, 2, 3])

    text = (tmp_path / "run-run-1.md").read_text()
    assert text.startswith("# Run run-1\n")
    assert "[A1] context:entities" in text
    assert text.index("[A1]") < text.index("[A2]")
    assert "```\nhello\n```" in text          # str renders plain
    assert '"symbols": [\n    "Foo"\n  ]' in text  # dict renders as json


def test_no_run_id_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_LOG_DIR", str(tmp_path))
    debug_log.log(None, "[A1] x", output="anything")
    assert list(tmp_path.iterdir()) == []


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_LOG_DIR", "")
    debug_log.log("run-1", "[A1] x", output="anything")
    assert not (tmp_path / "run-run-1.md").exists()


def test_unwritable_dir_does_not_raise(monkeypatch):
    monkeypatch.setenv("DEBUG_LOG_DIR", "/nonexistent-root/definitely-not-writable")
    debug_log.log("run-1", "[A1] x", output="anything")  # must not raise


def test_unserializable_value_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBUG_LOG_DIR", str(tmp_path))
    debug_log.log("run-1", "[A1] x", output=object())  # default=str handles it, but even if not: no raise

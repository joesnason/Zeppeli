"""Automated tests for core/sessions.py — no Ollama/network dependency.
Safe to run anytime and exits non-zero on failure, unlike test_tool_call.py.

Covers:
- short_id()/default_title()/session_file_path()
- create_session() shape (id length, provider, empty history/runs)
- append_history_from_messages() for all three roles, including a single
  AIMessage carrying multiple tool_calls (split into multiple entries) and
  the toolResult "ok" heuristic (Error:/CANCELLED strings -> ok=False)
- finish_run_from_messages() completed/failed classification and stats
- save_session()/_write_json_atomic(): directory auto-creation, valid
  JSON on disk, atomicity (no leftover temp files, no corruption on an
  injected write failure), and that save_session() swallows errors
- the background write queue: save_session() returns without blocking on a
  slow write, queued writes are processed in FIFO order (last call always
  wins), the writer thread survives an individual write failure and keeps
  processing later items, and flush_pending_writes() returns immediately
  on an empty queue
- StoredSession.to_dict()/from_dict() round-trip
- an integration test: ui.repl.main(prompt=...) with load_llm/run_turn
  monkeypatched (as test_permission_modes.py already does) plus
  core.sessions.SESSIONS_DIR redirected to a temp dir, asserting exactly
  one session-<8char>.json file is created
"""

import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import core.eventlog as eventlog
import core.sessions as sessions
import ui.repl as repl
from core.sessions import (
    StoredSession,
    append_history_from_messages,
    create_session,
    default_title,
    finish_run_from_messages,
    save_session,
    session_file_path,
    short_id,
    start_run,
)

_ORIGINAL_SESSIONS_DIR = sessions.SESSIONS_DIR
_ORIGINAL_LOGS_DIR = eventlog.LOGS_DIR
_ORIGINAL_REPL_LOAD_LLM = repl.load_llm
_ORIGINAL_REPL_RUN_TURN = repl.run_turn
_ORIGINAL_REPL_MODEL_SUPPORTS_REASONING = repl.model_supports_reasoning
_ORIGINAL_REPL_GET_CONTEXT_WINDOW = repl.get_context_window


def _with_tmp_sessions_dir():
    """Context-manager-free helper: returns (tmpdir_obj, restore_fn). Callers
    must call restore_fn() in a finally block. Also redirects
    core.eventlog.LOGS_DIR alongside SESSIONS_DIR — any test that drives
    ui.repl.main() (e.g. test_main_prompt_mode_writes_one_session_file)
    triggers both, and neither should ever touch the real ~/.zeppeli/."""
    tmp = tempfile.TemporaryDirectory()
    sessions.SESSIONS_DIR = Path(tmp.name) / "sessions"
    eventlog.LOGS_DIR = Path(tmp.name) / "logs"

    def restore():
        sessions.SESSIONS_DIR = _ORIGINAL_SESSIONS_DIR
        eventlog.LOGS_DIR = _ORIGINAL_LOGS_DIR
        tmp.cleanup()

    return tmp, restore


def test_short_id_truncates_to_8():
    full = str(uuid.uuid4())
    assert short_id(full) == full[:8]
    assert len(short_id(full)) == 8


def test_default_title_basename():
    assert default_title("/Users/x/workspace/ollama") == "ollama"


def test_default_title_root_fallback():
    assert default_title("/") == "/"


def test_session_file_path_shape():
    tmp, restore = _with_tmp_sessions_dir()
    try:
        p = session_file_path("a1b2c3d4")
        assert p == sessions.SESSIONS_DIR / "session-a1b2c3d4.json"
    finally:
        restore()


def test_create_session_shape():
    s = create_session(str(uuid.uuid4()), "/Users/x/workspace/ollama", "gemma4:26b")
    assert len(s.id) == 8
    assert s.provider == "ollama"
    assert s.model == "gemma4:26b"
    assert s.title == "ollama"
    assert s.createdAt == s.updatedAt
    assert s.history == []
    assert s.runs == []
    assert s.ollamaUrl is None
    assert s.lmStudioUrl is None
    assert s.version == 1


def _fresh_session():
    return create_session(str(uuid.uuid4()), "/tmp/proj", "gemma4:26b")


def test_append_history_skips_system_message():
    s = _fresh_session()
    messages = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    append_history_from_messages(s, messages, 0)
    assert len(s.history) == 1
    assert s.history[0].role == "user"


def test_append_history_human_message():
    s = _fresh_session()
    messages = [HumanMessage(content="hello there")]
    append_history_from_messages(s, messages, 0)
    assert s.history[0].role == "user"
    assert s.history[0].content == "hello there"
    assert s.history[0].toolCall is None
    assert s.history[0].toolResult is None


def test_append_history_ai_message_no_tool_calls():
    s = _fresh_session()
    messages = [AIMessage(content="final answer")]
    append_history_from_messages(s, messages, 0)
    assert len(s.history) == 1
    assert s.history[0].role == "assistant"
    assert s.history[0].content == "final answer"
    assert s.history[0].toolCall is None


def test_append_history_ai_message_single_tool_call():
    s = _fresh_session()
    ai = AIMessage(content="", tool_calls=[
        {"name": "list_files", "args": {"path": "."}, "id": "call_1"},
    ])
    append_history_from_messages(s, [ai], 0)
    assert len(s.history) == 1
    entry = s.history[0]
    assert entry.role == "assistant"
    assert entry.toolCall.tool == "list_files"
    assert entry.toolCall.args == {"path": "."}


def test_append_history_ai_message_multiple_tool_calls_splits_entries():
    s = _fresh_session()
    ai = AIMessage(content="thinking...", tool_calls=[
        {"name": "list_files", "args": {"path": "."}, "id": "call_1"},
        {"name": "read_file", "args": {"path": "a.txt"}, "id": "call_2"},
    ])
    append_history_from_messages(s, [ai], 0)
    assert len(s.history) == 2
    assert s.history[0].content == "thinking..."
    assert s.history[0].toolCall.tool == "list_files"
    assert s.history[1].content == ""  # text only attached to the first entry
    assert s.history[1].toolCall.tool == "read_file"


def test_append_history_tool_message_ok():
    s = _fresh_session()
    tm = ToolMessage(content="Wrote 12 bytes to a.txt", tool_call_id="call_1")
    append_history_from_messages(s, [tm], 0)
    assert s.history[0].role == "tool"
    assert s.history[0].toolResult.ok is True
    assert s.history[0].toolResult.output == "Wrote 12 bytes to a.txt"


def test_append_history_tool_message_error_is_not_ok():
    s = _fresh_session()
    tm = ToolMessage(content="Error: file not found: a.txt", tool_call_id="call_1")
    append_history_from_messages(s, [tm], 0)
    assert s.history[0].toolResult.ok is False


def test_append_history_tool_message_cancelled_is_not_ok():
    s = _fresh_session()
    tm = ToolMessage(
        content="[write_file] CANCELLED: the user denied permission.",
        tool_call_id="call_1",
    )
    append_history_from_messages(s, [tm], 0)
    assert s.history[0].toolResult.ok is False


def test_append_history_tool_message_prefers_full_output():
    s = _fresh_session()
    tm = ToolMessage(
        content="[truncated 20 lines]",
        tool_call_id="call_1",
        additional_kwargs={"full_output": "the full untruncated output"},
    )
    append_history_from_messages(s, [tm], 0)
    assert s.history[0].toolResult.output == "the full untruncated output"


def test_append_history_tool_message_falls_back_to_content_without_full_output():
    s = _fresh_session()
    tm = ToolMessage(content="ok", tool_call_id="call_1")
    append_history_from_messages(s, [tm], 0)
    assert s.history[0].toolResult.output == "ok"


def test_append_history_start_index_offset():
    s = _fresh_session()
    messages = [SystemMessage(content="sys"), HumanMessage(content="first"),
                HumanMessage(content="second")]
    append_history_from_messages(s, messages, 2)
    assert len(s.history) == 1
    assert s.history[0].content == "second"


def test_finish_run_completed():
    run = start_run()
    ai = AIMessage(content="done", usage_metadata={
        "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
    })
    finish_run_from_messages(run, [HumanMessage(content="hi"), ai], duration_ms=250)
    assert run.status == "completed"
    assert run.completedAt is not None
    assert run.stats.turns == 1
    assert run.stats.toolCalls == 0
    assert run.stats.durationMs == 250
    assert run.stats.inputTokens == 10
    assert run.stats.outputTokens == 5


def test_finish_run_completed_counts_tool_calls_across_hops():
    run = start_run()
    ai1 = AIMessage(content="", tool_calls=[
        {"name": "list_files", "args": {}, "id": "call_1"},
    ])
    tm = ToolMessage(content="ok", tool_call_id="call_1")
    ai2 = AIMessage(content="final")
    finish_run_from_messages(run, [HumanMessage(content="hi"), ai1, tm, ai2], duration_ms=1)
    assert run.status == "completed"
    assert run.stats.turns == 2  # ai1 + ai2
    assert run.stats.toolCalls == 1


def test_finish_run_failed_stream_returned_none_first_hop():
    run = start_run()
    # run_turn() appended the HumanMessage, then stream_response() returned
    # None -> run_turn() returned early, so the last appended message is
    # the HumanMessage itself.
    finish_run_from_messages(run, [HumanMessage(content="hi")], duration_ms=5)
    assert run.status == "failed"
    assert run.error is not None


def test_finish_run_failed_stream_returned_none_after_tool_hop():
    run = start_run()
    ai1 = AIMessage(content="", tool_calls=[
        {"name": "list_files", "args": {}, "id": "call_1"},
    ])
    tm = ToolMessage(content="ok", tool_call_id="call_1")
    finish_run_from_messages(run, [HumanMessage(content="hi"), ai1, tm], duration_ms=5)
    assert run.status == "failed"


def test_save_session_creates_directory_and_valid_json():
    tmp, restore = _with_tmp_sessions_dir()
    try:
        s = _fresh_session()
        save_session(s)
        sessions.flush_pending_writes()
        path = session_file_path(s.id)
        assert path.is_file()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["version"] == 1
        assert data["id"] == s.id
        assert data["provider"] == "ollama"
    finally:
        restore()


def test_save_session_writes_readable_utf8_not_escaped():
    tmp, restore = _with_tmp_sessions_dir()
    try:
        s = _fresh_session()
        s.title = "8月17日"
        save_session(s)
        sessions.flush_pending_writes()
        raw = session_file_path(s.id).read_text(encoding="utf-8")
        assert "8月17日" in raw
        assert "\\u6708" not in raw
    finally:
        restore()


def test_save_session_no_leftover_temp_files():
    tmp, restore = _with_tmp_sessions_dir()
    try:
        s = _fresh_session()
        save_session(s)
        sessions.flush_pending_writes()
        leftovers = list(sessions.SESSIONS_DIR.glob(".tmp-session-*"))
        assert leftovers == []
    finally:
        restore()


def test_save_session_swallows_write_failure():
    tmp, restore = _with_tmp_sessions_dir()
    original = sessions._write_json_atomic
    sessions._write_json_atomic = lambda path, data: (_ for _ in ()).throw(OSError("disk full"))
    try:
        s = _fresh_session()
        save_session(s)  # must not raise
        sessions.flush_pending_writes()  # background thread must not raise either
    finally:
        sessions._write_json_atomic = original
        restore()


def test_write_json_atomic_raises_and_cleans_up_temp_file():
    tmp, restore = _with_tmp_sessions_dir()
    original_dump = json.dump
    json.dump = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
    try:
        s = _fresh_session()
        try:
            sessions._write_json_atomic(session_file_path(s.id), s.to_dict())
            assert False, "expected an exception"
        except ValueError:
            pass
        assert not session_file_path(s.id).exists()
        leftovers = list(sessions.SESSIONS_DIR.glob(".tmp-session-*"))
        assert leftovers == []
    finally:
        json.dump = original_dump
        restore()


def test_save_session_does_not_block_caller():
    tmp, restore = _with_tmp_sessions_dir()
    original = sessions._write_json_atomic

    def _slow_write(path, data):
        time.sleep(0.3)
        original(path, data)

    sessions._write_json_atomic = _slow_write
    try:
        s = _fresh_session()
        t0 = time.monotonic()
        save_session(s)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"save_session() blocked for {elapsed}s — should return immediately"
        sessions.flush_pending_writes()
        assert session_file_path(s.id).is_file()
    finally:
        sessions._write_json_atomic = original
        restore()


def test_save_session_processes_writes_in_order():
    tmp, restore = _with_tmp_sessions_dir()
    try:
        s = _fresh_session()
        s.title = "first"
        save_session(s)
        s.title = "second"
        save_session(s)
        sessions.flush_pending_writes()
        with open(session_file_path(s.id), encoding="utf-8") as f:
            data = json.load(f)
        assert data["title"] == "second"
    finally:
        restore()


def test_writer_thread_survives_a_failed_write():
    tmp, restore = _with_tmp_sessions_dir()
    original = sessions._write_json_atomic
    calls = {"n": 0}

    def _fail_once_then_succeed(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated failure on first write")
        original(path, data)

    sessions._write_json_atomic = _fail_once_then_succeed
    try:
        s1 = _fresh_session()
        s2 = _fresh_session()
        save_session(s1)  # this one fails inside the writer thread
        save_session(s2)  # the thread must still process this one
        sessions.flush_pending_writes()
        assert not session_file_path(s1.id).exists()
        assert session_file_path(s2.id).is_file()
    finally:
        sessions._write_json_atomic = original
        restore()


def test_flush_pending_writes_on_empty_queue_returns_immediately():
    tmp, restore = _with_tmp_sessions_dir()
    try:
        t0 = time.monotonic()
        sessions.flush_pending_writes()
        assert time.monotonic() - t0 < 0.1
    finally:
        restore()


def test_stored_session_round_trip():
    s = _fresh_session()
    ai = AIMessage(content="", tool_calls=[{"name": "list_files", "args": {}, "id": "c1"}])
    append_history_from_messages(s, [HumanMessage(content="hi"), ai], 0)
    run = start_run()
    finish_run_from_messages(run, [HumanMessage(content="hi"), AIMessage(content="ok")], 10)
    s.runs.append(run)

    restored = StoredSession.from_dict(s.to_dict())
    assert restored.id == s.id
    assert restored.provider == s.provider
    assert len(restored.history) == len(s.history)
    assert restored.history[0].role == s.history[0].role
    assert restored.runs[0].status == s.runs[0].status
    assert restored.runs[0].stats.turns == s.runs[0].stats.turns


def test_main_prompt_mode_writes_one_session_file():
    tmp, restore = _with_tmp_sessions_dir()
    repl.load_llm = lambda **k: object()
    repl.model_supports_reasoning = lambda *a, **k: False  # no real Ollama call in tests
    repl.get_context_window = lambda *a, **k: None  # no real Ollama call in tests

    def _fake_run_turn(llm_with_tools, messages, user_input, console, initial_cwd,
                        mode, images=None, session_id=None, run_id=None, reasoning=False,
                        context_window=None):
        messages.append(HumanMessage(content=user_input))
        messages.append(AIMessage(content="hi back"))

    repl.run_turn = _fake_run_turn
    try:
        repl.main(prompt="hi", mode="approval")
        files = list(sessions.SESSIONS_DIR.glob("session-*.json"))
        assert len(files) == 1
        with open(files[0], encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["runs"]) == 1
        assert data["runs"][0]["status"] == "completed"
        assert len(data["history"]) == 2
    finally:
        repl.load_llm = _ORIGINAL_REPL_LOAD_LLM
        repl.run_turn = _ORIGINAL_REPL_RUN_TURN
        repl.model_supports_reasoning = _ORIGINAL_REPL_MODEL_SUPPORTS_REASONING
        repl.get_context_window = _ORIGINAL_REPL_GET_CONTEXT_WINDOW
        restore()


TESTS = [
    test_short_id_truncates_to_8,
    test_default_title_basename,
    test_default_title_root_fallback,
    test_session_file_path_shape,
    test_create_session_shape,
    test_append_history_skips_system_message,
    test_append_history_human_message,
    test_append_history_ai_message_no_tool_calls,
    test_append_history_ai_message_single_tool_call,
    test_append_history_ai_message_multiple_tool_calls_splits_entries,
    test_append_history_tool_message_ok,
    test_append_history_tool_message_error_is_not_ok,
    test_append_history_tool_message_cancelled_is_not_ok,
    test_append_history_tool_message_prefers_full_output,
    test_append_history_tool_message_falls_back_to_content_without_full_output,
    test_append_history_start_index_offset,
    test_finish_run_completed,
    test_finish_run_completed_counts_tool_calls_across_hops,
    test_finish_run_failed_stream_returned_none_first_hop,
    test_finish_run_failed_stream_returned_none_after_tool_hop,
    test_save_session_creates_directory_and_valid_json,
    test_save_session_writes_readable_utf8_not_escaped,
    test_save_session_no_leftover_temp_files,
    test_save_session_swallows_write_failure,
    test_write_json_atomic_raises_and_cleans_up_temp_file,
    test_save_session_does_not_block_caller,
    test_save_session_processes_writes_in_order,
    test_writer_thread_survives_a_failed_write,
    test_flush_pending_writes_on_empty_queue_returns_immediately,
    test_stored_session_round_trip,
    test_main_prompt_mode_writes_one_session_file,
]


if __name__ == "__main__":
    failures = []
    for t in TESTS:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failures.append(t.__name__)

    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)

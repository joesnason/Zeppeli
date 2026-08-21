"""Automated tests for core/eventlog.py — the ~/.zeppeli/logs/log-<session-id>.jsonl
event stream. No Ollama/network dependency, exits non-zero on failure, style
matches test_sessions.py (which this suite deliberately mirrors: a
background-thread + queue.Queue writer, but appending JSONL lines instead of
rewriting a whole JSON document).

Covers:
- log_path() filename shape (full session id, not the 8-char short_id())
- each log_*() function's JSONL line shape: type/timestamp/sessionId/
  [runId]/data, including run_completed's completed vs. failed variants and
  cli_error's message-length cap
- build_turns_and_outputs(): single tool call, multiple tool calls in one
  hop, a final no-tool-call answer, and thinkingChars sourced from
  additional_kwargs["reasoning_content"]
- the background write queue: non-blocking, FIFO ordering, the writer
  thread surviving a bad write, flush_pending_events() on an empty queue
- ui/streaming.py's stream_response(): model_activity is only emitted when
  session_id/run_id/turn_index are all given; omitted (no event, no crash)
  for any other caller
"""

import io
import json
import sys
import tempfile
import time
from pathlib import Path

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from rich.console import Console

import core.eventlog as eventlog
import core.sessions as sessions
import ui.repl as repl
from core.eventlog import (
    build_turns_and_outputs,
    flush_pending_events,
    log_cli_error,
    log_model_activity,
    log_path,
    log_run_completed,
    log_run_started,
    log_session_started,
)
from ui.streaming import stream_response

_ORIGINAL_LOGS_DIR = eventlog.LOGS_DIR
_ORIGINAL_SESSIONS_DIR = sessions.SESSIONS_DIR
_ORIGINAL_REPL_LOAD_LLM = repl.load_llm
_ORIGINAL_REPL_MODEL_SUPPORTS_REASONING = repl.model_supports_reasoning


def _with_tmp_logs_dir():
    """Context-manager-free helper: returns (tmpdir_obj, restore_fn). Callers
    must call restore_fn() in a finally block."""
    tmp = tempfile.TemporaryDirectory()
    eventlog.LOGS_DIR = Path(tmp.name) / "logs"

    def restore():
        eventlog.LOGS_DIR = _ORIGINAL_LOGS_DIR
        tmp.cleanup()

    return tmp, restore


def _read_lines(session_id: str) -> list[dict]:
    path = log_path(session_id)
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# --- log_path -----------------------------------------------------------

def test_log_path_uses_full_session_id():
    sid = "7a3b4c5d-6e7f-8a9b-0c1d-2e3f4a5b6c7d"
    assert log_path(sid) == eventlog.LOGS_DIR / f"log-{sid}.jsonl"
    assert len(sid) > 8  # this is the point: NOT truncated like sessions.py's short_id()


# --- log_*() shapes -------------------------------------------------------

def test_log_session_started_shape():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-1"
        log_session_started(sid, cwd="/x/y", provider="ollama", model="gemma4:26b",
                             ollama_url="http://127.0.0.1:11434", pid=123, platform="darwin",
                             reasoning_mode="enabled")
        flush_pending_events()
        lines = _read_lines(sid)
        assert len(lines) == 1
        line = lines[0]
        assert line["type"] == "session_started"
        assert line["sessionId"] == sid
        assert "runId" not in line
        assert line["data"]["cwd"] == "/x/y"
        assert line["data"]["provider"] == "ollama"
        assert line["data"]["model"] == "gemma4:26b"
        assert line["data"]["ollamaUrl"] == "http://127.0.0.1:11434"
        assert line["data"]["pid"] == 123
        assert line["data"]["platform"] == "darwin"
        assert line["data"]["reasoningMode"] == "enabled"
        assert line["data"]["recoveredInterruptedRuns"] == 0
        assert "maxTurns" not in line["data"]
    finally:
        restore()


def test_log_session_started_reasoning_mode_defaults_to_unavailable():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-1b"
        log_session_started(sid, cwd="/x/y", provider="litellm", model="gpt-4o-mini",
                             ollama_url=None, pid=123, platform="darwin")
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["data"]["reasoningMode"] == "unavailable"
    finally:
        restore()


def test_log_run_started_shape():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-2"
        log_run_started(sid, "run-1", "hello world")
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["type"] == "run_started"
        assert line["runId"] == "run-1"
        assert line["data"]["promptLength"] == len("hello world")
        assert line["data"]["promptPreview"] == "hello world"
    finally:
        restore()


def test_log_run_started_preview_is_truncated():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-3"
        long_prompt = "x" * 500
        log_run_started(sid, "run-1", long_prompt)
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["data"]["promptLength"] == 500
        assert len(line["data"]["promptPreview"]) == 200
    finally:
        restore()


def test_log_model_activity_shape():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-4"
        log_model_activity(sid, "run-1", index=0, finalization=False,
                            thinking="pondering...", content="partial answer")
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["type"] == "model_activity"
        assert line["runId"] == "run-1"
        assert line["data"]["index"] == 0
        assert line["data"]["finalization"] is False
        assert line["data"]["chunk"] == {
            "thinking": "pondering...", "content": "partial answer", "done": True,
        }
    finally:
        restore()


def test_log_run_completed_shape_completed():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-5"
        from core.sessions import RunStats
        stats = RunStats(durationMs=1200, turns=2, toolCalls=1, inputTokens=10, outputTokens=20)
        turns = [{"kind": "final", "content": "the answer"}]
        outputs = [{"index": 0, "finalization": True, "contentChars": 10,
                    "thinkingChars": 0, "done": True}]
        log_run_completed(sid, "run-1", status="completed", answer="the answer",
                           stats=stats, turns=turns, model_outputs=outputs)
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["type"] == "run_completed"
        assert line["data"]["completionStatus"] == "completed"
        assert line["data"]["answer"] == "the answer"
        assert line["data"]["answerLength"] == len("the answer")
        assert line["data"]["stats"]["turns"] == 2
        assert line["data"]["stats"]["toolCalls"] == 1
        assert line["data"]["turns"] == turns
        assert line["data"]["modelOutputs"] == outputs
        assert "error" not in line["data"]
    finally:
        restore()


def test_log_run_completed_shape_failed_includes_error():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-6"
        log_run_completed(sid, "run-1", status="failed", answer="", stats=None,
                           turns=[], model_outputs=[], error="model call failed")
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["data"]["completionStatus"] == "failed"
        assert line["data"]["stats"] is None
        assert line["data"]["error"] == "model call failed"
    finally:
        restore()


def test_log_cli_error_shape():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-7"
        log_cli_error(sid, ValueError("bad config"))
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert line["type"] == "cli_error"
        assert line["data"]["name"] == "ValueError"
        assert line["data"]["message"] == "bad config"
        assert "runId" not in line
    finally:
        restore()


def test_log_cli_error_message_is_capped():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-8"
        log_cli_error(sid, RuntimeError("x" * 3000))
        flush_pending_events()
        line = _read_lines(sid)[0]
        assert len(line["data"]["message"]) == eventlog._MAX_ERROR_MESSAGE_CHARS + 1  # + ellipsis
    finally:
        restore()


# --- build_turns_and_outputs() ---------------------------------------------

def test_build_turns_single_tool_call():
    ai = AIMessage(content="using a tool", tool_calls=[
        {"name": "list_files", "args": {"path": "."}, "id": "call_1"},
    ])
    tm = ToolMessage(content="a.txt\nb.txt", tool_call_id="call_1")
    turns, outputs = build_turns_and_outputs([HumanMessage(content="hi"), ai, tm])
    assert len(turns) == 1
    assert turns[0]["kind"] == "tool"
    assert turns[0]["toolCall"] == {"tool": "list_files", "args": {"path": "."}}
    assert turns[0]["toolResult"] == {"ok": True, "output": "a.txt\nb.txt"}
    assert len(outputs) == 1
    assert outputs[0] == {"index": 0, "finalization": False, "contentChars": len("using a tool"),
                           "thinkingChars": 0, "done": True}


def test_build_turns_multiple_tool_calls_one_hop():
    ai = AIMessage(content="", tool_calls=[
        {"name": "list_files", "args": {}, "id": "call_1"},
        {"name": "read_file", "args": {"path": "a.txt"}, "id": "call_2"},
    ])
    tm1 = ToolMessage(content="ok1", tool_call_id="call_1")
    tm2 = ToolMessage(content="Error: not found", tool_call_id="call_2")
    turns, outputs = build_turns_and_outputs([ai, tm1, tm2])
    assert len(turns) == 2
    assert turns[0]["toolCall"]["tool"] == "list_files"
    assert turns[0]["toolResult"]["ok"] is True
    assert turns[1]["toolCall"]["tool"] == "read_file"
    assert turns[1]["toolResult"]["ok"] is False
    assert len(outputs) == 1  # one AIMessage hop, even with two tool calls


def test_build_turns_final_answer_no_tool_calls():
    ai = AIMessage(content="the final answer")
    turns, outputs = build_turns_and_outputs([ai])
    assert turns == [{"kind": "final", "content": "the final answer"}]
    assert outputs[0]["finalization"] is True


def test_build_turns_tool_hop_then_final_hop():
    ai1 = AIMessage(content="", tool_calls=[{"name": "list_files", "args": {}, "id": "call_1"}])
    tm = ToolMessage(content="ok", tool_call_id="call_1")
    ai2 = AIMessage(content="done")
    turns, outputs = build_turns_and_outputs([HumanMessage(content="hi"), ai1, tm, ai2])
    assert len(turns) == 2
    assert turns[0]["kind"] == "tool"
    assert turns[1] == {"kind": "final", "content": "done"}
    assert len(outputs) == 2
    assert outputs[0]["index"] == 0 and outputs[0]["finalization"] is False
    assert outputs[1]["index"] == 1 and outputs[1]["finalization"] is True


def test_build_turns_thinking_chars_from_reasoning_content():
    ai = AIMessage(content="the answer",
                    additional_kwargs={"reasoning_content": "step by step..."})
    _, outputs = build_turns_and_outputs([ai])
    assert outputs[0]["thinkingChars"] == len("step by step...")


def test_build_turns_empty_messages():
    assert build_turns_and_outputs([]) == ([], [])


# --- background write queue -------------------------------------------------

def test_flush_pending_events_on_empty_queue_returns_immediately():
    tmp, restore = _with_tmp_logs_dir()
    try:
        t0 = time.monotonic()
        flush_pending_events()
        assert time.monotonic() - t0 < 0.1
    finally:
        restore()


def test_log_does_not_block_caller():
    tmp, restore = _with_tmp_logs_dir()
    original = eventlog._append_jsonl

    def _slow_append(path, line):
        time.sleep(0.3)
        original(path, line)

    eventlog._append_jsonl = _slow_append
    try:
        t0 = time.monotonic()
        log_cli_error("sess-slow", RuntimeError("x"))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, f"log_cli_error() blocked for {elapsed}s — should return immediately"
        flush_pending_events()
        assert log_path("sess-slow").is_file()
    finally:
        eventlog._append_jsonl = original
        restore()


def test_log_processes_writes_in_fifo_order():
    tmp, restore = _with_tmp_logs_dir()
    try:
        sid = "sess-fifo"
        for i in range(5):
            log_cli_error(sid, RuntimeError(f"error-{i}"))
        flush_pending_events()
        lines = _read_lines(sid)
        assert [line["data"]["message"] for line in lines] == [f"error-{i}" for i in range(5)]
    finally:
        restore()


def test_writer_thread_survives_a_failed_write():
    tmp, restore = _with_tmp_logs_dir()
    original = eventlog._append_jsonl
    calls = {"n": 0}

    def _fail_once_then_succeed(path, line):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated failure on first write")
        original(path, line)

    eventlog._append_jsonl = _fail_once_then_succeed
    try:
        log_cli_error("sess-fail", RuntimeError("first"))   # fails inside the writer thread
        log_cli_error("sess-ok", RuntimeError("second"))     # thread must still process this one
        flush_pending_events()
        assert not log_path("sess-fail").exists()
        assert log_path("sess-ok").is_file()
    finally:
        eventlog._append_jsonl = original
        restore()


# --- ui/streaming.py integration --------------------------------------------

class _FakeStreamingLLM:
    """Stands in for llm_with_tools: .stream(messages) yields a small
    sequence of AIMessageChunks with plain-str content and no tool calls."""

    def stream(self, messages):
        yield AIMessageChunk(content="hello ")
        yield AIMessageChunk(content="world")


def test_stream_response_emits_model_activity_when_ids_given():
    tmp, restore = _with_tmp_logs_dir()
    try:
        console = Console(file=io.StringIO())
        sid, rid = "sess-stream", "run-stream"
        response = stream_response(_FakeStreamingLLM(), [], console,
                                    session_id=sid, run_id=rid, turn_index=0)
        assert response is not None
        flush_pending_events()
        lines = _read_lines(sid)
        assert len(lines) == 1
        assert lines[0]["type"] == "model_activity"
        assert lines[0]["data"]["index"] == 0
        assert lines[0]["data"]["finalization"] is True  # no tool_calls
        assert lines[0]["data"]["chunk"]["content"] == "hello world"
    finally:
        restore()


def test_stream_response_omits_model_activity_without_full_identity():
    tmp, restore = _with_tmp_logs_dir()
    try:
        console = Console(file=io.StringIO())
        # No session_id/run_id/turn_index — must not write anything, must not raise.
        response = stream_response(_FakeStreamingLLM(), [], console)
        assert response is not None
        flush_pending_events()
        written = list(eventlog.LOGS_DIR.glob("*.jsonl")) if eventlog.LOGS_DIR.exists() else []
        assert written == []
    finally:
        restore()


def test_main_logs_cli_error_on_uncaught_exception():
    """ui.repl.main() re-raises any exception from load_llm() onward, but
    first logs it as a cli_error event — the one hook point this codebase
    has today for reporting genuinely unexpected bugs to the event log."""
    tmp, restore = _with_tmp_logs_dir()
    sessions_tmp = tempfile.TemporaryDirectory()
    sessions.SESSIONS_DIR = Path(sessions_tmp.name) / "sessions"

    def _boom(**k):
        raise RuntimeError("simulated load_llm failure")

    repl.load_llm = _boom
    repl.model_supports_reasoning = lambda *a, **k: False  # no real Ollama call in tests
    try:
        try:
            repl.main(mode="approval", prompt="hi")
            assert False, "expected the exception to propagate"
        except RuntimeError as e:
            assert "simulated load_llm failure" in str(e)
        flush_pending_events()
        lines = _read_lines(repl._session_state["id"])
        types = [line["type"] for line in lines]
        assert "session_started" in types
        cli_errors = [line for line in lines if line["type"] == "cli_error"]
        assert len(cli_errors) == 1
        assert cli_errors[0]["data"]["name"] == "RuntimeError"
        assert "simulated load_llm failure" in cli_errors[0]["data"]["message"]
    finally:
        repl.load_llm = _ORIGINAL_REPL_LOAD_LLM
        repl.model_supports_reasoning = _ORIGINAL_REPL_MODEL_SUPPORTS_REASONING
        sessions.SESSIONS_DIR = _ORIGINAL_SESSIONS_DIR
        sessions_tmp.cleanup()
        restore()


TESTS = [
    test_log_path_uses_full_session_id,
    test_log_session_started_shape,
    test_log_session_started_reasoning_mode_defaults_to_unavailable,
    test_log_run_started_shape,
    test_log_run_started_preview_is_truncated,
    test_log_model_activity_shape,
    test_log_run_completed_shape_completed,
    test_log_run_completed_shape_failed_includes_error,
    test_log_cli_error_shape,
    test_log_cli_error_message_is_capped,
    test_build_turns_single_tool_call,
    test_build_turns_multiple_tool_calls_one_hop,
    test_build_turns_final_answer_no_tool_calls,
    test_build_turns_tool_hop_then_final_hop,
    test_build_turns_thinking_chars_from_reasoning_content,
    test_build_turns_empty_messages,
    test_flush_pending_events_on_empty_queue_returns_immediately,
    test_log_does_not_block_caller,
    test_log_processes_writes_in_fifo_order,
    test_writer_thread_survives_a_failed_write,
    test_stream_response_emits_model_activity_when_ids_given,
    test_stream_response_omits_model_activity_without_full_identity,
    test_main_logs_cli_error_on_uncaught_exception,
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

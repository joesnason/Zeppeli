"""Per-session event log — appends one JSON line per lifecycle event to
~/.zeppeli/logs/log-<full-session-id>.jsonl.

This is a separate, more granular record than core/sessions.py's
~/.zeppeli/sessions/session-<8charid>.json summary file: an append-only
event stream (session_started, run_started, model_activity, run_completed,
cli_error) for local observability/debugging of runs, rather than a
resumable session summary. Both live under ~/.zeppeli/ but are independent
on-disk artifacts, keyed off the same full session UUID
(ui/repl.py's `_session_state["id"]`) — this file uses that UUID in full
for its filename, unlike core/sessions.py's truncated short_id().

Pure data/logic, no UI deps — same core/ui split as core/sessions.py.

Design notes (mirrors core/sessions.py's write-up):
- Every log_*() function never raises — event logging is a nice-to-have
  side feature; a disk-full/permissions/serialization bug must never crash
  the chat or interrupt the user's turn.
- Every log_*() function is non-blocking: each event line is enqueued and
  appended to disk by a single background daemon thread, so slow disk I/O
  never blocks the chat/turn flow. flush_pending_events() blocks until the
  queue is drained; it's called explicitly at the end of one-shot -p mode
  and via atexit on normal interpreter exit (which also fires on an
  uncaught exception/KeyboardInterrupt reaching the top of the script) —
  same accepted residual risk as core/sessions.py: a hard kill/crash
  between enqueue and the actual write can still lose the most recent
  event.
- Unlike core/sessions.py's whole-file JSON rewrites (temp file +
  os.replace()), this is an append-only log, so each write is a plain
  `open(path, "a").write(line + "\\n")` under a dedicated writer thread —
  a single writer thread means appends are never interleaved, so there's
  no torn-write risk to guard against with atomic-replace machinery.

Known limitations (see docs/logging.md):
- model_activity's "thinking" field is populated for local Ollama runs on
  a model that advertises reasoning support (ui/repl.py's main() checks
  core.agent.model_supports_reasoning() up front, via ollama.show()'s
  capabilities list, then enables reasoning=True through ui/streaming.py's
  stream_response() — which also carries an automatic once-per-process
  fallback as a safety net, in case that upfront check was wrong). Cloud/
  self-hosted models via --base-url have no equivalent, so "thinking" is
  always empty for those; session_started.reasoningMode is "unsupported"
  for a local model that doesn't advertise the capability.
- session_started has no "maxTurns" field — run_turn()'s tool-call loop is
  genuinely unbounded, no such config exists in this CLI.
- "recoveredInterruptedRuns" is always 0 — this codebase has no
  session-resume concept; every process starts a fresh session id/file.
- cli_error only covers exceptions raised after a session exists (i.e.
  inside ui/repl.py's main(), after the session id is minted) — pre-session
  argparse validation errors in cli.py already exit cleanly via
  parser.error() and aren't logged here.
"""

import atexit
import dataclasses
import json
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, ToolMessage

from .messages import extract_text, tool_result_ok

LOGS_DIR = Path.home() / ".zeppeli" / "logs"

_MAX_ERROR_MESSAGE_CHARS = 2000
_PROMPT_PREVIEW_CHARS = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_path(session_id: str) -> Path:
    return LOGS_DIR / f"log-{session_id}.jsonl"


# --- turns[] / modelOutputs[] construction ---------------------------------

def build_turns_and_outputs(new_messages: list) -> tuple[list[dict], list[dict]]:
    """Convert messages[start_index:] (one run_turn() call's newly appended
    LangChain messages) into run_completed's `turns`/`modelOutputs` arrays.

    turns: one {"kind": "tool", ...} entry per tool call made by an
    AIMessage (paired with its matching ToolMessage by tool_call_id), plus
    one {"kind": "final", ...} entry for the terminal tool-call-free
    AIMessage, if present. modelOutputs: one summary entry per AIMessage,
    in order (index = hop position, matching model_activity's own index).
    """
    tool_messages_by_id = {
        m.tool_call_id: m for m in new_messages if isinstance(m, ToolMessage)
    }

    turns: list[dict] = []
    model_outputs: list[dict] = []
    ai_index = 0
    for m in new_messages:
        if not isinstance(m, AIMessage):
            continue
        text = extract_text(m.content)
        thinking = m.additional_kwargs.get("reasoning_content", "") or ""
        if m.tool_calls:
            for tc in m.tool_calls:
                tool_msg = tool_messages_by_id.get(tc["id"])
                if tool_msg is not None:
                    output = str(tool_msg.additional_kwargs.get("full_output", tool_msg.content))
                else:
                    output = ""
                turns.append({
                    "kind": "tool",
                    "content": text,
                    "toolCall": {"tool": tc["name"], "args": tc["args"]},
                    "toolResult": {"ok": tool_result_ok(output), "output": output},
                })
        else:
            turns.append({"kind": "final", "content": text})
        model_outputs.append({
            "index": ai_index,
            "finalization": not bool(m.tool_calls),
            "contentChars": len(text),
            "thinkingChars": len(thinking),
            "done": True,
        })
        ai_index += 1
    return turns, model_outputs


# --- event construction / emission ------------------------------------------

def _envelope(event_type: str, session_id: str, data: dict, run_id: str | None = None) -> dict:
    d = {"type": event_type, "timestamp": _now_iso(), "sessionId": session_id}
    if run_id is not None:
        d["runId"] = run_id
    d["data"] = data
    return d


def log_session_started(session_id: str, *, cwd: str, provider: str, model: str | None,
                          ollama_url: str | None, pid: int, platform: str,
                          reasoning_mode: str = "unavailable") -> None:
    _emit(session_id, _envelope("session_started", session_id, {
        "cwd": cwd,
        "provider": provider,
        "model": model,
        "ollamaUrl": ollama_url,
        "platform": platform,
        "pid": pid,
        "reasoningMode": reasoning_mode,
        "recoveredInterruptedRuns": 0,
    }))


def log_run_started(session_id: str, run_id: str, prompt: str) -> None:
    _emit(session_id, _envelope("run_started", session_id, {
        "promptLength": len(prompt),
        "promptPreview": prompt[:_PROMPT_PREVIEW_CHARS],
    }, run_id=run_id))


def log_model_activity(session_id: str, run_id: str, *, index: int, finalization: bool,
                         thinking: str, content: str) -> None:
    _emit(session_id, _envelope("model_activity", session_id, {
        "index": index,
        "finalization": finalization,
        "chunk": {"thinking": thinking, "content": content, "done": True},
    }, run_id=run_id))


def log_run_completed(session_id: str, run_id: str, *, status: str, answer: str,
                        stats, turns: list[dict], model_outputs: list[dict],
                        error: str | None = None) -> None:
    data = {
        "completionStatus": status,
        "answer": answer,
        "answerLength": len(answer),
        "stats": dataclasses.asdict(stats) if stats is not None else None,
        "turns": turns,
        "modelOutputs": model_outputs,
    }
    if status == "failed" and error is not None:
        data["error"] = error
    _emit(session_id, _envelope("run_completed", session_id, data, run_id=run_id))


def log_cli_error(session_id: str, error: Exception) -> None:
    message = str(error)
    if len(message) > _MAX_ERROR_MESSAGE_CHARS:
        message = message[:_MAX_ERROR_MESSAGE_CHARS] + "…"
    _emit(session_id, _envelope("cli_error", session_id, {
        "name": type(error).__name__,
        "message": message,
    }))


# --- disk I/O ---------------------------------------------------------------
#
# Every write goes through a single background thread consuming a FIFO
# queue.Queue, so log_*() never blocks its caller on disk I/O. Appends from
# a single writer thread are never interleaved, so no atomic-replace
# machinery is needed the way core/sessions.py's whole-file rewrites need it.

def _append_jsonl(path: Path, line: dict) -> None:
    """Raises on failure — internal, only ever called from the background
    writer thread (or synchronously by a test)."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


_write_queue: "queue.Queue[tuple[Path, dict]]" = queue.Queue()
_writer_thread: threading.Thread | None = None
_writer_lock = threading.Lock()


def _writer_loop() -> None:
    while True:
        path, line = _write_queue.get()
        try:
            _append_jsonl(path, line)
        except Exception:
            # Swallow per-item — a single bad write (disk full, permission
            # denied) must not kill this thread, or every event queued
            # after it would silently never be processed again.
            pass
        finally:
            _write_queue.task_done()


def _ensure_writer_thread() -> None:
    global _writer_thread
    with _writer_lock:
        if _writer_thread is None:
            _writer_thread = threading.Thread(
                target=_writer_loop, daemon=True, name="zeppeli-eventlog-writer")
            _writer_thread.start()


def flush_pending_events() -> None:
    """Block until every event enqueued so far has been written to disk.
    Returns immediately if nothing has ever been enqueued."""
    _write_queue.join()


atexit.register(flush_pending_events)


def _emit(session_id: str, line: dict) -> None:
    """Enqueue `line` to be appended to log_path(session_id) by the
    background writer thread — never blocks the caller on I/O. Never
    raises — event logging is a nice-to-have side feature; a disk-full,
    permissions, or serialization bug must never crash the chat or
    interrupt the user's turn."""
    try:
        _ensure_writer_thread()
        _write_queue.put((log_path(session_id), line))
    except Exception:
        pass

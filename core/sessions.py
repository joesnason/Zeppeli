"""Session history persistence — writes each REPL/one-shot run to
~/.zeppeli/sessions/session-<8charid>.json.

Pure data/logic, no UI deps (no Console, no prompt_toolkit) — writing JSON
to disk isn't "talking to a terminal" per the core/ui architecture split.

Design notes (see docs/sessions.md for the full write-up):
- `provider` is always "ollama" — this codebase has no real LM Studio
  integration; the only other backend is generic litellm-cloud/self-hosted,
  which is not semantically "lmstudio".
- The persisted `id` is the first 8 characters of the process's existing
  full session UUID (ui/repl.py's `_session_state["id"]`), not a separately
  minted id — one session, one identity, just a shorter wire format.
- `ollamaUrl`/`lmStudioUrl` are always left None — this codebase has no
  "custom Ollama URL" concept, and litellm's `base_url` isn't semantically
  either field.
- save_session() never raises — persistence is a nice-to-have side feature;
  a disk-full/permissions/serialization bug must never crash the chat or
  interrupt the user's turn.
- save_session() is non-blocking: every write is enqueued and processed
  sequentially by a single background thread, so slow disk I/O never
  blocks the chat/turn flow. flush_pending_writes() blocks until the queue
  is drained; it's called explicitly at the end of one-shot -p mode and
  via atexit on normal interpreter exit (which also fires on an uncaught
  exception/KeyboardInterrupt reaching the top of the script) — see
  docs/sessions.md for the accepted residual risk (a hard kill/crash
  between enqueue and the actual write can still lose the most recent
  write, the same class of risk a synchronous design has for a kill
  landing mid-write()).
"""

import atexit
import dataclasses
import json
import os
import queue
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .messages import extract_text, tool_result_ok

SESSIONS_DIR = Path.home() / ".zeppeli" / "sessions"
STORED_SESSION_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_id(full_session_id: str) -> str:
    """Truncate a full uuid4 session id (ui.repl._session_state['id']) to
    its first 8 chars — this, not a separately minted id, is the persisted
    session's id/filename. The toolbar keeps showing the full UUID."""
    return full_session_id[:8]


def default_title(cwd: str) -> str:
    """Default session title: the cwd's basename (e.g. 'ollama' for
    /Users/x/workspace/ollama), or the full path string in the edge case
    where the basename is empty (cwd == '/')."""
    p = Path(cwd)
    return p.name or str(p)


def session_file_path(session_id_8: str) -> Path:
    return SESSIONS_DIR / f"session-{session_id_8}.json"


# --- dataclasses ---------------------------------------------------------
# Field names deliberately mirror the JSON schema's camelCase keys
# (createdAt, toolCall, durationMs, ...) rather than snake_case + a
# translation table — this is a wire format, and to_dict() is a structural
# dump, not a key-renaming pass.

@dataclass
class ToolCallInfo:
    tool: str
    args: dict


@dataclass
class ToolResultInfo:
    ok: bool
    output: str
    meta: dict | None = None


@dataclass
class StoredSessionMessage:
    role: str  # "user" | "assistant" | "tool"
    content: str
    timestamp: str
    toolCall: ToolCallInfo | None = None
    toolResult: ToolResultInfo | None = None

    def to_dict(self) -> dict:
        d = {"role": self.role, "content": self.content, "timestamp": self.timestamp}
        if self.toolCall is not None:
            d["toolCall"] = dataclasses.asdict(self.toolCall)
        if self.toolResult is not None:
            d["toolResult"] = dataclasses.asdict(self.toolResult)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "StoredSessionMessage":
        tc = d.get("toolCall")
        tr = d.get("toolResult")
        return cls(
            role=d["role"], content=d["content"], timestamp=d["timestamp"],
            toolCall=ToolCallInfo(**tc) if tc else None,
            toolResult=ToolResultInfo(**tr) if tr else None,
        )


@dataclass
class RunStats:
    durationMs: int
    turns: int
    toolCalls: int
    inputTokens: int | None = None
    outputTokens: int | None = None


@dataclass
class RunEntry:
    id: str
    startedAt: str
    status: str = "running"  # "running" | "completed" | "failed" | "cancelled"
    completedAt: str | None = None
    stats: RunStats | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = {"id": self.id, "startedAt": self.startedAt, "status": self.status}
        if self.completedAt is not None:
            d["completedAt"] = self.completedAt
        if self.stats is not None:
            d["stats"] = dataclasses.asdict(self.stats)
        if self.error is not None:
            d["error"] = self.error
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RunEntry":
        stats = d.get("stats")
        return cls(
            id=d["id"], startedAt=d["startedAt"], status=d.get("status", "running"),
            completedAt=d.get("completedAt"),
            stats=RunStats(**stats) if stats else None,
            error=d.get("error"),
        )


@dataclass
class StoredSession:
    id: str
    cwd: str
    title: str
    createdAt: str
    updatedAt: str
    provider: str = "ollama"          # hardcoded — see module docstring
    model: str | None = None
    ollamaUrl: str | None = None      # always None — see module docstring
    lmStudioUrl: str | None = None    # always None — see module docstring
    history: list = field(default_factory=list)   # list[StoredSessionMessage]
    runs: list = field(default_factory=list)       # list[RunEntry]
    version: int = STORED_SESSION_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "id": self.id,
            "cwd": self.cwd,
            "title": self.title,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "provider": self.provider,
            "model": self.model,
            "ollamaUrl": self.ollamaUrl,
            "lmStudioUrl": self.lmStudioUrl,
            "history": [m.to_dict() for m in self.history],
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StoredSession":
        return cls(
            id=d["id"], cwd=d["cwd"], title=d["title"],
            createdAt=d["createdAt"], updatedAt=d["updatedAt"],
            provider=d.get("provider", "ollama"), model=d.get("model"),
            ollamaUrl=d.get("ollamaUrl"), lmStudioUrl=d.get("lmStudioUrl"),
            history=[StoredSessionMessage.from_dict(m) for m in d.get("history", [])],
            runs=[RunEntry.from_dict(r) for r in d.get("runs", [])],
            version=d.get("version", STORED_SESSION_VERSION),
        )


# --- construction / mutation ----------------------------------------------

def create_session(full_session_id: str, cwd: str, model: str | None) -> StoredSession:
    now = _now_iso()
    return StoredSession(
        id=short_id(full_session_id), cwd=cwd, title=default_title(cwd),
        createdAt=now, updatedAt=now, provider="ollama", model=model,
    )


def start_run() -> RunEntry:
    return RunEntry(id=str(uuid.uuid4()), startedAt=_now_iso(), status="running")


def complete_run(run: RunEntry, *, turns: int, tool_calls: int, duration_ms: int,
                  input_tokens: int | None = None, output_tokens: int | None = None) -> None:
    run.status = "completed"
    run.completedAt = _now_iso()
    run.stats = RunStats(durationMs=duration_ms, turns=turns, toolCalls=tool_calls,
                          inputTokens=input_tokens, outputTokens=output_tokens)


def fail_run(run: RunEntry, error: str) -> None:
    run.status = "failed"
    run.completedAt = _now_iso()
    run.error = error


def finish_run_from_messages(run: RunEntry, new_messages: list, duration_ms: int) -> None:
    """Classify and stat one run_turn() call's outcome from the LangChain
    messages it appended (messages[start_index:] after the call).

    - turns = number of AIMessage instances (one per stream_response() hop;
      AIMessageChunk is a subclass of AIMessage so this covers both)
    - toolCalls = total tool_calls across those AIMessages
    - completed iff the last message is an AIMessage with no pending
      tool_calls (a genuine final answer); otherwise failed (last message
      is the HumanMessage itself — stream_response returned None on the
      first hop — or a ToolMessage — stream_response returned None after a
      tool hop, per ui/turn.py's two `if response is None: return` sites).

    Status "cancelled" has no code path today — see docs/sessions.md for
    why (a crash mid-run is still captured honestly: the "running" stub is
    written to disk before run_turn() is called, so an interrupted process
    just leaves that run stuck at status "running").
    """
    turns = sum(1 for m in new_messages if isinstance(m, AIMessage))
    tool_calls = sum(len(m.tool_calls) for m in new_messages if isinstance(m, AIMessage))
    last = new_messages[-1]
    usage = getattr(last, "usage_metadata", None) if isinstance(last, AIMessage) else None
    input_tokens = usage.get("input_tokens") if usage else None
    output_tokens = usage.get("output_tokens") if usage else None

    if isinstance(last, AIMessage) and not last.tool_calls:
        complete_run(run, turns=turns, tool_calls=tool_calls, duration_ms=duration_ms,
                     input_tokens=input_tokens, output_tokens=output_tokens)
    else:
        reason = ("model call failed or returned no output" if not isinstance(last, AIMessage)
                   else "AI stopped after a tool call without a final response")
        fail_run(run, reason)


def append_history_from_messages(session: StoredSession, messages: list, start_index: int) -> None:
    """Convert messages[start_index:] (one run_turn() call's newly appended
    LangChain messages) into StoredSessionMessage entries, appended to
    session.history. SystemMessage is skipped — it has no place in the
    {user, assistant, tool} role enum."""
    for m in messages[start_index:]:
        if isinstance(m, SystemMessage):
            continue
        if isinstance(m, HumanMessage):
            session.history.append(StoredSessionMessage(
                role="user", content=extract_text(m.content), timestamp=_now_iso()))
        elif isinstance(m, ToolMessage):
            output = str(m.content)
            session.history.append(StoredSessionMessage(
                role="tool", content=output, timestamp=_now_iso(),
                toolResult=ToolResultInfo(ok=tool_result_ok(output), output=output)))
        elif isinstance(m, AIMessage):
            text = extract_text(m.content)
            calls = m.tool_calls or [None]
            # StoredSessionMessage.toolCall is singular, but one AIMessage
            # can carry multiple tool_calls in a single hop (ui/turn.py's
            # `for tc in response.tool_calls:`) — split into one history
            # entry per call, text attached only to the first, to keep a
            # 1:1 entry-per-ToolMessage pairing.
            for i, tc in enumerate(calls):
                session.history.append(StoredSessionMessage(
                    role="assistant",
                    content=text if i == 0 else "",
                    timestamp=_now_iso(),
                    toolCall=ToolCallInfo(tool=tc["name"], args=tc["args"]) if tc else None,
                ))


# --- disk I/O ---------------------------------------------------------------
#
# Every write goes through a single background thread consuming a FIFO
# queue.Queue, so save_session() never blocks its caller on disk I/O and
# concurrent saves are always applied in the order they were made (the
# latest call always wins, never overwritten by an out-of-order earlier
# one). flush_pending_writes() is the synchronization point — it blocks
# until every enqueued write has actually hit disk — used by tests, by
# ui/repl.py's one-shot -p mode before it returns, and via atexit below for
# the interactive REPL's eventual exit.

def _write_json_atomic(path: Path, data: dict) -> None:
    """Raises on failure — internal, exercised directly by tests and only
    ever called from the background writer thread (or synchronously by a
    test). Writes to a temp file in SESSIONS_DIR then os.replace()s over
    the target, so a crash mid-write never leaves a truncated/corrupt JSON
    file. Takes an already-serialized `dict`, not a StoredSession, so it
    has no shared mutable state with the caller's (possibly still-live)
    session object."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=SESSIONS_DIR, prefix=".tmp-session-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # ensure_ascii=False: store non-ASCII text (e.g. CJK from tool
            # output or a Chinese prompt) as literal UTF-8 rather than
            # \uXXXX escapes — both are valid JSON and decode identically,
            # this is purely so the file reads as plain text on disk.
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


_write_queue: "queue.Queue[tuple[Path, dict]]" = queue.Queue()
_writer_thread: threading.Thread | None = None
_writer_lock = threading.Lock()


def _writer_loop() -> None:
    while True:
        path, data = _write_queue.get()
        try:
            _write_json_atomic(path, data)
        except Exception:
            # Swallow per-item — a single bad write (disk full, permission
            # denied) must not kill this thread, or every write queued
            # after it would silently never be processed again.
            pass
        finally:
            _write_queue.task_done()


def _ensure_writer_thread() -> None:
    global _writer_thread
    with _writer_lock:
        if _writer_thread is None:
            _writer_thread = threading.Thread(
                target=_writer_loop, daemon=True, name="zeppeli-session-writer")
            _writer_thread.start()


def flush_pending_writes() -> None:
    """Block until every write enqueued so far has been written to disk.
    Returns immediately if nothing has ever been enqueued. Call this
    whenever the caller needs an up-to-date file on disk right now rather
    than eventually — ui/repl.py's one-shot -p mode calls it before
    returning; it's also registered below via atexit for normal interpreter
    shutdown (including an uncaught exception/KeyboardInterrupt reaching
    the top of the script)."""
    _write_queue.join()


atexit.register(flush_pending_writes)


def save_session(session: StoredSession) -> None:
    """Touch updatedAt and enqueue `session` to be written to disk,
    atomically, by the background writer thread — never blocks the caller
    on I/O. Never raises — persistence is a nice-to-have side feature; a
    disk-full, permissions, or serialization bug must never crash the chat
    or interrupt the user's turn, so any failure here is swallowed. Call
    flush_pending_writes() when the write actually needs to be on disk
    before proceeding (e.g. before the process exits)."""
    session.updatedAt = _now_iso()
    try:
        data = session.to_dict()
        _ensure_writer_thread()
        _write_queue.put((session_file_path(session.id), data))
    except Exception:
        pass

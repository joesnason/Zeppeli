# Session history persistence

Every run of this CLI — interactive REPL or one-shot `-p` — is recorded to a
JSON file under `~/.zeppeli/sessions/`, always on, no flag required. This
doc covers the storage format, the lifecycle that writes it, and the
handful of judgment calls made where the on-disk schema doesn't map
cleanly onto anything this codebase already has a concept of.

## Storage location

```
~/.zeppeli/sessions/session-<8charid>.json
```

`<8charid>` is the **first 8 characters** of the process's session UUID
(`ui/repl.py`'s `_session_state["id"] = str(uuid.uuid4())`) — not a
separately minted id. One process, one session, one identity; the toolbar's
`Session ID: <uuid>` line keeps showing the full UUID unchanged, and the
persisted file's `id` field is just a shorter wire format of the same
value (`core/sessions.py`'s `short_id()`).

The directory is created lazily (`SESSIONS_DIR.mkdir(parents=True,
exist_ok=True)`) on first write — nothing touches `~/.zeppeli/` until a
session actually needs saving.

## Schema

`core/sessions.py`'s dataclasses mirror this JSON shape field-for-field
(camelCase, matching the original schema — not translated to snake_case):

```jsonc
{
  "version": 1,
  "id": "a1b2c3d4",
  "cwd": "/Users/x/workspace/ollama",
  "title": "ollama",
  "createdAt": "2026-08-18T09:00:00+00:00",
  "updatedAt": "2026-08-18T09:05:12+00:00",
  "provider": "ollama",
  "model": "gemma4:26b-nvfp4",
  "ollamaUrl": null,
  "lmStudioUrl": null,
  "history": [
    { "role": "user", "content": "list the files here", "timestamp": "..." },
    { "role": "assistant", "content": "", "timestamp": "...",
      "toolCall": { "tool": "list_files", "args": { "path": "." } } },
    { "role": "tool", "content": "total 12\n...", "timestamp": "...",
      "toolResult": { "ok": true, "output": "total 12\n..." } },
    { "role": "assistant", "content": "Here's what's in the directory...", "timestamp": "..." }
  ],
  "runs": [
    { "id": "abb9ed82-...", "startedAt": "...", "completedAt": "...",
      "status": "completed",
      "stats": { "durationMs": 1830, "turns": 2, "toolCalls": 1,
                 "inputTokens": 512, "outputTokens": 94 } }
  ]
}
```

## Lifecycle

- **Created once** per process, in `ui/repl.py`'s `main()`, right after
  `messages = [SystemMessage(...)]` is built — after `load_llm()` and
  (for interactive `--auto-mode`) after the trust gate, so declining trust
  never creates a file. Identical code path for interactive REPL and
  one-shot `-p`.
- **Written immediately at creation** (0 runs, 0 history) — even a session
  that's interrupted before completing a single turn still leaves a file.
- **Updated after every turn** via `ui/repl.py`'s `_run_and_persist()`,
  which wraps `run_turn()` without modifying it:
  1. Appends a `RunEntry` with `status: "running"` and saves — a stub
     written to disk *before* `run_turn()` is called.
  2. Calls `run_turn()` as before.
  3. Diffs `messages` before/after the call, classifies the run
     (`finish_run_from_messages()`) and converts the newly appended
     LangChain messages into `history[]` entries
     (`append_history_from_messages()`), and saves again.
- Every write is **atomic** — the background writer thread (below) writes
  to a temp file in `SESSIONS_DIR`, then `os.replace()`s it over the
  target — so a crash mid-write never leaves a truncated/corrupt JSON
  file on disk.
- **Never crashes the CLI.** `save_session()` swallows every exception
  (disk full, permission denied, serialization bug), and the writer
  thread's per-item `try/except` means one bad write doesn't stop later
  ones from being processed. Session persistence is a nice-to-have side
  feature; a bug in it must never interrupt the actual chat turn already
  shown on screen.

### The write queue

`save_session()` never writes to disk itself — it touches `updatedAt`,
serializes the session to a plain `dict` (`StoredSession.to_dict()`, which
naturally deep-copies everything, decoupling it from `session`'s further
in-place mutation), and enqueues `(path, dict)` onto a module-level
`queue.Queue`. A single background daemon thread (`core/sessions.py`'s
`_writer_loop()`) consumes that queue **sequentially, in FIFO order**, and
does the actual atomic write. This means:

- `save_session()` returns immediately — slow disk I/O (a network-mounted
  home directory, a busy disk) never blocks the chat/turn flow, even though
  `_run_and_persist()` calls it twice per turn.
- Writes for the same session are always applied in the order they were
  made — the most recent `save_session()` call is always the one that ends
  up on disk, never overwritten by an out-of-order earlier one.
- A single failed write doesn't wedge the thread — the consumer's
  `try/except Exception: pass` around each item is what keeps it alive to
  process everything queued after a failure.

**`flush_pending_writes()`** blocks until every write enqueued so far has
actually been written (`queue.Queue.join()`; returns immediately if nothing
was ever enqueued). It's called from two places:

- Explicitly, at the end of `ui/repl.py`'s one-shot `-p` branch, right
  before `main()` returns — `-p` is meant to be deterministic/scriptable
  (a caller may read the session file the instant the process exits), so
  it doesn't rely on `atexit` timing.
- Via `atexit.register(flush_pending_writes)`, registered once at module
  import time — this drains the queue on normal interpreter shutdown,
  which in CPython **also fires when an unhandled exception (including an
  uncaught `KeyboardInterrupt`) propagates to the top of the script** —
  covering a Ctrl+C mid-turn, which today already propagates uncaught out
  of `run_turn()`/`main()`. Verified manually: enqueuing a write and then
  letting an uncaught `KeyboardInterrupt` reach the top of a throwaway
  script still leaves the write on disk.

**Accepted residual risk**: only a hard kill (`SIGKILL`) or a genuine
interpreter crash between a write being enqueued and the writer thread
actually processing it can lose that write — `atexit` never gets a chance
to run in either case. This is the same class of risk a fully-synchronous
design would have anyway if the kill lands mid-`write()`; queuing doesn't
make it meaningfully worse, it just widens the window slightly (queued-but-
not-yet-written vs. mid-write). The atomic-write guarantee itself (no
*corrupted* file) is unaffected either way — the worst case is a file
that's missing its most recent update, never a broken one.

## Message → history conversion

LangChain's `messages` list (plain `HumanMessage`/`AIMessage`/`ToolMessage`
objects, mutated in place by `run_turn()`) maps onto `history[]` as:

| LangChain message | `role` | notes |
|---|---|---|
| `SystemMessage` | *(skipped)* | not in the `{user, assistant, tool}` enum |
| `HumanMessage` | `"user"` | `content` via `core/messages.py`'s `extract_text()` (handles both plain-str and image-attached list content) |
| `AIMessage`, no `tool_calls` | `"assistant"` | one entry |
| `AIMessage`, with `tool_calls` | `"assistant"` × N | **one entry per call** — a single hop can carry multiple tool calls (`ui/turn.py`'s `for tc in response.tool_calls:`); the response text is attached to the first entry only, `""` on the rest, so each entry still pairs 1:1 with its `ToolMessage` |
| `ToolMessage` | `"tool"` | `toolResult.ok` is a heuristic: `False` if the output starts with `"Error:"` (the convention every `@tool` in `core/tools.py` uses to signal failure) or contains `"CANCELLED"` (the literal string `ui/turn.py` puts in a denied-permission result); `True` otherwise |

## Judgment calls made without an existing precedent

This codebase had no prior concept of "provider", a config directory, or a
short session id — these were decided explicitly rather than guessed:

- **`provider` is hardcoded `"ollama"`, always.** There's no real LM Studio
  integration anywhere in this codebase (zero references) — the only other
  backend is a generic litellm-routed cloud/self-hosted model
  (`--base-url`), which is not semantically `"lmstudio"`. Labeling it that
  way would actively mislead anything reading this file later.
- **`ollamaUrl` / `lmStudioUrl` are always `null`.** This codebase has no
  "custom Ollama URL" concept (`ChatOllama` uses its own client
  defaults/env, never threaded through `load_llm()`), and litellm's
  `base_url` isn't semantically either field — especially since `provider`
  is always `"ollama"`, so `lmStudioUrl` would never even be reachable.
  Populating either from `base_url` would be misleading.
- **`title` defaults to the launch directory's basename**
  (`Path(initial_cwd).name`, falling back to the full path string only in
  the `cwd == "/"` edge case where the basename is empty).
- **`"cancelled"` is a reserved, currently-unused `RunEntry.status`.**
  `ui/repl.py`'s REPL loop only catches `KeyboardInterrupt`/`EOFError`
  around the input prompt, not around `run_turn()` — a Ctrl+C mid-turn
  already propagates uncaught out of `main()` today, and this feature
  deliberately doesn't add new exception handling to change that. The
  "running" stub is enqueued *before* `run_turn()` runs, and `atexit` fires
  even on that uncaught exception (see "The write queue" above), so an
  interrupted process still leaves an honest record: that run just stays
  at `status: "running"` with no `completedAt`, rather than needing a
  `"cancelled"` state manufactured after the fact.
- **`timestamp` on each `history[]` entry is persistence time, not model
  generation time** — messages are timestamped when
  `append_history_from_messages()` converts them, all at once right after
  `run_turn()` returns, not incrementally as each one is produced.

## Testing

`test_sessions.py` covers the data shapes, message-conversion rules
(including multi-tool-call splitting and the `ok` heuristic), run
classification, atomic-write behavior (including that `save_session()`
swallows a write failure), the write queue itself (`save_session()` doesn't
block on a slow write, queued writes are applied in FIFO order, the writer
thread survives a failed write and keeps processing later ones,
`flush_pending_writes()` returns immediately on an empty queue), and an
integration test asserting `ui.repl.main()` writes exactly one, fully
up-to-date session file per process (relying on the one-shot branch's
explicit `flush_pending_writes()` call) — all with
`core.sessions.SESSIONS_DIR` redirected to a temp directory, no Ollama or
network dependency. `test_permission_modes.py`'s existing tests that call
`repl.main()` for real also redirect `SESSIONS_DIR` for the same reason —
see that file's module docstring.

Manually verified (2026-08-19): enqueuing a write and then letting an
uncaught `KeyboardInterrupt` propagate to the top of a throwaway script
(simulating a Ctrl+C mid-turn) still leaves the write on disk, via
`atexit`; and a live `-p` run's session file is fully populated with the
completed run's `history`/`stats` the instant the process exits.

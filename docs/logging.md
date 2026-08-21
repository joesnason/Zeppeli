# Event log

Every run of this CLI — interactive REPL or one-shot `-p` — also writes a
second, more granular record: an append-only JSONL event stream under
`~/.zeppeli/logs/`, always on, no flag required. This is a parallel
artifact to [`docs/sessions.md`](sessions.md)'s session-history summary
file — a debug/audit trail of lifecycle events as they happen, rather than
a resumable session summary. This doc covers the storage format, the
lifecycle that writes it, and the judgment calls made where an event's
schema doesn't map cleanly onto anything this codebase already has a
concept of.

## Storage location

```
~/.zeppeli/logs/log-<session-id>.jsonl
```

`<session-id>` is the **full** session UUID (`ui/repl.py`'s
`_session_state["id"] = str(uuid.uuid4())`) — unlike
[`docs/sessions.md`](sessions.md)'s `session-<8charid>.json`, this
filename is **not** truncated to 8 characters. Both files are derived from
the same process-lifetime UUID and live under `~/.zeppeli/`, but are
independent on-disk artifacts with independent lifecycles
(`core/eventlog.py` vs. `core/sessions.py`).

The directory is created lazily (`LOGS_DIR.mkdir(parents=True,
exist_ok=True)`) on first write, same as the sessions directory.

## Event types

Each line is one JSON object: `{"type", "timestamp", "sessionId",
["runId"], "data": {...}}`. `timestamp` is ISO-8601 UTC. `runId` is present
on every event except `session_started` and `cli_error`, which aren't
scoped to a single run.

### `session_started`

Logged once per process, right after the auto-mode trust gate (if any) —
so declining trust never writes a file — but **before** `load_llm()` is
attempted, since a bad model/cloud config is itself a real failure point
this event's sibling, `cli_error`, needs to be able to report.

```jsonc
{
  "type": "session_started",
  "data": {
    "cwd": "/Users/x/workspace/ollama",
    "provider": "ollama",       // "ollama" or "litellm", from whether --base-url was given
    "model": "gemma4:26b-nvfp4",
    "ollamaUrl": "http://127.0.0.1:11434",  // OLLAMA_HOST env var or the client default; null when provider is "litellm"
    "platform": "darwin",
    "pid": 12345,
    "reasoningMode": "enabled",  // "enabled" for local Ollama, "unavailable" for cloud/litellm
    "recoveredInterruptedRuns": 0  // always 0 — see "Known limitations" below
  }
}
```

### `run_started`

Logged once per turn, right after the `RunEntry` stub is created (mirrors
[`docs/sessions.md`](sessions.md)'s "running" stub timing).

```jsonc
{
  "type": "run_started",
  "runId": "9f8e7d6c-...",
  "data": { "promptLength": 28, "promptPreview": "the first 200 chars of the prompt" }
}
```

### `model_activity`

Logged once per `stream_response()` hop (`ui/turn.py`'s tool-call loop
calls it once per pass) — **not** per streamed token. `index` is the
0-based hop position within the run; `finalization` is `true` iff that
hop's response had no `tool_calls` (i.e. it was the run's terminal
answer).

```jsonc
{
  "type": "model_activity",
  "runId": "9f8e7d6c-...",
  "data": {
    "index": 0,
    "finalization": false,
    "chunk": { "thinking": "The user wants X, so I should call tool Y first...", "content": "", "done": true }
  }
}
```

`chunk.thinking` reads `additional_kwargs["reasoning_content"]` off the
merged `AIMessage`. For local Ollama, `ui/repl.py`'s `main()` asks
`ui/streaming.py`'s `stream_response()` to pass `reasoning=True` to
`ChatOllama`'s `.stream()` calls, which is what actually populates this
field — see "Known limitations" below for when it's empty instead.

### `run_completed`

Logged once per turn, right after the run is classified
(`finish_run_from_messages()`). Covers **both** success and failure via
`completionStatus` — there's no separate failure event type.

```jsonc
{
  "type": "run_completed",
  "runId": "9f8e7d6c-...",
  "data": {
    "completionStatus": "completed",  // or "failed"
    "answer": "the final answer text",  // "" when failed
    "answerLength": 28,
    "stats": { "durationMs": 8000, "turns": 2, "toolCalls": 1,
               "inputTokens": 512, "outputTokens": 94 },  // null when failed with no stats
    "turns": [
      { "kind": "tool", "content": "...", "toolCall": { "tool": "read_file", "args": {...} },
        "toolResult": { "ok": true, "output": "..." } },
      { "kind": "final", "content": "the final answer text" }
    ],
    "modelOutputs": [
      { "index": 0, "finalization": false, "contentChars": 0, "thinkingChars": 118, "done": true },
      { "index": 1, "finalization": true, "contentChars": 28, "thinkingChars": 64, "done": true }
    ]
    // "error": "..." — present only when completionStatus is "failed"
  }
}
```

`turns`/`modelOutputs` are built by `core/eventlog.py`'s
`build_turns_and_outputs()` from the same `new_messages` list
[`docs/sessions.md`](sessions.md)'s `finish_run_from_messages()` and
`append_history_from_messages()` already consume — `turns` mirrors
`append_history_from_messages()`'s one-entry-per-tool-call splitting
(`toolResult.ok` uses the same `Error:`/`CANCELLED` heuristic, now shared
as `core/messages.py::tool_result_ok()`), and `modelOutputs` is one summary
per `AIMessage` hop.

### `cli_error`

Logged when an exception escapes `ui/repl.py`'s `main()` (from `load_llm()`
onward — see "Known limitations"). Always re-raised after logging, so the
process's crash/exit-code behavior is unchanged; this only adds a record.

```jsonc
{
  "type": "cli_error",
  "data": { "name": "RuntimeError", "message": "..." }  // message capped ~2000 chars
}
```

## Lifecycle

Mirrors [`docs/sessions.md`](sessions.md)'s design closely — same
background-thread-plus-queue non-blocking model, same "never crash the
chat" swallow-all-exceptions philosophy — but appends JSONL lines instead
of rewriting a whole JSON document:

- A dedicated background daemon thread (`core/eventlog.py`'s
  `_writer_loop()`) consumes a module-level `queue.Queue`, appending one
  line (`open(path, "a").write(json.dumps(line) + "\n")`) per item,
  sequentially, in FIFO order. Since a single thread is the only writer,
  there's no interleaving risk to guard against — no atomic-replace
  machinery needed the way whole-file rewrites need it.
- Every `log_*()` function enqueues and returns immediately — slow disk
  I/O never blocks a turn — and swallows every exception itself, same as
  `save_session()`.
- **`flush_pending_events()`** blocks until the queue is drained. Called
  explicitly at the end of `ui/repl.py`'s one-shot `-p` branch (alongside
  `flush_pending_writes()`), and via `atexit.register(flush_pending_events)`
  for the interactive REPL's eventual exit — including an uncaught
  exception/`KeyboardInterrupt` reaching the top of the script, same as
  `flush_pending_writes()`.
- **Accepted residual risk**: identical to
  [`docs/sessions.md`](sessions.md)'s write-up — only a hard kill or
  interpreter crash between enqueue and the actual write can lose that
  event.

## Known limitations

- **`chunk.thinking` is only populated for local Ollama.** `ui/repl.py`'s
  `main()` computes `reasoning_enabled = base_url is None` once at startup
  and threads it through `_run_and_persist()` → `run_turn()` →
  `stream_response()`, which passes `reasoning=True` to `ChatOllama`'s
  `.stream()` calls when it's set. Manually verified (2026-08-21) against
  `gemma4:26b-nvfp4`: this genuinely separates the model's reasoning into
  `additional_kwargs["reasoning_content"]` without changing the visible
  response, and works fine together with tool-calling (a hop that ends in
  a tool call still gets real `thinking` text, e.g. explaining *why* it's
  calling that tool). If the loaded model doesn't actually support
  reasoning mode, the first attempt's exception is treated as possibly
  reasoning-related: `stream_response()` retries once without it, and
  remembers not to try again for the rest of the process
  (`ui/streaming.py`'s `_reasoning_unsupported`) — so a genuinely
  unsupported model costs one extra failed attempt on the very first hop,
  not every hop of every turn. Cloud/self-hosted models via `--base-url`
  have no equivalent, so `chunk.thinking` is always empty for those.
- **`session_started.recoveredInterruptedRuns` is always `0`.** This
  codebase has no session-resume concept — every process starts a fresh
  session id/log file, so there's never anything to "recover."
- **No `maxTurns` field.** `ui/turn.py`'s tool-call loop is genuinely
  unbounded — no such config exists in this CLI, so the field is omitted
  rather than fabricated.
- **`cli_error` only covers exceptions after a session exists** — from
  `load_llm()` onward inside `ui/repl.py`'s `main()`. Pre-session
  `argparse` validation errors in `cli.py` (bad `--model`/`--base-url`
  combination, a bad `--image` path) already exit cleanly via
  `parser.error()` before any session id is minted, and aren't logged here.

## Testing

`test_eventlog.py` covers every event type's JSONL shape (including
`run_completed`'s completed/failed variants, `cli_error`'s message-length
cap, and `session_started.reasoningMode`'s `"enabled"`/`"unavailable"`
values), `build_turns_and_outputs()` (single and multiple tool calls in
one hop, a final no-tool-call answer, thinking-chars from
`additional_kwargs`), the write queue itself (non-blocking, FIFO order,
the writer thread surviving a bad write, `flush_pending_events()` on an
empty queue), `ui/streaming.py`'s `stream_response()` only emitting
`model_activity` when `session_id`/`run_id`/`turn_index` are all given, and
an integration test asserting a forced exception from `load_llm()` lands
one `cli_error` line before propagating out of `ui.repl.main()` — all with
`core.eventlog.LOGS_DIR` redirected to a temp directory, no Ollama or
network dependency. `test_sessions.py` and `test_permission_modes.py`'s
existing tests that call `repl.main()` for real also redirect `LOGS_DIR`
for the same reason.

`test_streaming.py` separately covers the `reasoning=True` mechanics
itself: it's passed when requested and omitted by default, the
once-per-process fallback (`_reasoning_unsupported`) when the first
attempt raises, that the fallback is remembered across later calls (no
repeated failed retry), and that a failure in both the reasoning attempt
and its fallback still reports an error and returns `None` like before —
all with a fake LLM, no Ollama dependency.

Manually verified (2026-08-21): a live `-p` run against local Ollama
produces `session_started` → `run_started` → one `model_activity` per hop
→ `run_completed`, with `turns`/`modelOutputs`/`stats` matching what
actually happened (one `list_files` tool call, then a final answer). A
second live run with `reasoning=True` wired in (a decimal-comparison
question, `9.11` vs `9.9`) confirmed `session_started.reasoningMode:
"enabled"` and a populated `chunk.thinking` with genuine step-by-step
reasoning, while the visible chat response stayed clean.

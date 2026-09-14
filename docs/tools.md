# Tools

Zeppeli exposes a fixed set of LangChain `@tool`-decorated functions to the model
via `llm.bind_tools([...])`, wired up in `core/agent.py`'s `load_llm()`. This
document describes each tool's signature, behavior, and implementation
details. All tool code lives in `core/tools.py` — see `CLAUDE.md`'s
"Architecture" section for the `core/`/`ui/` layering this file's code sits in.

All tools are defined in `core/tools.py`:

```python
TOOLS = [list_files, glob_files, rg_search, read_file, tail_file, run_bash, write_file, delete_file]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
SLACK_TOOLS = [t for t in TOOLS if t.name != "run_bash"]
```

`TOOLS` feeds `load_llm()`'s `bind_tools()` call; `TOOLS_BY_NAME` is used by
`ui/turn.py`'s `run_turn()` (and `tests/test_tool_call.py`'s `run_agent()`) to look
up and invoke a tool by name once the model requests a call. `SLACK_TOOLS`
is what `slack_bot.py` binds instead of `TOOLS` — `load_llm()` takes an
optional `tools` param for exactly this — since the Slack bot always runs
unattended and `run_bash`'s permission prompts have no one to answer them
(`SlackLive.ask_menu()` always denies; see `docs/slack.md`).

## Path resolution

Every tool argument listed in `PATH_ARGS` is resolved before the tool runs, via
`resolve_paths()` (both in `core/tools.py`):

```python
PATH_ARGS = {
    "list_files": ["path"],
    "glob_files": ["cwd"],
    "rg_search": ["path"],
    "read_file": ["path"],
    "tail_file": ["path"],
    "run_bash": ["cwd"],
    "write_file": ["path"],
    "delete_file": ["path"],
}
```

Resolution rules, applied in order:

1. `~/…` is expanded to the user's home directory (`pathlib.Path.expanduser()`).
2. If the resulting path is still relative, it is joined against `initial_cwd`
   — the directory the CLI was launched from (captured once in `ui/repl.py`'s
   `main()`), not the model's current working directory or any per-turn state.
3. Absolute paths pass through unchanged.

This means the model can pass any of: an absolute path, `~/foo`, or a bare
relative path like `foo/bar.py`, and it will always resolve consistently
regardless of what the shell's cwd happens to be at tool-call time.

## Images are not a tool

Attaching an image (`@path` mention, `/image <path>` command, or `--image`
CLI flag — see [`docs/models.md`](models.md#vision--image-input) for the
wire format) never goes through `TOOLS`/`bind_tools()`. It's message
*content*, built by `core/images.py`'s `build_message_content()` and
appended straight into the `HumanMessage` in `ui/turn.py`'s `run_turn()` —
the model never issues a tool call to see an attached image, and there is
no `view_image` tool.

`core/images.py`'s `resolve_image_path()` deliberately mirrors this file's
`resolve_paths()` rule-for-rule (expanduser → join `initial_cwd` if
relative → absolute passes through unchanged), so `@shots/error.png` and a
`read_file({"path": "shots/error.png"})` tool call resolve to the exact
same absolute path. That equivalence is also why `SYSTEM_PROMPT`
(`core/agent.py`) explicitly warns the model **not** to call `read_file` on
an image path it sees attached — `read_file` opens with
`encoding="utf-8", errors="replace"` (see below), which turns binary image
bytes into mojibake instead of raising a clean error.

## Pre-tool hooks (permission prompts)

Some tools are considered destructive and run through a confirmation hook
before executing. This is a `ui/` concern — `permission_ask()` and the hook
registry both live in `ui/permissions.py`:

```python
PRE_TOOL_HOOKS: dict[str, callable] = {
    "write_file": permission_ask,
    "delete_file": permission_ask,
    "run_bash": permission_ask,
}
```

`PRE_TOOL_HOOKS`'s **keys** are the registry of which tools are gated —
its values are no longer directly invocable: `permission_ask()` is `async`
and needs a `live` argument (`ui/live_region.py`'s `LiveRegion`, or
`SimpleLive` in one-shot `-p` mode) only available at call time, so
`build_pre_tool_hooks()` always binds a fresh `live`-bound closure per call
rather than copying `PRE_TOOL_HOOKS`'s values verbatim. `run_bash`'s entry
here is vestigial like the other two — its actual gating logic is a
dedicated hook (`_make_bash_hook`), not `permission_ask` itself, since its
policy differs from `write_file`/`delete_file`'s (see "run_bash's hook"
below).

`run_turn()` (`ui/turn.py`) doesn't read `PRE_TOOL_HOOKS` directly — it calls
`build_pre_tool_hooks(mode, initial_cwd, live)` once per turn (right after
`reset_turn_approvals()`) and checks the returned dict for every tool call.
If a hook is present for a tool, it is awaited as
`await hook(tool_name, resolved_args, console)` and must return `True` for
the tool to actually run (tool invocation itself is offloaded via
`asyncio.to_thread()` so it never blocks the persistent Application's
event loop — see `ui/CLAUDE.md`). If it returns `False` (or the user
cancels), the tool is skipped and the model receives an explicit
`"[<tool_name>] CANCELLED: the user denied permission..."` message as the
tool result instead of the real output — worded deliberately strongly
(and reinforced by a rule in `SYSTEM_PROMPT`, `core/agent.py`) so the
model reports the cancellation accurately instead of hallucinating
success, which it has been observed to do with a milder message.

### Permission modes

`build_pre_tool_hooks(mode, initial_cwd, live) -> dict[str, callable]` is
the single dispatch point that decides which hooks (if any) apply, based on
the mode chosen at launch (`cli.py`'s `--yolo-mode` / `--auto-mode` flags,
default none of them). The three mode constants live in `ui/permissions.py`:

| Mode | Constant | Behavior |
|------|----------|----------|
| approval (default) | `MODE_APPROVAL` | Binds a fresh `permission_ask`-calling closure to `write_file`/`delete_file` — every call prompts via `permission_ask()`, exactly as described above. `run_bash` is not part of this closure — see below. |
| yolo | `MODE_YOLO` | Returns `{}` — no hooks at all. `hooks.get(tc["name"])` is always `None`, so every tool call (including `run_bash`) runs unguarded, with no prompt of any kind. |
| auto | `MODE_AUTO` | Maps `write_file`/`delete_file` to a hook built by `_make_auto_hook(initial_cwd, live)`. `run_bash` still uses its own hook (below), not this one. |

`_make_auto_hook(initial_cwd, live)` returns an `async` hook that checks
`_is_within_cwd(path, initial_cwd)` (resolves both sides with
`Path.resolve()`, so `..` segments and symlinks are handled correctly): if
the resolved path is inside `initial_cwd`, it auto-approves with a dim note
and no prompt; otherwise it delegates to the real `permission_ask()`, so
calls outside the launch directory still get the full interactive prompt
(including turn/session remember-approval).

#### `run_bash`'s hook

`run_bash` doesn't fit the write/delete pattern above (approval mode always
asks; only auto mode checks scope) — its policy is the *same in both
approval and auto mode* (only yolo mode bypasses it), since it isn't
"destructive" in the write/delete sense so much as "needs a scope/sudo
check regardless of mode." `build_pre_tool_hooks()` gives it its own
closure, `_make_bash_hook(initial_cwd, live)`:

- Auto-runs with no prompt when **all** of the following hold: the
  resolved `cwd` argument is inside `initial_cwd`, no path-like token
  found in the `command` string resolves outside `initial_cwd`, and the
  command doesn't invoke `sudo`.
- Otherwise shows the same three-option menu as `permission_ask()`
  (**Yes** / **Yes, always allow (this session)** / **No**), listing every
  reason it triggered (working directory outside the workspace, a
  referenced path outside the workspace, and/or `sudo`).
- `sudo` detection (`_contains_sudo()`) is a simple `\bsudo\b` regex — not
  a shell parse.
- The workspace-escape check on the command string itself
  (`_command_escapes_cwd()`) is a **heuristic**, not a full shell parse:
  it tokenizes `command` with `shlex.split()`, treats any token containing
  `/` or starting with `~` as a path candidate, resolves each the same way
  `resolve_paths()` does, and flags the first one landing outside
  `initial_cwd`. Absolute paths under `/dev/`, `/tmp/`, `/var/tmp/` are
  exempted (common redirect/scratch targets, e.g. `> /dev/null`, that
  aren't a meaningful workspace escape). Unparsable quoting (a `shlex`
  `ValueError`) is treated as "can't verify" and always prompts, rather
  than silently skipping the check. This won't catch every obfuscation
  (e.g. a path built from a shell variable) — consistent with this
  project's explicit "no sandbox, just a simple confirm-the-scope check"
  design, not a security boundary.
- Approvals are keyed by the exact `command` string (via `_key()`'s
  `path`-or-`command` fallback — `run_bash` calls have no `path` arg), so
  "always allow this session" remembers one specific command, not a
  pattern.
- Not available to the Slack bot at all — see `SLACK_TOOLS` above.

`python3 cli.py -p "<prompt>"` (one-shot mode, see README) goes through the
exact same `run_turn(..., mode)` call as a normal REPL turn — there's no
special-casing per mode for one-shot invocations. See
[`docs/manual-testing.md`](manual-testing.md) for a hands-on checklist that
exercises all three modes via `-p`.

`permission_ask()` first checks whether this exact call was already approved
(see "Approval records" below); if so, it skips straight to `return True`
with a dim note instead of prompting. Otherwise it shows the same
interactive menu as always — three options, same behavior — but the
*mechanism* changed with the persistent-toolbar rendering rewrite: it's now
an in-Layout modal overlay (`live.ask_menu()`, `ui/live_region.py`) on the
same persistent `Application` the toolbar lives in, not a second nested
`Application` (see `ui/CLAUDE.md`'s "Pre-Tool Hooks" section for why the
old approach can't coexist with a persistent toolbar):

- Prints the pending action (`write to` or `delete`) and target path.
- Arrow keys move a `▶` selection cursor between **Yes** / **Yes, always
  allow (this session)** / **No** (default: **Yes**, i.e. `state["idx"] = 0`).
- `Enter` confirms the highlighted option.
- `Esc` moves the cursor to **No** and confirms it — same effect as
  arrowing down to **No** and pressing `Enter`, so the final frame visibly
  shows the selection landing on **No** before the menu closes.
- `Ctrl-C` exits the whole TUI (one consistent app-wide meaning after the
  rendering rewrite — see `ui/CLAUDE.md`'s "UI" section) rather than just
  denying the prompt.
- Choosing **Yes** or **Yes, always allow (this session)** records an approval
  (turn-scoped or session-scoped respectively) before returning `True`.
  Choosing **No** returns `False` without recording anything.

### Approval records

`ui/permissions.py` tracks approvals in two in-memory sets, keyed by
**`(tool_name, path)`** — or `(tool_name, command)` for `run_bash`, which
has no `path` arg — so approving one file's write (or one exact command)
doesn't approve a different file's write (or a different command):

```python
_session_approved: set[tuple[str, str]] = set()
_turn_approved: set[tuple[str, str]] = set()
```

- **turn scope** — cleared by `reset_turn_approvals()`, which `run_turn()`
  (`ui/turn.py`) calls once at the start of every turn, so it never survives
  into the next user input
- **session scope** — never cleared; lives for the process's lifetime only,
  no disk persistence

Denials are never recorded, so a declined call is prompted again on its next
occurrence.

This hook mechanism is generic — new destructive tools can opt in by adding
an entry to `PRE_TOOL_HOOKS`, without changing `run_turn()`. That entry
automatically participates in all three permission modes via
`build_pre_tool_hooks()` — no separate per-mode registration is needed.

## Tool-output truncation

Like the pre-tool hooks above, this is a cross-cutting mechanism applied in
`ui/turn.py`'s `run_turn()` — not a per-tool cap. It runs uniformly on
**every** tool result (all eight tools) and on the CANCELLED
permission-denial message, right before each becomes `ToolMessage` content
sent back to the model:

```python
full_output = str(result)
messages.append(ToolMessage(
    content=truncate_tool_output(full_output),
    tool_call_id=tc["id"],
    additional_kwargs={"full_output": full_output},
))
```

`truncate_tool_output()` (`core/messages.py`) applies two independent rules,
both checked, both able to apply:

1. **Line rule**: if the text has more than 40 lines, keep the first 20 and
   last 20 lines, with a `[truncated N lines]` marker in between (`N` = the
   number of omitted lines).
2. **Char rule**: checked against the result of step 1 — if it still exceeds
   2400 characters, keep the first 1200 and last 1200 characters, with a
   `[truncated N chars]` marker in between (`N` = the number of omitted
   characters).

The two rules are independent, not mutually exclusive: text with 35 lines but
5000 total characters (many short lines) still gets char-truncated even
though the line rule never fired, and text that trips the line rule can
still trip the char rule afterward if the 40 kept lines are still long
enough — in that case the char cut can land on top of (and remove) the line
marker itself, since it operates as a blunt cut over whatever the line rule
produced.

This is a generic backstop layered on top of, not a replacement for, the
per-tool caps described below — `rg_search`'s own `max_bytes` cap,
`read_file`'s pagination, and `tail_file`'s own `max_bytes`-bounded window
all still apply first, and this cap only trims further if their output is
still large. It's also the *only* cap for `list_files`, `glob_files`,
`write_file`, and `delete_file`, which have no per-tool cap of their own.

Truncation only affects what the model sees. The untruncated original is
preserved in `ToolMessage.additional_kwargs["full_output"]`, which
`core/sessions.py`'s `append_history_from_messages()` and
`core/eventlog.py`'s `build_turns_and_outputs()` both read in preference to
`.content` (falling back to `.content` for any `ToolMessage` built without
`full_output`, e.g. `tests/test_tool_call.py`'s own direct construction) — so
session history (`~/.zeppeli/sessions/`) and the event log
(`~/.zeppeli/logs/`) always record the complete, untruncated tool output
even when the model itself only saw a trimmed version.

## Search tools

### `list_files(path=".")`

Lists files and directories at `path` by shelling out to `ls -la`.

```python
subprocess.run(["ls", "-la", path], capture_output=True, text=True)
```

Returns stdout on success, or `"Error: <stderr>"` if `ls` exits non-zero.

### `glob_files(pattern, cwd=".")`

Finds files matching a glob `pattern`, supporting `**` for recursive matches.
Implemented by shelling out to Node.js and using `node:fs/promises`'s
`glob()` async iterator:

```js
const { glob } = require('node:fs/promises');
(async () => {
  const results = [];
  for await (const f of glob(pattern, { cwd })) results.push(f);
  console.log(results.join('\n') || '(no matches)');
})().catch(e => { process.stderr.write(e.message + '\n'); process.exit(1); });
```

The pattern and cwd are JSON-encoded (`json.dumps`) into the generated script
before being passed to `node -e`. Requires Node.js 22+ on `PATH` (this is
the only tool with a Node.js dependency — everything else is pure Python or
a bundled binary).

### `rg_search(pattern, path=".", glob="", max_bytes=50000)`

Searches file contents with ripgrep. Which `rg` binary to invoke
(`RG_BIN`) is resolved once at import time by `core/tools.py`'s
`_find_rg_bin()`:

1. A system-installed `rg` on `PATH` (`shutil.which("rg")`), if present
   — takes priority regardless of platform, so a newer/system version
   is always preferred over a bundled one.
2. Otherwise, a bundled binary matching the current
   `(platform.system(), platform.machine())` — `bin/rg-darwin-arm64`
   (macOS Apple Silicon) or `bin/rg-linux-x86_64` (Linux x86-64), both
   ripgrep v15.1.0 so behavior/output format is identical across
   platforms. Zero setup needed on either of these two platform/arch
   combinations.
3. Otherwise `RG_BIN` is `None` — `rg_search()` returns a friendly
   `"Error: ripgrep ('rg') isn't available for this platform. Install
   it — ..."` message (with the right install command per OS) instead
   of attempting to run anything.

```python
cmd = [RG_BIN, "--no-heading", "--color=never", pattern, path]
if glob:
    cmd += ["--glob", glob]
```

`pattern` supports ripgrep's regex syntax. `glob` filters by filename (e.g.
`*.py`). Returns `"(no matches)"` on a clean empty result, or `"Error: ..."`
if ripgrep exits with code 2 (its convention for a genuine error, as opposed
to code 1 for "no matches").

`subprocess.run(cmd, ...)` itself is wrapped in `try/except OSError` — a
binary that exists but can't actually be launched (wrong-platform
executable format, corrupted file, permissions) raises there rather
than producing a `returncode`, and previously propagated uncaught all
the way up and crashed the whole process (found for real: a Linux
machine running the old single macOS-only `bin/rg` hit `OSError:
[Errno 8] Exec format error`). Now it's caught and returned as a normal
`"Error: couldn't run ripgrep at <path>: ..."` tool-result string.

Output is capped at `max_bytes` (default 50 000, measured as UTF-8-encoded
byte length): a broad pattern against a large file (e.g. a build log with
many `FAILED`/`error:` lines) can otherwise return tens of thousands of
tokens in one call, which — especially against a small-context cloud model
— can blow past the model's context window and raise an unhandled
`ContextWindowExceededError` deep in litellm. If the raw output exceeds
`max_bytes`, it's cut off at that byte offset and a
`[Output truncated at <n> bytes — narrow the pattern or glob to see fewer,
more targeted matches.]` note is appended, telling the model to narrow its
next call rather than silently losing matches. Unlike `read_file`, there's
no pagination/offset mechanism here — `rg_search` doesn't currently support
resuming past the truncation point; narrowing `pattern`/`glob` is the only
way to see the rest. `ui/streaming.py`'s `stream_response()` also catches
and reports (rather than crashes on) any model-call exception that still
makes it through, including this one — see its module docstring.

### `read_file(path, offset=0, limit=400, max_lines=10000, max_bytes=98304)`

Reads a file in bounded chunks so large files can't blow out the model's
context window in one call.

- `offset` is 0-indexed and counts lines already skipped via `f.readline()`
  in a loop (not seek-based, so it's O(offset) per call, not O(1)).
- `limit` is clamped to a hard max of 400 lines per call
  (`limit = min(limit, 400)`).
- The read loop stops early — before reaching `limit` — if it would exceed
  `max_lines` (default 10 000 lines) or `max_bytes` (default 98 304 = 96 KiB,
  measured as UTF-8-encoded byte length per line, accumulated).
- File is opened with `encoding="utf-8", errors="replace"` so non-UTF-8 bytes
  don't raise; they're replaced rather than crashing the tool.

Return format is a header, the raw lines, and a footer:

```
[File: <path> | lines <start>–<end> | <bytes> bytes]
<...file content...>
[More available: use offset=<end> to continue]
```

The footer is one of three variants, communicating to the model how to
paginate:

- `[Stopped: max_lines limit reached — use offset=<n> to continue]` /
  `[Stopped: max_bytes limit reached — use offset=<n> to continue]`
  when a hard limit truncated the read — as explicit about the next
  offset as the "more available" footer below (a hard-limit stop still
  has more to read; a model shouldn't need to infer that from "at line
  <n>" alone — this was previously less explicit, and contributed to a
  small local model failing to reliably continue reading past one).
- `[More available: use offset=<n> to continue]` when the file has more
  lines but no limit was hit (i.e. `limit` was reached first).
- `[End of file]` when there's nothing left to read.

A single line longer than `max_bytes` on its own (e.g. one giant
classpath/command line in a build log) is **truncated and still
included** — with a `[line truncated, N more bytes]` marker — rather
than returned as empty content. It's also still counted as one consumed
line, so the next `offset` always advances past it. Without this, a
line bigger than `max_bytes` left `lines` empty and `end_line` equal to
the call's own `offset`, so every follow-up call at that same offset
repeated the identical `max_bytes` stop forever with no way to read
past it (fixed after a real build-log analysis hit exactly this).

Errors: `FileNotFoundError` and an `offset` past end-of-file both return a
`[read_file] Error: ...` string rather than raising, since tool results must
be strings the model can read.

### `tail_file(path, lines=100, max_bytes=98304)`

Reads the last `lines` lines of a file — e.g. the most recent entries in a
log — without loading the whole file into memory, so it stays fast and
memory-bounded even on multi-GB files. This is a separate tool from
`read_file`, not a mode of it: `read_file`'s `offset`/`limit` is a
forward, resumable-pagination contract, while tail reading is a genuinely
different algorithm (seek from the end, read backward-bounded) — folding
both into one function would force `offset`/footer semantics to mean two
different things depending on a mode flag.

Algorithm: seek to `max(0, filesize - max_bytes)`, read forward to EOF
(bounded to at most `max_bytes` bytes, opened in binary mode so the seek
is byte-precise), decode with `errors="replace"` (matching `read_file`'s
approach), then:

```python
filesize = p.stat().st_size
seek_pos = max(0, filesize - max_bytes)
with open(path, "rb") as f:
    f.seek(seek_pos)
    raw = f.read()
```

- If `seek_pos > 0` (the window doesn't start at the true beginning of the
  file), the first line of that window is very likely partial — cut
  mid-line by the seek — so it's dropped, the same way real `tail`
  discards a partial leading line after a backward seek. This is
  unconditional, even on the rare exact-line-boundary seek: a known,
  accepted one-line-short bias, safer than risking a corrupted fragment.
- If the window contains no newline at all (`max_bytes` smaller than the
  file's actual last line), the whole window is that one partial line —
  it's dropped entirely rather than kept as a truncated slice (unlike
  `read_file`'s oversized-single-line handling). There's no offset/
  pagination state here that could get stuck in a loop the way
  `read_file`'s old bug did, so the fix is simply "call again with a
  larger `max_bytes`," which the footer says explicitly.
- The last `lines` lines of whatever remains are kept.

Return format mirrors `read_file`'s header/footer style:

```
[File: <path> | last <N> lines | <bytes> bytes]
<...file content...>
[Showing last <N> lines]
```

The footer is one of three variants:

- `[Showing last N lines]` — the full requested line count was found.
- `[Beginning of file reached — file has only M lines]` — the window
  reached the true start of the file (`seek_pos == 0`) and the file
  genuinely has fewer than `lines` lines total; not a truncation artifact.
- `[Only found M of requested N lines within the last max_bytes bytes of
  the file — increase max_bytes to search further back]` — the
  `max_bytes` window (after the partial-first-line drop) came up short of
  `lines`, but more of the file exists before it.

Errors: file not found, path is a directory, or any other exception all
return a `[tail_file] Error: ...` string rather than raising, matching
`read_file`/`delete_file`'s style.

## Shell execution tool

### `run_bash(command, cwd=".", timeout=120)`

Executes an arbitrary shell command via `bash -c` (or `direnv exec ... bash
-c` — see "direnv / `.envrc`" below):

```python
cmd = [DIRENV_BIN, "exec", cwd, "bash", "-c", command] if DIRENV_BIN else ["bash", "-c", command]
result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
```

- `cwd` (default `"."`, resolved like every other `PATH_ARGS` entry) is the
  working directory the command runs in.
- `timeout` (default 120 seconds) kills the process and returns
  `"Error: command timed out after <n>s"` if it hasn't finished in time.
- `subprocess.run(...)` is wrapped in `try/except OSError` (same convention
  as `rg_search`) — a launch failure returns `"Error: couldn't run bash:
  ..."` instead of crashing the process.
- Returns combined `stdout`+`stderr` (stripped, capped at 50 000 bytes —
  same byte-cap style as `rg_search`), or `"(no output)"` if both are
  empty. If the exit code is non-zero, it's prefixed as
  `(exit code <n>)\n<output>` so the model can see the command failed
  without having to parse the output for clues.

This is **not a sandbox** — the tool itself places no restriction on what
the command can do. The only gate is the pre-tool hook described in
"Pre-tool hooks" above: a command whose working directory or a referenced
path falls outside the current workspace, or that invokes `sudo`, prompts
the user for approval first; an in-scope, non-`sudo` command runs
immediately, same as any other tool. Not bound to the model in the Slack
bot (`SLACK_TOOLS` excludes it) — the Slack bot always runs unattended, and
`SlackLive.ask_menu()` always denies, so a Slack-bound `run_bash` could
never actually run once it needed a prompt.

#### direnv / `.envrc`

`DIRENV_BIN = shutil.which("direnv")` is resolved once at import, exactly
like `RG_BIN`. Whenever it's set, `run_bash` wraps the command:

```python
cmd = [DIRENV_BIN, "exec", cwd, "bash", "-c", command] if DIRENV_BIN else ["bash", "-c", command]
```

Three deliberate constraints, all mirroring direnv's own real behavior
rather than reimplementing or overriding it:

- **No auto-install.** If `direnv` isn't on `PATH`, `run_bash` uses plain
  `bash -c` — that absence means the user isn't using this workflow at all.
- **No bypassing `direnv allow`.** Whether an `.envrc` actually gets loaded
  is entirely direnv's own decision.
- **No custom `.envrc` discovery.** `direnv exec` already implements the
  real directory-walk-up lookup and the allow/deny check — reimplementing
  that here would risk quietly diverging from direnv's actual rules.

**The blocked-`.envrc` fallback** — verified against real `direnv` (v2.37.1)
rather than assumed: when an `.envrc` exists but hasn't been `direnv
allow`-ed, `direnv exec` does **not** just skip loading it and run the
command anyway — it refuses to run anything at all, exiting non-zero with
`direnv: error <path>/.envrc is blocked. Run 'direnv allow' to approve its
content` on stderr. Since the command must still run either way (the user's
requirement is "don't load the env," not "don't run the command"),
`run_bash` detects this specific message (`"is blocked"` and `"direnv
allow"` both present in stderr, on a non-zero exit) and retries with a
plain `["bash", "-c", command]` — deliberately without direnv, so the
environment genuinely stays unloaded, matching what direnv itself refused
to do:

```python
if DIRENV_BIN and result.returncode != 0 and "is blocked" in result.stderr and "direnv allow" in result.stderr:
    result, error = _run_subprocess(["bash", "-c", command], cwd, timeout)
```

In the normal (allowed or no-`.envrc`) case, `direnv exec`'s own stderr
(e.g. its "loading ~/.../.envrc" note) flows through the same combined
stdout+stderr capture as any other command output, visible to the
model/user like anything else `run_bash` runs.

## File editing tools

`write_file`/`delete_file` are gated by the `permission_ask` pre-tool hook
described above — the user must interactively approve every write or
delete.

### `write_file(path, content)`

Creates a new file or replaces the entire contents of an existing one.

```python
p = pathlib.Path(path)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(content, encoding="utf-8")
```

Intermediate directories are created automatically. There is no partial-edit
or append mode — every call replaces the full file content. Returns
`"Wrote <n> bytes to <path>"` or `"Error: ..."`.

### `delete_file(path)`

Deletes a single file. Explicitly refuses to delete directories as a safety
guard, since a generic recursive-delete tool would be far more dangerous to
expose to a model:

```python
if not p.exists():
    return f"[delete_file] Error: file not found: {path}"
if p.is_dir():
    return f"[delete_file] Error: {path} is a directory, not a file"
p.unlink()
```

## Adding a new tool

1. Define it with `@tool` in `core/tools.py` and give it a clear docstring —
   the docstring is what the model sees as the tool description.
2. Add it to the `TOOLS` list in `core/tools.py` — this alone updates
   `TOOLS_BY_NAME` and `load_llm()`'s `bind_tools()` call, so `cli.py`,
   `ui/turn.py`, and `tests/test_tool_call.py` all pick it up automatically.
3. Describe it in `SYSTEM_PROMPT` (`core/agent.py`) so the model knows when to
   reach for it.
4. If it takes a filesystem path argument, add an entry to `PATH_ARGS`
   (`core/tools.py`) so `resolve_paths()` normalizes it.
5. If it's destructive/irreversible, register it in `PRE_TOOL_HOOKS`
   (`ui/permissions.py`; reuse `permission_ask` or write a new hook with the
   same `(tool_name, args, console) -> bool` signature). It then
   automatically participates in all three permission modes (approval/
   yolo/auto) via `build_pre_tool_hooks()` — no extra per-mode wiring needed.
6. Document it here in `docs/tools.md`.

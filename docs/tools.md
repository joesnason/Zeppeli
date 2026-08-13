# Tools

Zeppeli exposes a fixed set of LangChain `@tool`-decorated functions to the model
via `llm.bind_tools([...])`, wired up in `core/agent.py`'s `load_llm()`. This
document describes each tool's signature, behavior, and implementation
details. All tool code lives in `core/tools.py` — see `CLAUDE.md`'s
"Architecture" section for the `core/`/`ui/` layering this file's code sits in.

All tools are defined in `core/tools.py`:

```python
TOOLS = [list_files, glob_files, rg_search, read_file, write_file, delete_file]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
```

`TOOLS` feeds `load_llm()`'s `bind_tools()` call; `TOOLS_BY_NAME` is used by
`ui/turn.py`'s `run_turn()` (and `test_tool_call.py`'s `run_agent()`) to look
up and invoke a tool by name once the model requests a call.

## Path resolution

Every tool argument listed in `PATH_ARGS` is resolved before the tool runs, via
`resolve_paths()` (both in `core/tools.py`):

```python
PATH_ARGS = {
    "list_files": ["path"],
    "glob_files": ["cwd"],
    "rg_search": ["path"],
    "read_file": ["path"],
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

## Pre-tool hooks (permission prompts)

Some tools are considered destructive and run through a confirmation hook
before executing. This is a `ui/` concern — `permission_ask()` and the hook
registry both live in `ui/permissions.py`:

```python
PRE_TOOL_HOOKS: dict[str, callable] = {
    "write_file": permission_ask,
    "delete_file": permission_ask,
}
```

`run_turn()` (`ui/turn.py`) doesn't read `PRE_TOOL_HOOKS` directly — it calls
`build_pre_tool_hooks(mode, initial_cwd)` once per turn (right after
`reset_turn_approvals()`) and checks the returned dict for every tool call.
If a hook is present for a tool, it is invoked with
`(tool_name, resolved_args, console)` and must return `True` for the tool to
actually run. If it returns `False` (or the user cancels), the tool is
skipped and the model receives an explicit `"[<tool_name>] CANCELLED: the
user denied permission..."` message as the tool result instead of the real
output — worded deliberately strongly (and reinforced by a rule in
`SYSTEM_PROMPT`, `core/agent.py`) so the model reports the cancellation
accurately instead of hallucinating success, which it has been observed to
do with a milder message.

### Permission modes

`build_pre_tool_hooks(mode, initial_cwd) -> dict[str, callable]` is the
single dispatch point that decides which hooks (if any) apply, based on the
mode chosen at launch (`cli.py`'s `--yolo-mode` / `--auto-mode` flags, default
none of them). The three mode constants live in `ui/permissions.py`:

| Mode | Constant | Behavior |
|------|----------|----------|
| approval (default) | `MODE_APPROVAL` | Returns `dict(PRE_TOOL_HOOKS)` — every `write_file`/`delete_file` call prompts via `permission_ask()`, exactly as described above. |
| yolo | `MODE_YOLO` | Returns `{}` — no hooks at all. `hooks.get(tc["name"])` is always `None`, so every tool call runs unguarded, with no prompt of any kind. |
| auto | `MODE_AUTO` | Returns `write_file`/`delete_file` mapped to a hook built by `_make_auto_hook(initial_cwd)`. |

`_make_auto_hook(initial_cwd)` returns a hook that checks
`_is_within_cwd(path, initial_cwd)` (resolves both sides with
`Path.resolve()`, so `..` segments and symlinks are handled correctly): if
the resolved path is inside `initial_cwd`, it auto-approves with a dim note
and no prompt; otherwise it delegates to the real `permission_ask()`, so
calls outside the launch directory still get the full interactive prompt
(including turn/session remember-approval).

`python3 cli.py -p "<prompt>"` (one-shot mode, see README) goes through the
exact same `run_turn(..., mode)` call as a normal REPL turn — there's no
special-casing per mode for one-shot invocations. See
[`docs/manual-testing.md`](manual-testing.md) for a hands-on checklist that
exercises all three modes via `-p`.

`permission_ask()` first checks whether this exact call was already approved
(see "Approval records" below); if so, it skips straight to `return True`
with a dim note instead of prompting. Otherwise it renders an interactive
prompt_toolkit menu with three options:

- Prints the pending action (`write to` or `delete`) and target path.
- Arrow keys move a `▶` selection cursor between **Yes** / **Always allow
  (this session)** / **No** (default: **No**, i.e. `state["idx"] = 2`).
- `Enter` confirms the highlighted option.
- `Ctrl-C` cancels (treated as **No** / deny).
- Choosing **Yes** or **Always allow (this session)** records an approval
  (turn-scoped or session-scoped respectively) before returning `True`.
  Choosing **No** returns `False` without recording anything.

### Approval records

`ui/permissions.py` tracks approvals in two in-memory sets, keyed by
**`(tool_name, path)`** so approving one file's write doesn't approve a
different file's:

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

### `rg_search(pattern, path=".", glob="")`

Searches file contents with ripgrep, using the binary bundled at `bin/rg`
(`RG_BIN`, defined in `core/tools.py` as
`Path(__file__).parent.parent / "bin" / "rg"` — one `.parent` to get out of
`core/` plus one to reach the repo root — so it resolves correctly regardless
of launch cwd):

```python
cmd = [RG_BIN, "--no-heading", "--color=never", pattern, path]
if glob:
    cmd += ["--glob", glob]
```

`pattern` supports ripgrep's regex syntax. `glob` filters by filename (e.g.
`*.py`). Returns `"(no matches)"` on a clean empty result, or `"Error: ..."`
if ripgrep exits with code 2 (its convention for a genuine error, as opposed
to code 1 for "no matches").

The bundled binary is built for `aarch64-apple-darwin` — it will not run on
other platforms/architectures without swapping the binary at `bin/rg`.

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

- `[Stopped: max_lines limit reached at line <n>]` / `[Stopped: max_bytes limit reached at line <n>]`
  when a hard limit truncated the read.
- `[More available: use offset=<n> to continue]` when the file has more
  lines but no limit was hit (i.e. `limit` was reached first).
- `[End of file]` when there's nothing left to read.

Errors: `FileNotFoundError` and an `offset` past end-of-file both return a
`[read_file] Error: ...` string rather than raising, since tool results must
be strings the model can read.

## File editing tools

Both of these are gated by the `permission_ask` pre-tool hook described
above — the user must interactively approve every write or delete.

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
   `ui/turn.py`, and `test_tool_call.py` all pick it up automatically.
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

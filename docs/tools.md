# Tools

Zeppeli exposes a fixed set of LangChain `@tool`-decorated functions to the model
via `llm.bind_tools([...])` in `cli.py`. This document describes each tool's
signature, behavior, and implementation details.

All tools are defined in `cli.py` and registered in `run_turn()` / `main()`:

```python
tools = {t.name: t for t in [list_files, glob_files, rg_search, read_file, write_file, delete_file]}
```

## Path resolution

Every tool argument listed in `PATH_ARGS` is resolved before the tool runs, via
`resolve_paths()`:

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
   — the directory `cli.py` was launched from (captured once in `main()`), not
   the model's current working directory or any per-turn state.
3. Absolute paths pass through unchanged.

This means the model can pass any of: an absolute path, `~/foo`, or a bare
relative path like `foo/bar.py`, and it will always resolve consistently
regardless of what the shell's cwd happens to be at tool-call time.

## Pre-tool hooks (permission prompts)

Some tools are considered destructive and run through a confirmation hook
before executing. The hook registry:

```python
PRE_TOOL_HOOKS: dict[str, callable] = {
    "write_file": permission_ask,
    "delete_file": permission_ask,
}
```

`run_turn()` checks this dict for every tool call: if a hook is registered, it
is invoked with `(tool_name, resolved_args, console)` and must return `True`
for the tool to actually run. If it returns `False` (or the user cancels), the
tool is skipped and the model receives `"[<tool_name>] Cancelled by user."` as
the tool result instead of the real output.

`permission_ask()` renders an interactive prompt_toolkit yes/no menu:

- Prints the pending action (`write to` or `delete`) and target path.
- Arrow keys move a `▶` selection cursor between **Yes** / **No** (default:
  **No**, i.e. `state["idx"] = 1`).
- `Enter` confirms the highlighted option.
- `Ctrl-C` cancels (treated as **No**).

This hook mechanism is generic — new destructive tools can opt in by adding
an entry to `PRE_TOOL_HOOKS`, without changing `run_turn()`.

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
(`RG_BIN`, resolved relative to `cli.py`'s own directory so it works
regardless of launch cwd):

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

1. Define it with `@tool` in `cli.py` and give it a clear docstring — the
   docstring is what the model sees as the tool description.
2. Add it to the `tools` dict build in `run_turn()` and the `bind_tools([...])`
   call in `main()`.
3. Describe it in `SYSTEM_PROMPT` so the model knows when to reach for it.
4. If it takes a filesystem path argument, add an entry to `PATH_ARGS` so
   `resolve_paths()` normalizes it.
5. If it's destructive/irreversible, register it in `PRE_TOOL_HOOKS` (reusing
   `permission_ask` or writing a new hook with the same
   `(tool_name, args, console) -> bool` signature).
6. Document it here in `docs/tools.md`.

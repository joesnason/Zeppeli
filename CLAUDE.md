# Ollama CLI Chat — Claude Code Instructions

## Project Overview

Local LLM chat CLI using Ollama + LangChain. Entry point is `cli.py` (interactive REPL); `test_tool_call.py` is a batch test script.

## Environment

- Python 3.12, venv at `.venv/` (activated via `.envrc`)
- Use `pip3` for package installs
- Ollama must be running locally before executing any script

## Key Files

- `cli.py` — main CLI: REPL loop, Rich markdown streaming, multi-turn history, tool calling
- `test_tool_call.py` — batch runner for testing tool call behavior
- `bin/rg` — bundled ripgrep v15.1.0 binary (aarch64-apple-darwin); used by `rg_search` tool

## Model

Default: `gemma4:26b-nvfp4`. To change, update `MODEL` in the relevant file.

## Running

```bash
python3 cli.py
```

## UI

The CLI runs in normal terminal mode (no alternate screen), so the terminal's native scrollback works. Layout per turn:

```
[AI response rendered as Markdown, streamed live via rich.live.Live]
                                ← blank line after AI response
──────────────────────────────  ← Rule (Rich) above the input prompt
> user message                  ← orange, replaces the typed line in-place
──────────────────────────────  ← bottom_toolbar line 1: rule below input
                                ← bottom_toolbar lines 2–N: slash command hints
```

- Input is handled by `prompt_toolkit.PromptSession` with an orange `>` prompt (`fg:#ff8700 bold`)
- The input area is framed by a Rule above (`console.print(Rule())`) and a rule below (first line of `_get_toolbar()`)
- AI responses are separated from the next input frame by a blank line only — no Rule around responses
- The bottom toolbar is **always a fixed height** (`1 + len(SLASH_COMMANDS)` lines) to prevent blank-line rendering artifacts; empty slots are padded with `""`
- Typing `/` filters `SLASH_COMMANDS` and shows matching commands below the toolbar rule, one per line
- User input is echoed in bold orange after Enter (typed line replaced via ANSI `\x1b[A\x1b[2K`)
- A `Thinking...` dots spinner (`console.status`) is shown from Enter until the first content token arrives, then transitions to Live markdown
- AI responses stream token-by-token and re-render as Markdown in real time (`rich.live.Live`)
- Tool calls appear as dim `[tool: name(args)]` lines between responses

## Slash Commands

Defined in `SLASH_COMMANDS` list at the top of `cli.py`. To add a new command:
1. Append the string to `SLASH_COMMANDS` (e.g. `"/clear"`)
2. Add the corresponding `if user_input == "/clear":` branch in `main()`

Current commands: `/exit`, `/quit`

## Working Directory

At startup, `main()` captures `initial_cwd = pathlib.Path.cwd()` (the directory the user launched from). This value is:
- Injected into the `SystemMessage` as `Working directory: <path>` so the model is aware of context
- Passed to every `run_turn()` call, which resolves relative path arguments via `resolve_paths()` before invoking any tool

`resolve_paths()` uses the `PATH_ARGS` map to know which argument of each tool is a path, and resolves it with `pathlib.Path(cwd) / arg` if not already absolute. Absolute paths are passed through unchanged.

## Available Tools

| Tool | Implementation | Notes |
|------|---------------|-------|
| `list_files(path)` | `subprocess` → `ls -la` | relative `path` resolved against `initial_cwd` |
| `glob_files(pattern, cwd)` | `subprocess` → `node -e` using `node:fs/promises` `glob` | relative `cwd` resolved against `initial_cwd`; requires Node.js 22+ |
| `rg_search(pattern, path, glob)` | `subprocess` → `bin/rg` | relative `path` resolved against `initial_cwd`; uses bundled binary |
| `read_file(path, offset, limit, max_lines, max_bytes)` | pure Python `open()` | relative `path` resolved against `initial_cwd`; 400 lines/call max; returns next `offset` hint |
| `write_file(path, content)` | pure Python `pathlib.Path.write_text` | relative `path` resolved against `initial_cwd`; creates intermediate directories; overwrites existing content |

## Key Functions (`cli.py`)

| Function | Purpose |
|---|---|
| `stream_response(llm, messages, console)` | Shows `Thinking...` spinner, then streams one LLM response as live Markdown; returns accumulated `AIMessage` |
| `run_turn(llm, messages, user_input, console, initial_cwd)` | Appends user message, calls `stream_response`, resolves tool paths, handles the tool-call loop |
| `resolve_paths(tool_name, args, cwd)` | Resolves relative path args against `cwd` before tool invocation; uses `PATH_ARGS` map |
| `_get_toolbar()` | prompt_toolkit bottom_toolbar callback; always returns fixed-height string (rule + padded command lines) |
| `main()` | REPL: captures `initial_cwd`, creates `PromptSession`, prints Rules around each turn, replaces typed line with orange echo |

## Adding Tools

Define new tools with the `@tool` decorator in `cli.py`, then:
1. Add to the `tools` dict in `run_turn()` (and `run_agent()` in `test_tool_call.py`)
2. Add to the `bind_tools([...])` call in both files
3. Mention in `SYSTEM_PROMPT` (both files share the same prompt text)
4. If the tool accepts a path argument, add it to the `PATH_ARGS` dict (e.g. `"my_tool": ["path"]`)

## Testing

Run `test_tool_call.py` to verify tool calling works correctly across a fixed set of prompts:

```bash
python3 test_tool_call.py
```

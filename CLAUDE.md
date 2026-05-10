# Ollama CLI Chat — Claude Code Instructions

## Project Overview

Local LLM chat CLI using Ollama + LangChain. Entry point is `cli.py` (interactive REPL); `test_tool_call.py` is a batch test script.

## Environment

- Python 3.12, venv at `.venv/` (activated via `.envrc`)
- Use `pip3` for package installs
- Ollama must be running locally before executing any script

## Key Files

- `cli.py` — main CLI: REPL loop, streaming, multi-turn history, tool calling
- `test_tool_call.py` — batch runner for testing tool call behavior
- `bin/rg` — bundled ripgrep v15.1.0 binary (aarch64-apple-darwin); used by `rg_search` tool

## Model

Default: `gemma4:26b-nvfp4`. To change, update `MODEL` in the relevant file.

## Running

```bash
python3 cli.py
```

## Available Tools

| Tool | Implementation | Notes |
|------|---------------|-------|
| `list_files(path)` | `subprocess` → `ls -la` | |
| `glob_files(pattern, cwd)` | `subprocess` → `node -e` using `node:fs/promises` `glob` | Requires Node.js 22+ |
| `rg_search(pattern, path, glob)` | `subprocess` → `bin/rg` | Uses bundled binary; no system `rg` needed |
| `read_file(path, offset, limit, max_lines, max_bytes)` | pure Python `open()` | 400 lines/call max; stops at 10 000 lines or 96 KB; returns next `offset` hint |

## Adding Tools

Define new tools with the `@tool` decorator in `cli.py`, then:
1. Add to the `tools` dict in `run_turn()` (and `run_agent()` in `test_tool_call.py`)
2. Add to the `bind_tools([...])` call
3. Mention in `SYSTEM_PROMPT`

## Testing

Run `test_tool_call.py` to verify tool calling works correctly across a fixed set of prompts:

```bash
python3 test_tool_call.py
```

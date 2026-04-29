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

## Model

Default: `gemma4:26b-nvfp4`. To change, update `MODEL` in the relevant file.

## Running

```bash
python3 cli.py
```

## Adding Tools

Define new tools with the `@tool` decorator in `cli.py`, then add them to the `bind_tools([...])` call and mention them in `SYSTEM_PROMPT`.

## Testing

Run `test_tool_call.py` to verify tool calling works correctly across a fixed set of prompts:

```bash
python3 test_tool_call.py
```

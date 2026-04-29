# Zeppeli

Interactive terminal chat interface powered by a local [Ollama](https://ollama.com) model with tool-calling support.

## Features

- Multi-turn conversation — AI remembers context across turns
- Streaming output — responses print token by token
- Tool calling — AI can list filesystem contents when relevant
- Slash commands: `/exit` to quit

## Requirements

- [Ollama](https://ollama.com) running locally
- The model pulled: `ollama pull gemma4:26b-nvfp4`
- Python 3.12+

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install langchain-ollama langchain-core ollama
```

Or if you use [direnv](https://direnv.net/), `.envrc` activates the venv automatically.

## Usage

```bash
python3 cli.py
```

```
Ollama Chat (gemma4:26b-nvfp4)  — type 'quit' or Ctrl+C to exit

You> 現在目錄下有哪些檔案？
  [tool: list_files({'path': '.'})]
Zeppeli> 目前目錄下有以下檔案：...

You> /exit
Bye!
```

## Files

| File | Purpose |
|------|---------|
| `cli.py` | Interactive CLI entry point |
| `test_tool_call.py` | Batch test script for tool calling |

## Exit

Type `quit`, `exit`, `/exit`, or press `Ctrl+C` / `Ctrl+D`.

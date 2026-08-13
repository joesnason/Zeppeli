# Zeppeli

Interactive terminal chat interface powered by a local [Ollama](https://ollama.com) model with tool-calling support.

## Features

- Multi-turn conversation — AI remembers context across turns
- Streaming output — responses print token by token
- Tool calling — AI can search/inspect files and edit (write/delete) files when relevant
- Slash commands: `/exit` to quit

## Requirements

- [Ollama](https://ollama.com) running locally
- The model pulled: `ollama pull gemma4:26b-nvfp4`
- Python 3.12+
- Node.js 22+ (for `glob_files` tool)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

Or if you use [direnv](https://direnv.net/), `.envrc` activates the venv automatically.

## Usage

```bash
python3 cli.py
```

By default, every write/delete asks for confirmation (see
[`docs/tools.md`](docs/tools.md) for details). Two flags loosen that:

- `python3 cli.py --yolo-mode` — never ask, run every tool call immediately
- `python3 cli.py --auto-mode` — auto-approve writes/deletes inside the
  launch directory; still ask for anything outside it

(`--yolo-mode` and `--auto-mode` are mutually exclusive.)

- `python3 cli.py -p "<prompt>"` (or `--prompt`) — run one turn
  non-interactively with `<prompt>` as input, print the response, and exit
  (skips the REPL). Combine with `--yolo-mode`/`--auto-mode` as needed —
  handy for quickly testing a permission mode without sitting in the REPL.

```
Ollama Chat (gemma4:26b-nvfp4)  — type 'quit' or Ctrl+C to exit

You> 現在目錄下有哪些檔案？
  [tool: list_files({'path': '.'})]
Zeppeli> 目前目錄下有以下檔案：...

You> 找出所有 Python 檔案
  [tool: glob_files({'pattern': '**/*.py'})]
Zeppeli> 找到以下 Python 檔案：...

You> 搜尋所有含有 @tool 的地方
  [tool: rg_search({'pattern': '@tool', 'glob': '*.py'})]
Zeppeli> 在以下位置找到 @tool：...

You> 讀取 cli.py 的前 20 行
  [tool: read_file({'path': 'cli.py', 'limit': 20})]
Zeppeli> 以下是 cli.py 的前 20 行：...

You> 建立一個 hello.txt，內容是 "Hello, world!"
  [tool: write_file({'path': 'hello.txt', 'content': 'Hello, world!'})]
Zeppeli> 已建立 hello.txt，寫入 13 bytes。

You> 刪除 hello.txt
  [tool: delete_file({'path': 'hello.txt'})]
Zeppeli> 已刪除 hello.txt。

You> /exit
Bye!
```

## Tools

The AI has access to tools for searching/inspecting files and for editing
(write/delete) files, with destructive actions requiring interactive
confirmation. See [`docs/tools.md`](docs/tools.md) for the full list and
implementation details.

## Files

| File | Purpose |
|------|---------|
| `cli.py` | Interactive CLI entry point |
| `core/` | AI agent/model layer — tool definitions, path resolution, Ollama loading |
| `ui/` | User interaction layer — REPL loop, streaming/Markdown rendering, permission prompts |
| `test_tool_call.py` | Batch test script for tool calling |
| `test_permission_modes.py` | Automated tests for permission-mode logic — no Ollama needed |
| `requirements.txt` | Python dependencies (`pip3 install -r requirements.txt`) |
| `bin/rg` | Bundled ripgrep binary (aarch64-apple-darwin) |
| `docs/` | Implementation details (tool internals, etc.) — see also [`docs/manual-testing.md`](docs/manual-testing.md) |

## Exit

Type `quit`, `exit`, `/exit`, or press `Ctrl+C` / `Ctrl+D`.

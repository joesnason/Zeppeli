# Zeppeli

Interactive terminal chat interface powered by a local [Ollama](https://ollama.com) model with tool-calling support.

## Features

- Multi-turn conversation — AI remembers context across turns
- Streaming output — responses print token by token
- Tool calling — AI can inspect, search, and write files when relevant
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

You> /exit
Bye!
```

## Tools

Path arguments accept absolute paths, `~/…` (home-relative), or relative paths (resolved against the launch directory).

| Tool | Description |
|------|-------------|
| `list_files(path)` | List files and directories via `ls -la` |
| `glob_files(pattern, cwd)` | Find files by glob pattern via Node.js `fs.glob`; supports `**` |
| `rg_search(pattern, path, glob)` | Search file contents with ripgrep (regex supported); uses bundled `bin/rg` |
| `read_file(path, offset, limit, max_lines, max_bytes)` | Read a file in chunks of up to 400 lines; use `offset` to paginate; stops at 10 000 lines or 96 KB |
| `write_file(path, content)` | Create a new file or replace all content in an existing file; creates intermediate directories automatically |

## Files

| File | Purpose |
|------|---------|
| `cli.py` | Interactive CLI entry point |
| `test_tool_call.py` | Batch test script for tool calling |
| `bin/rg` | Bundled ripgrep binary (aarch64-apple-darwin) |

## Exit

Type `quit`, `exit`, `/exit`, or press `Ctrl+C` / `Ctrl+D`.

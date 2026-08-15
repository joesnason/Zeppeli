# Zeppeli

Interactive terminal chat interface powered by a local [Ollama](https://ollama.com) model with tool-calling support.

## Features

- Multi-turn conversation — AI remembers context across turns
- Streaming output — responses print token by token
- Tool calling — AI can search/inspect files and edit (write/delete) files when relevant
- Slash commands: `/exit` to quit

## Requirements

- [Ollama](https://ollama.com) running locally (unless you're only using a
  cloud model via `--base-url`, see below)
- The model pulled: `ollama pull gemma4:26b-nvfp4`
- Python 3.12+
- Node.js 22+ (for `glob_files` tool)
- `langchain-litellm`/`litellm` install by default via `requirements.txt`,
  but are only actually used when `--base-url`/`LITELLM_BASE_URL` is set

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

- `python3 cli.py --yolo-mode` — **EXTREMELY DANGEROUS**: never ask, run
  every tool call immediately — the AI can overwrite or delete any file it
  can reach with no confirmation. Only use it if you fully trust the
  prompts you're giving it.
- `python3 cli.py --auto-mode` — auto-approve writes/deletes inside the
  launch directory; still ask for anything outside it. Launching this way
  interactively first asks you to confirm you trust the folder (**Yes, I
  trust this folder** / **No, exit**) before doing anything else; declining
  exits immediately. (This one-time check doesn't apply to `-p ...
  --auto-mode`, which stays fully non-interactive.)

(`--yolo-mode` and `--auto-mode` are mutually exclusive.)

The bottom toolbar shows the loaded model and current permission mode
(`Model: <name>  |  Manual mode` by default). Press **Shift+Tab** at any
time in the REPL to toggle live between Manual and Auto mode without
restarting (no effect if launched with `--yolo-mode`). Press **Esc** while
typing at the input prompt to clear the line back to empty.

- `python3 cli.py -p "<prompt>"` (or `--prompt`) — run one turn
  non-interactively with `<prompt>` as input, print the response, and exit
  (skips the REPL). Combine with `--yolo-mode`/`--auto-mode` as needed —
  handy for quickly testing a permission mode without sitting in the REPL.
- `python3 cli.py --model <name>` — override the local Ollama model tag
  (default set in `core/agent.py`) without editing the file.
- `python3 cli.py --base-url <url> --model <name> [--api-key <key>]` — use
  a cloud/self-hosted model via [litellm](https://docs.litellm.ai) instead
  of local Ollama. `--model` is required with `--base-url` and needs
  litellm's provider prefix, e.g. `openai/gpt-4o-mini`. Each of the three
  can also be set via `LITELLM_BASE_URL`/`LITELLM_MODEL`/`LITELLM_API_KEY`
  env vars (flag takes precedence). See [`docs/models.md`](docs/models.md).

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
| `test_model_config.py` | Automated tests for model/cloud config resolution — no Ollama/network needed |
| `test_streaming.py` | Automated tests for chunk-content normalization and model-error handling in streaming — no Ollama/network needed |
| `test_tools.py` | Automated tests for `rg_search`'s output cap — no Ollama/network needed |
| `requirements.txt` | Python dependencies (`pip3 install -r requirements.txt`) |
| `bin/rg` | Bundled ripgrep binary (aarch64-apple-darwin) |
| `docs/` | Implementation details (tool internals, etc.) — see also [`docs/manual-testing.md`](docs/manual-testing.md) and [`docs/models.md`](docs/models.md) |

## Exit

Type `quit`, `exit`, `/exit`, or press `Ctrl+C` / `Ctrl+D`.

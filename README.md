# Zeppeli

Interactive terminal chat interface powered by a local [Ollama](https://ollama.com) model with tool-calling support.

## Features

- Multi-turn conversation — AI remembers context across turns
- Streaming output — responses print token by token
- Tool calling — AI can search/inspect files and edit (write/delete) files when relevant
- Image input — attach a local image via `@path`, `/image <path>`, or `--image` for vision-capable models (see [`docs/models.md`](docs/models.md#vision--image-input))
- Slash commands: `/exit` to quit, `/image <path>` to attach an image

## Requirements

- [Ollama](https://ollama.com) running locally (unless you're only using a
  cloud model via `--base-url`, see below)
- The model pulled: `ollama pull gemma4:26b-nvfp4`
- Python 3.12+
- Node.js 22+ (for `glob_files` tool)
- `langchain-litellm`/`litellm` install by default via `requirements.txt`,
  but are only actually used when `--base-url`/`LITELLM_BASE_URL` is set
- `Pillow` installs by default via `requirements.txt`, used to downscale
  attached images before sending — a small image still works without it,
  see [`docs/models.md`](docs/models.md#vision--image-input)

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
- `python3 cli.py --image <path>` (repeatable, max 4) — attach a local
  image to the turn. With `-p`, attaches to that one turn; without it,
  attaches to your first REPL message. Requires a vision-capable model
  (e.g. `--base-url http://host:8000/v1 --model hosted_vllm/qwen3.6-27b-awq-int4`).
  In the REPL you can also attach with `@path` inline or `/image <path>`.
  See [`docs/models.md`](docs/models.md#vision--image-input).

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

You> 這張圖有什麼問題 @shots/error.png
  [image: shots/error.png]
Zeppeli> 這是一個 Python TypeError...

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

## Session History

Every run — interactive REPL or one-shot `-p` — is automatically recorded
to `~/.zeppeli/sessions/session-<id>.json` (conversation history plus
per-turn run stats), no flag required. See
[`docs/sessions.md`](docs/sessions.md) for the storage format and lifecycle.

## Event Log

Every run also writes a second, more granular record: an append-only
JSONL event stream to `~/.zeppeli/logs/log-<session-id>.jsonl` (session
started, each turn started/completed, per-hop model activity, CLI
errors), no flag required. See [`docs/logging.md`](docs/logging.md) for
the event schema and lifecycle.

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
| `test_images.py` | Automated tests for image attachment (`@path`/`/image`/`--image`) — no Ollama/network needed |
| `test_sessions.py` | Automated tests for session-history persistence (`core/sessions.py`) — no Ollama/network needed |
| `test_eventlog.py` | Automated tests for the JSONL event log (`core/eventlog.py`) — no Ollama/network needed |
| `requirements.txt` | Python dependencies (`pip3 install -r requirements.txt`) |
| `bin/rg` | Bundled ripgrep binary (aarch64-apple-darwin) |
| `docs/` | Implementation details (tool internals, etc.) — see also [`docs/manual-testing.md`](docs/manual-testing.md), [`docs/models.md`](docs/models.md), [`docs/sessions.md`](docs/sessions.md), and [`docs/logging.md`](docs/logging.md) |

## Exit

Type `quit`, `exit`, `/exit`, or press `Ctrl+C` / `Ctrl+D`.

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
- ripgrep (for `rg_search` tool) — bundled, zero-setup, for macOS
  Apple Silicon and Linux x86-64 (`bin/rg-darwin-arm64`/
  `bin/rg-linux-x86_64`); any other platform/architecture needs `rg`
  installed and on `PATH` (e.g. `brew install ripgrep` / `apt install
  ripgrep`) — see [`docs/tools.md`](docs/tools.md#rg_searchpattern-path-glob-max_bytes50000)
- `langchain-litellm`/`litellm` install by default via `requirements.txt`,
  but are only actually used when `--base-url`/`LITELLM_BASE_URL` is set
- `Pillow` installs by default via `requirements.txt`, used to downscale
  attached images before sending — a small image still works without it,
  see [`docs/models.md`](docs/models.md#vision--image-input)
- `slack-bolt`/`aiohttp` install by default via `requirements.txt` but are
  only used by the optional Slack bot (`slack_bot.py`) — see
  [`docs/slack.md`](docs/slack.md)

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
- Alternatively, copy `config.json.example` to `config.json` (git-ignored)
  and fill in `model`/`base_url`/`api_key` there for a persistent local
  testing default, instead of retyping flags or exporting env vars every
  run. Precedence is flag > env var > `config.json` > built-in default.
  See [`docs/models.md`](docs/models.md).
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

## Slack Bot

`slack_bot.py` runs a second, independent entry point: a long-running
Socket Mode bot that lets people trigger the same AI agent from Slack
messages instead of a terminal, replying in-thread.

```bash
python3 slack_bot.py
```

Purely `config.json`-driven (`slack.bot_token`/`slack.app_token`/
`slack.allowed_dir`/`slack.allowed_users` — copy `config.json.example` to
get started), no CLI flags. Each Slack thread is its own independent
conversation; tool calls auto-approve inside `allowed_dir` and are
auto-refused outside it (no interactive approval over Slack). See
[`docs/slack.md`](docs/slack.md) for setup (creating the Slack app,
enabling Socket Mode, required scopes) and the security considerations
around who can trigger it.

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
| `tests/test_tool_call.py` | Batch test script for tool calling |
| `tests/test_permission_modes.py` | Automated tests for permission-mode logic — no Ollama needed |
| `tests/test_model_config.py` | Automated tests for model/cloud config resolution — no Ollama/network needed |
| `tests/test_streaming.py` | Automated tests for chunk-content normalization and model-error handling in streaming — no Ollama/network needed |
| `tests/test_tools.py` | Automated tests for `rg_search`'s output cap — no Ollama/network needed |
| `tests/test_read_file.py` | Automated tests for `read_file()`'s pagination and truncation behavior — no Ollama/network needed |
| `tests/test_truncation.py` | Automated tests for the generic tool-output line/char cap and its full-record preservation — no Ollama/network needed |
| `tests/test_compaction.py` | Automated tests for the two-tier conversation-history compaction sent to the model — no Ollama/network needed |
| `tests/test_images.py` | Automated tests for image attachment (`@path`/`/image`/`--image`) — no Ollama/network needed |
| `tests/test_sessions.py` | Automated tests for session-history persistence (`core/sessions.py`) — no Ollama/network needed |
| `tests/test_eventlog.py` | Automated tests for the JSONL event log (`core/eventlog.py`) — no Ollama/network needed |
| `requirements.txt` | Python dependencies (`pip3 install -r requirements.txt`) |
| `config.json.example` | Template for the optional git-ignored `config.json` (local `model`/`base_url`/`api_key` overrides, plus the optional `slack` block) — see [`docs/models.md`](docs/models.md) and [`docs/slack.md`](docs/slack.md) |
| `slack_bot.py` | Slack bot entry point (Socket Mode) — see [`docs/slack.md`](docs/slack.md) |
| `slack_bot/` | Slack integration layer — Bolt event wiring, per-thread sessions, live-region adapter |
| `tests/test_slack_bot.py` | Automated tests for Slack access control, throttled live-updates, and thread-session locking — no live Slack/network needed |
| `bin/rg-darwin-arm64` | Bundled ripgrep binary, macOS Apple Silicon |
| `bin/rg-linux-x86_64` | Bundled ripgrep binary, Linux x86-64 |
| `docs/` | Implementation details (tool internals, etc.) — see also [`docs/manual-testing.md`](docs/manual-testing.md), [`docs/models.md`](docs/models.md), [`docs/sessions.md`](docs/sessions.md), [`docs/logging.md`](docs/logging.md), and [`docs/slack.md`](docs/slack.md) |

## Exit

Type `quit`, `exit`, `/exit`, or press `Ctrl+C` / `Ctrl+D`.

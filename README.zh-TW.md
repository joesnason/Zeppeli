# Zeppeli

[English](README.md) | 繁體中文

由本機 [Ollama](https://ollama.com) 模型驅動、支援工具呼叫（tool calling）的互動式終端機聊天介面。

## 功能

- 多輪對話 — AI 會在多輪之間記住上下文
- 串流輸出 — 回應逐 token 印出
- 工具呼叫 — AI 可在需要時搜尋／檢視檔案，以及編輯（寫入／刪除）檔案
- 圖片輸入 — 對支援視覺的模型，可透過 `@path`、`/image <path>` 或 `--image` 附加本機圖片（見 [`docs/models.zh-TW.md`](docs/models.zh-TW.md#vision--image-input)）
- Skills — 把特定任務的說明放在 `~/.zeppeli/skills/<name>/SKILL.md` 或 `./.zeppeli/skills/<name>/SKILL.md`；模型會在需要時透過 `skill://<name>` 讀取（見 [`docs/skills.zh-TW.md`](docs/skills.zh-TW.md)）
- 斜線指令：`/exit` 離開、`/image <path>` 附加圖片

## 需求

- 本機執行中的 [Ollama](https://ollama.com)（除非你只透過 `--base-url`
  使用雲端模型，見下文）
- 已拉取模型：`ollama pull gemma4:26b-nvfp4`
- Python 3.12+
- Node.js 22+（`glob_files` 工具需要）
- ripgrep（`rg_search` 工具需要）— 已為 macOS Apple Silicon 與 Linux
  x86-64 內建、免設定（`bin/rg-darwin-arm64`／`bin/rg-linux-x86_64`）；
  其他平台／架構需自行安裝 `rg` 並放在 `PATH` 上（例如
  `brew install ripgrep`／`apt install ripgrep`）— 見
  [`docs/tools.zh-TW.md`](docs/tools.zh-TW.md#rg_searchpattern-path-glob-max_bytes50000)
- `langchain-litellm`／`litellm` 預設會透過 `requirements.txt` 安裝，
  但只有在設定 `--base-url`／`LITELLM_BASE_URL` 時才會實際使用
- `Pillow` 預設會透過 `requirements.txt` 安裝，用來在送出前縮小附加的
  圖片 — 小圖片沒有它也能用，見
  [`docs/models.zh-TW.md`](docs/models.zh-TW.md#vision--image-input)
- `slack-bolt`／`aiohttp` 預設會透過 `requirements.txt` 安裝，但只有選用的
  Slack bot（`slack_bot.py`）會用到 — 見
  [`docs/slack.zh-TW.md`](docs/slack.zh-TW.md)

## 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r requirements.txt
```

若你使用 [direnv](https://direnv.net/)，`.envrc` 會自動啟用 venv。

## 使用方式

```bash
python3 cli.py
```

預設情況下，每次寫入／刪除都會要求確認（細節見
[`docs/tools.zh-TW.md`](docs/tools.zh-TW.md)）。有兩個旗標可以放寬這項限制：

- `python3 cli.py --yolo-mode` — **極度危險**：永不詢問，所有工具呼叫
  立即執行 — AI 可以在未經確認的情況下覆寫或刪除任何它能觸及的檔案。
  只有在你完全信任自己給的提示時才使用。
- `python3 cli.py --auto-mode` — 自動核准啟動目錄內的寫入／刪除；
  目錄外的任何操作仍會詢問。以這種方式互動啟動時，會先要求你確認信任
  該資料夾（**Yes, I trust this folder**／**No, exit**），才會做其他事；
  拒絕則立即離開。（這個一次性檢查不適用於 `-p ... --auto-mode`，後者
  維持完全非互動。）

（`--yolo-mode` 與 `--auto-mode` 互斥。）

底部工具列會顯示已載入的模型與目前的權限模式（預設為
`Model: <name>  |  Manual mode`）。在 REPL 中隨時按 **Shift+Tab** 可即時
在 Manual 與 Auto 模式之間切換，無需重新啟動（若以 `--yolo-mode` 啟動則
無作用）。在輸入提示列打字時按 **Esc** 可清空整行。

- `python3 cli.py -p "<prompt>"`（或 `--prompt`）— 以 `<prompt>` 為輸入，
  非互動地執行一輪、印出回應後離開（略過 REPL）。可視需要搭配
  `--yolo-mode`／`--auto-mode` — 方便快速測試某個權限模式，而不必待在 REPL 裡。
- `python3 cli.py --model <name>` — 覆寫本機 Ollama 模型 tag
  （預設值設定於 `core/agent.py`），無需修改檔案。
- `python3 cli.py --base-url <url> --model <name> [--api-key <key>]` — 透過
  [litellm](https://docs.litellm.ai) 使用雲端／自架模型，取代本機 Ollama。
  使用 `--base-url` 時必須提供 `--model`，且需加上 litellm 的 provider
  前綴，例如 `openai/gpt-4o-mini`。三者也都可以透過
  `LITELLM_BASE_URL`／`LITELLM_MODEL`／`LITELLM_API_KEY` 環境變數設定
  （旗標優先）。見 [`docs/models.zh-TW.md`](docs/models.zh-TW.md)。
- 或者，將 `config.json.example` 複製為 `config.json`（已被 git 忽略），
  在其中填入 `model`／`base_url`／`api_key`，作為持久的本機測試預設值，
  不必每次重打旗標或匯出環境變數。優先順序為旗標 > 環境變數 >
  `config.json` > 內建預設值。見 [`docs/models.zh-TW.md`](docs/models.zh-TW.md)。
- `python3 cli.py --image <path>`（可重複，最多 4 張）— 為該輪附加本機
  圖片。搭配 `-p` 時附加到那一輪；不搭配時則附加到你在 REPL 的第一則
  訊息。需要支援視覺的模型（例如
  `--base-url http://host:8000/v1 --model hosted_vllm/qwen3.6-27b-awq-int4`）。
  在 REPL 中也可以用行內 `@path` 或 `/image <path>` 附加。
  見 [`docs/models.zh-TW.md`](docs/models.zh-TW.md#vision--image-input)。

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

`slack_bot.py` 是第二個獨立的進入點：一個長時間執行的 Socket Mode bot，
讓使用者可以從 Slack 訊息（而非終端機）觸發同一個 AI agent，並在討論串中回覆。

```bash
python3 slack_bot.py
```

完全由 `config.json` 驅動（`slack.bot_token`／`slack.app_token`／
`slack.allowed_dir`／`slack.allowed_users` — 複製 `config.json.example`
開始設定），沒有 CLI 旗標。每個 Slack 討論串都是各自獨立的對話；
`allowed_dir` 內的工具呼叫會自動核准，之外則自動拒絕（Slack 上沒有互動式
核准）。設定方式（建立 Slack app、啟用 Socket Mode、所需 scopes）以及
誰能觸發它的安全考量，見 [`docs/slack.zh-TW.md`](docs/slack.zh-TW.md)。

## 工具

AI 可使用的工具涵蓋搜尋／檢視檔案、編輯（寫入／刪除）檔案，以及執行
shell 指令；破壞性操作，以及在目前工作區之外（或使用 `sudo`）的 shell
指令，都需要互動確認。完整清單與實作細節見
[`docs/tools.zh-TW.md`](docs/tools.zh-TW.md)。

## Skills

session 開始時，會掃描 `~/.zeppeli/skills/<name>/` 與
`./.zeppeli/skills/<name>/` 下的每個 `SKILL.md`（名稱衝突時專案層優先），
並把每個 skill 的名稱與說明列在 system prompt 中。當請求符合時，模型會用
`read_file("skill://<name>")` 讀取完整說明，並用 `skill://<name>/<path>`
讀取附屬檔案。在 skill 的 frontmatter 中設定
`disable-model-invocation: true` 可隱藏它。見
[`docs/skills.zh-TW.md`](docs/skills.zh-TW.md)。

## 對話紀錄（Session History）

每次執行 — 互動 REPL 或單次 `-p` — 都會自動記錄到
`~/.zeppeli/sessions/session-<id>.json`（對話歷史加上每輪的執行統計），
不需任何旗標。儲存格式與生命週期見
[`docs/sessions.zh-TW.md`](docs/sessions.zh-TW.md)。

## 事件日誌（Event Log）

每次執行還會寫入第二份更細緻的紀錄：一個只追加（append-only）的 JSONL
事件串流，寫到 `~/.zeppeli/logs/log-<session-id>.jsonl`（session 開始、
每輪開始／完成、每個 hop 的模型活動、CLI 錯誤），不需任何旗標。事件
schema 與生命週期見 [`docs/logging.zh-TW.md`](docs/logging.zh-TW.md)。

## 檔案

| 檔案 | 用途 |
|------|---------|
| `cli.py` | 互動式 CLI 進入點 |
| `core/` | AI agent／模型層 — 工具定義、路徑解析、Ollama 載入 |
| `core/skills.py` | skill 掃描（`SKILL.md` frontmatter）與 `skill://` 解析 — 見 [`docs/skills.zh-TW.md`](docs/skills.zh-TW.md) |
| `ui/` | 使用者互動層 — REPL 迴圈、串流／Markdown 渲染、權限提示 |
| `tests/test_tool_call.py` | 工具呼叫的批次測試腳本 |
| `tests/test_permission_modes.py` | 權限模式邏輯的自動化測試 — 不需 Ollama |
| `tests/test_model_config.py` | 模型／雲端設定解析的自動化測試 — 不需 Ollama／網路 |
| `tests/test_streaming.py` | 串流中 chunk 內容正規化與模型錯誤處理的自動化測試 — 不需 Ollama／網路 |
| `tests/test_tools.py` | `rg_search` 輸出上限的自動化測試 — 不需 Ollama／網路 |
| `tests/test_read_file.py` | `read_file()` 分頁與截斷行為的自動化測試 — 不需 Ollama／網路 |
| `tests/test_tail_file.py` | `tail_file()` 有界反向讀取演算法與邊界情況的自動化測試 — 不需 Ollama／網路 |
| `tests/test_truncation.py` | 通用工具輸出行數／字元上限及其完整紀錄保留的自動化測試 — 不需 Ollama／網路 |
| `tests/test_compaction.py` | 送給模型的兩層對話歷史壓縮（compaction）的自動化測試 — 不需 Ollama／網路 |
| `tests/test_images.py` | 圖片附加（`@path`／`/image`／`--image`）的自動化測試 — 不需 Ollama／網路 |
| `tests/test_sessions.py` | 對話紀錄持久化（`core/sessions.py`）的自動化測試 — 不需 Ollama／網路 |
| `tests/test_skills.py` | skill 掃描、system prompt 區段與 `skill://` 讀取的自動化測試 — 不需 Ollama／網路 |
| `tests/test_eventlog.py` | JSONL 事件日誌（`core/eventlog.py`）的自動化測試 — 不需 Ollama／網路 |
| `requirements.txt` | Python 相依套件（`pip3 install -r requirements.txt`） |
| `config.json.example` | 選用、已被 git 忽略的 `config.json` 範本（本機 `model`／`base_url`／`api_key` 覆寫，以及選用的 `slack` 區塊）— 見 [`docs/models.zh-TW.md`](docs/models.zh-TW.md) 與 [`docs/slack.zh-TW.md`](docs/slack.zh-TW.md) |
| `slack_bot.py` | Slack bot 進入點（Socket Mode）— 見 [`docs/slack.zh-TW.md`](docs/slack.zh-TW.md) |
| `slack_bot/` | Slack 整合層 — Bolt 事件串接、每個討論串的 session、live-region 轉接器 |
| `tests/test_slack_bot.py` | Slack 存取控制、節流的即時更新、討論串 session 鎖定的自動化測試 — 不需實際 Slack／網路 |
| `bin/rg-darwin-arm64` | 內建 ripgrep 執行檔，macOS Apple Silicon |
| `bin/rg-linux-x86_64` | 內建 ripgrep 執行檔，Linux x86-64 |
| `docs/` | 實作細節（工具內部機制等）— 另見 [`docs/manual-testing.zh-TW.md`](docs/manual-testing.zh-TW.md)、[`docs/models.zh-TW.md`](docs/models.zh-TW.md)、[`docs/sessions.zh-TW.md`](docs/sessions.zh-TW.md)、[`docs/logging.zh-TW.md`](docs/logging.zh-TW.md)、[`docs/skills.zh-TW.md`](docs/skills.zh-TW.md) 與 [`docs/slack.zh-TW.md`](docs/slack.zh-TW.md)。每份文件的英文版為預設，繁體中文版以 `.zh-TW.md` 結尾 |

## 離開

輸入 `quit`、`exit`、`/exit`，或按 `Ctrl+C`／`Ctrl+D`。

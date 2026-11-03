# 對話紀錄持久化（Session history persistence）

[English](sessions.md) | 繁體中文

這個 CLI 的每次執行 — 互動 REPL 或單次 `-p` — 都會記錄到
`~/.zeppeli/sessions/` 下的一個 JSON 檔，永遠開啟，不需任何旗標。本文件
涵蓋儲存格式、寫入它的生命週期，以及當磁碟上的 schema 無法乾淨對應到本
程式碼既有概念時所做的幾個取捨判斷。

## 儲存位置

```
~/.zeppeli/sessions/session-<8charid>.json
```

`<8charid>` 是行程 session UUID 的**前 8 個字元**（`ui/repl.py` 的
`_session_state["id"] = str(uuid.uuid4())`）— 而非另外產生的 id。一個
行程、一個 session、一個身分；工具列的 `Session ID: <uuid>` 那一行仍然
顯示完整 UUID，而持久化檔案的 `id` 欄位只是同一個值較短的傳輸格式
（`core/sessions.py` 的 `short_id()`）。

目錄會在第一次寫入時延遲建立（`SESSIONS_DIR.mkdir(parents=True,
exist_ok=True)`）— 在 session 真正需要儲存之前，不會碰 `~/.zeppeli/`。

## Schema

`core/sessions.py` 的 dataclass 逐欄位對應這個 JSON 結構（camelCase，
與原始 schema 一致 — 沒有轉成 snake_case）：

```jsonc
{
  "version": 1,
  "id": "a1b2c3d4",
  "cwd": "/Users/x/workspace/ollama",
  "title": "ollama",
  "createdAt": "2026-08-18T09:00:00+00:00",
  "updatedAt": "2026-08-18T09:05:12+00:00",
  "provider": "ollama",
  "model": "gemma4:26b-nvfp4",
  "ollamaUrl": null,
  "lmStudioUrl": null,
  "history": [
    { "role": "user", "content": "list the files here", "timestamp": "..." },
    { "role": "assistant", "content": "", "timestamp": "...",
      "toolCall": { "tool": "list_files", "args": { "path": "." } } },
    { "role": "tool", "content": "total 12\n...", "timestamp": "...",
      "toolResult": { "ok": true, "output": "total 12\n..." } },
    { "role": "assistant", "content": "Here's what's in the directory...", "timestamp": "..." }
  ],
  "runs": [
    { "id": "abb9ed82-...", "startedAt": "...", "completedAt": "...",
      "status": "completed",
      "stats": { "durationMs": 1830, "turns": 2, "toolCalls": 1,
                 "inputTokens": 512, "outputTokens": 94 } }
  ]
}
```

## 生命週期

- **每個行程建立一次**，在 `ui/repl.py` 的 `main()` 中、
  `messages = [SystemMessage(...)]` 建好之後 — 在 `load_llm()` 之後，且
  （對互動式 `--auto-mode`）在信任閘門之後，因此拒絕信任永遠不會建立
  檔案。互動 REPL 與單次 `-p` 走完全相同的程式路徑。
- **建立時立即寫入**（0 個 run、0 筆 history）— 即使 session 在完成任何
  一輪之前就被中斷，仍會留下檔案。
- **每輪之後更新**，透過 `ui/repl.py` 的 `_run_and_persist()`，它包裝
  `run_turn()` 而不修改它：
  1. 追加一個 `status: "running"` 的 `RunEntry` 並儲存 — 一個在呼叫
     `run_turn()` *之前*就寫到磁碟的 stub。
  2. 照常呼叫 `run_turn()`。
  3. 比對呼叫前後的 `messages`，分類該 run（`finish_run_from_messages()`），
     並將新追加的 LangChain 訊息轉為 `history[]` 項目
     （`append_history_from_messages()`），然後再儲存一次。
- 每次寫入都是**原子的** — 背景寫入執行緒（見下文）先寫到
  `SESSIONS_DIR` 中的暫存檔，再以 `os.replace()` 覆蓋目標 — 因此寫入
  途中崩潰永遠不會在磁碟上留下截斷／損毀的 JSON 檔。
- **永不讓 CLI 崩潰。** `save_session()` 會吞掉所有例外（磁碟已滿、權限
  被拒、序列化錯誤），而寫入執行緒對每個項目的 `try/except` 意味著一次
  失敗的寫入不會阻止後續寫入被處理。對話紀錄持久化是錦上添花的附加功能；
  它的 bug 絕不能中斷已經顯示在畫面上的實際聊天輪次。

### 寫入佇列

`save_session()` 本身從不寫磁碟 — 它更新 `updatedAt`，將 session 序列化為
一般的 `dict`（`StoredSession.to_dict()`，自然會深拷貝所有內容，與
`session` 之後的原地修改脫鉤），然後把 `(path, dict)` 放入一個
`core/queued_writer.py` `QueuedWriter` — 一個與 `core/eventlog.py` 共用的
小型通用包裝，負責佇列／執行緒／flush 機制，讓兩個模組都不必各自重新
實作。單一背景 daemon 執行緒**依序、以 FIFO 順序**消費該佇列，並執行
實際的原子寫入（`core/sessions.py` 的 `_write_json_atomic()`，在匯入時
傳給 `QueuedWriter`）。這代表：

- `save_session()` 會立即返回 — 緩慢的磁碟 I/O（網路掛載的家目錄、忙碌
  的磁碟）永遠不會阻塞聊天／輪次流程，即使 `_run_and_persist()` 每輪會
  呼叫它兩次。
- 同一個 session 的寫入一定依呼叫順序套用 — 最近一次 `save_session()`
  呼叫永遠是最終留在磁碟上的那一份，絕不會被順序錯亂的較早寫入覆蓋。
- 單一失敗的寫入不會卡住執行緒 — 消費者對每個項目包的
  `try/except Exception: pass` 正是讓它在失敗後仍能存活、處理之後排入的
  所有項目的原因。

**`flush_pending_writes()`** 會阻塞，直到目前為止排入的每個寫入都真正
寫出（`queue.Queue.join()`；若從未排入任何東西則立即返回）。它從兩處
被呼叫：

- 明確地，在 `ui/repl.py` 單次 `-p` 分支的結尾、`main()` 返回之前 —
  `-p` 旨在具決定性／可腳本化（呼叫者可能在行程結束的瞬間讀取 session
  檔），因此不依賴 `atexit` 的時機。
- 透過 `atexit.register(flush_pending_writes)`，在模組匯入時註冊一次 —
  這會在直譯器正常關閉時清空佇列，而在 CPython 中，**當未處理的例外
  （包括未捕捉的 `KeyboardInterrupt`）傳到腳本頂層時也會觸發** —
  涵蓋了輪次進行中按 Ctrl+C 的情況，這在目前本來就會未被捕捉地傳出
  `run_turn()`／`main()`。已手動驗證：排入一個寫入，然後讓未捕捉的
  `KeyboardInterrupt` 傳到一個拋棄式腳本的頂層，該寫入仍會留在磁碟上。

**可接受的殘餘風險**：只有在寫入被排入與寫入執行緒實際處理它之間發生
強制終止（`SIGKILL`）或真正的直譯器崩潰，才會遺失那次寫入 — 這兩種情況
下 `atexit` 都沒有機會執行。這與完全同步的設計在終止恰好發生在
`write()` 途中時所面臨的風險屬於同一類；佇列並不會讓它明顯更糟，只是讓
時間窗稍微變寬（已排入但尚未寫出 vs. 寫入途中）。原子寫入的保證本身
（不會有*損毀*的檔案）兩種情況都不受影響 — 最糟的情況是檔案少了最近
一次更新，而絕不會是壞掉的檔案。

<a id="message--history-conversion"></a>
## 訊息 → history 轉換

LangChain 的 `messages` 清單（一般的 `HumanMessage`／`AIMessage`／
`ToolMessage` 物件，由 `run_turn()` 原地修改）對應到 `history[]` 的方式：

| LangChain 訊息 | `role` | 說明 |
|---|---|---|
| `SystemMessage` | *（略過）* | 不在 `{user, assistant, tool}` 列舉中 |
| `HumanMessage` | `"user"` | `content` 透過 `core/messages.py` 的 `extract_text()` 取得（同時處理純字串與附加圖片的 list 內容） |
| `AIMessage`，無 `tool_calls` | `"assistant"` | 一筆 |
| `AIMessage`，有 `tool_calls` | `"assistant"` × N | **每個呼叫一筆** — 單一 hop 可能帶多個工具呼叫（`ui/turn.py` 的 `for tc in response.tool_calls:`）；回應文字只附在第一筆，其餘為 `""`，讓每筆仍與其 `ToolMessage` 一對一配對 |
| `ToolMessage` | `"tool"` | `toolResult.ok` 是啟發式判斷：若輸出以 `"Error:"` 開頭（`core/tools.py` 中每個 `@tool` 用來表示失敗的慣例）或包含 `"CANCELLED"`（`ui/turn.py` 放在被拒權限結果中的字面字串）則為 `False`；否則為 `True`。`toolResult.output`／`content` 來自 `m.additional_kwargs["full_output"]`（若不存在則退回 `m.content`）— 即**完整、未截斷**的工具結果，即使 `ui/turn.py` 可能已透過 `core/messages.py` 的 `truncate_tool_output()` 送給模型一份修剪過的版本（見 [`docs/tools.zh-TW.md`](tools.zh-TW.md#tool-output-truncation)） |

這張表描述的是 `_run_and_persist()`（`ui/repl.py`）透過 `start_index`
切片的完整、未壓縮的 `messages` 清單 — 而那是這個轉換*唯一*會看到的
清單。`core/messages.py` 的 `compact_messages()`（見
[`docs/models.zh-TW.md`](models.zh-TW.md#turn-level-context-compaction-25-turn-sliding-window)）
會在每個 hop 重新為模型建立一個獨立、以輪次為視窗的 `messages` *視圖*，
完全在 `ui/streaming.py` 的 `stream_response()` 內部進行 — 它從不寫回
正式清單，也不從中衍生。因此 `append_history_from_messages()` 與
`core/eventlog.py` 的 `build_turns_and_outputs()` 永遠看得到每一輪的每一則
訊息，即使對話早已遠遠超過模型本身不再看到較舊中段輪次的 25 輪門檻。

## 在沒有既有先例下所做的判斷

本程式碼先前沒有「provider」、設定目錄或短 session id 的概念 — 以下是
明確決定的，而非猜測：

- **`provider` 永遠硬編碼為 `"ollama"`。** 本程式碼中任何地方都沒有真正的
  LM Studio 整合（零引用）— 唯一的其他後端是一般性的、經 litellm 路由的
  雲端／自架模型（`--base-url`），在語意上並不是 `"lmstudio"`。那樣標記
  反而會誤導之後讀取此檔的任何東西。
- **`ollamaUrl`／`lmStudioUrl` 永遠為 `null`。** 本程式碼沒有「自訂 Ollama
  URL」的概念（`ChatOllama` 使用自己的 client 預設值／環境變數，從未經由
  `load_llm()` 傳遞），而 litellm 的 `base_url` 在語意上也不屬於任一欄位 —
  特別是 `provider` 永遠是 `"ollama"`，所以 `lmStudioUrl` 根本不可能用到。
  用 `base_url` 填入任一欄位都會造成誤導。
- **`title` 預設為啟動目錄的 basename**（`Path(initial_cwd).name`，只有在
  `cwd == "/"`、basename 為空的邊界情況下才退回完整路徑字串）。
- **`"cancelled"` 是保留、目前未使用的 `RunEntry.status`。** `ui/repl.py`
  的 REPL 迴圈只在輸入提示周圍捕捉 `KeyboardInterrupt`／`EOFError`，而非
  `run_turn()` 周圍 — 輪次中按 Ctrl+C 現在本來就會未被捕捉地傳出
  `main()`，而此功能刻意不新增例外處理來改變這點。"running" stub 會在
  `run_turn()` 執行*之前*排入，而 `atexit` 即使在該未捕捉例外下也會觸發
  （見上方「寫入佇列」），因此被中斷的行程仍會留下誠實的紀錄：那個 run
  就停在 `status: "running"`、沒有 `completedAt`，而不需要事後製造一個
  `"cancelled"` 狀態。
- **每筆 `history[]` 的 `timestamp` 是持久化時間，而非模型產生時間** —
  訊息是在 `append_history_from_messages()` 轉換時加上時間戳記的，全部在
  `run_turn()` 返回後一次完成，而非每則產生時逐一加上。

## 測試

`tests/test_sessions.py` 涵蓋資料結構、訊息轉換規則（包括多工具呼叫拆分與
`ok` 啟發式判斷）、run 分類、原子寫入行為（包括 `save_session()` 會吞掉
寫入失敗）、寫入佇列本身（`save_session()` 不會因緩慢寫入而阻塞、排入的
寫入依 FIFO 順序套用、寫入執行緒在失敗寫入後存活並繼續處理後續寫入、
`flush_pending_writes()` 在空佇列上立即返回），以及一個整合測試，斷言
`ui.repl.main()` 每個行程恰好寫出一個完全最新的 session 檔（依賴單次分支
明確的 `flush_pending_writes()` 呼叫）— 全部都將
`core.sessions.SESSIONS_DIR` 重新導向到暫存目錄，不依賴 Ollama 或網路。
`tests/test_permission_modes.py` 中實際呼叫 `repl.main()` 的既有測試，也
基於同樣理由重新導向 `SESSIONS_DIR` — 見該檔的模組 docstring。

已於 2026-08-19 手動驗證：排入一個寫入，然後讓未捕捉的
`KeyboardInterrupt` 傳到拋棄式腳本的頂層（模擬輪次中按 Ctrl+C），該寫入
仍會透過 `atexit` 留在磁碟上；而一次實際 `-p` 執行的 session 檔，在行程
結束的瞬間就已完整填入已完成 run 的 `history`／`stats`。

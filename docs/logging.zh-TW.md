# 事件日誌（Event log）

[English](logging.md) | 繁體中文

這個 CLI 的每次執行 — 互動 REPL 或單次 `-p` — 還會寫入第二份更細緻的
紀錄：位於 `~/.zeppeli/logs/` 下、只追加（append-only）的 JSONL 事件串流，
永遠開啟，不需任何旗標。它與 [`docs/sessions.zh-TW.md`](sessions.zh-TW.md)
的對話紀錄摘要檔是平行的產物 — 它是生命週期事件發生當下的除錯／稽核軌跡，
而非可恢復的 session 摘要。本文件涵蓋儲存格式、寫入它的生命週期，以及
當某個事件的 schema 無法乾淨對應到本程式碼既有概念時所做的取捨判斷。

## 儲存位置

```
~/.zeppeli/logs/log-<session-id>.jsonl
```

`<session-id>` 是**完整的** session UUID（`ui/repl.py` 的
`_session_state["id"] = str(uuid.uuid4())`）— 與
[`docs/sessions.zh-TW.md`](sessions.zh-TW.md) 的 `session-<8charid>.json`
不同，這個檔名**不會**被截斷為 8 個字元。兩個檔案都源自同一個隨行程存續的
UUID，也都位於 `~/.zeppeli/` 下，但它們是各自獨立的磁碟產物，生命週期也
各自獨立（`core/eventlog.py` 對 `core/sessions.py`）。

目錄會在第一次寫入時延遲建立（`LOGS_DIR.mkdir(parents=True,
exist_ok=True)`），與 sessions 目錄相同。

## 事件類型

每一行是一個 JSON 物件：`{"type", "timestamp", "sessionId",
["runId"], "data": {...}}`。`timestamp` 為 ISO-8601 UTC。除了
`session_started` 與 `cli_error`（兩者不屬於單一 run）以外，每個事件都帶有
`runId`。

### `session_started`

每個行程記錄一次，時機在 auto-mode 信任閘門（若有）之後 — 因此拒絕信任
永遠不會寫出檔案 — 但在嘗試 `load_llm()` **之前**，因為錯誤的模型／雲端
設定本身就是一個真實的失敗點，而這個事件的兄弟事件 `cli_error` 必須能
回報它。

```jsonc
{
  "type": "session_started",
  "data": {
    "cwd": "/Users/x/workspace/ollama",
    "provider": "ollama",       // "ollama" 或 "litellm"，依是否提供 --base-url 決定
    "model": "gemma4:26b-nvfp4",
    "ollamaUrl": "http://127.0.0.1:11434",  // OLLAMA_HOST 環境變數或 client 預設值；provider 為 "litellm" 時為 null
    "platform": "darwin",
    "pid": 12345,
    "reasoningMode": "enabled",  // 本機 Ollama 為 "enabled"，雲端／litellm 為 "unavailable"
    "recoveredInterruptedRuns": 0  // 永遠為 0 — 見下方「已知限制」
  }
}
```

### `run_started`

每輪記錄一次，時機在 `RunEntry` stub 建立之後（對應
[`docs/sessions.zh-TW.md`](sessions.zh-TW.md) 的 "running" stub 時機）。

```jsonc
{
  "type": "run_started",
  "runId": "9f8e7d6c-...",
  "data": { "promptLength": 28, "promptPreview": "the first 200 chars of the prompt" }
}
```

### `model_activity`

每個 `stream_response()` hop 記錄一次（`ui/turn.py` 的工具呼叫迴圈每次
迭代呼叫一次）— **不是**每個串流 token 一次。`index` 是該 hop 在 run 中
從 0 起算的位置；`finalization` 為 `true` 若且唯若該 hop 的回應沒有
`tool_calls`（亦即它是該 run 的最終答案）。

```jsonc
{
  "type": "model_activity",
  "runId": "9f8e7d6c-...",
  "data": {
    "index": 0,
    "finalization": false,
    "chunk": { "thinking": "The user wants X, so I should call tool Y first...", "content": "", "done": true }
  }
}
```

`chunk.thinking` 讀取自合併後 `AIMessage` 的
`additional_kwargs["reasoning_content"]`。對本機 Ollama，`ui/repl.py` 的
`main()` 會要求 `ui/streaming.py` 的 `stream_response()` 在 `ChatOllama`
的 `.stream()` 呼叫中傳入 `reasoning=True`，這才是實際填入此欄位的原因 —
何時會是空的，見下方「已知限制」。

### `run_completed`

每輪記錄一次，時機在 run 被分類（`finish_run_from_messages()`）之後。
透過 `completionStatus` **同時**涵蓋成功與失敗 — 沒有獨立的失敗事件類型。

```jsonc
{
  "type": "run_completed",
  "runId": "9f8e7d6c-...",
  "data": {
    "completionStatus": "completed",  // 或 "failed"
    "answer": "the final answer text",  // 失敗時為 ""
    "answerLength": 28,
    "stats": { "durationMs": 8000, "turns": 2, "toolCalls": 1,
               "inputTokens": 512, "outputTokens": 94 },  // 失敗且無統計時為 null
    "turns": [
      { "kind": "tool", "content": "...", "toolCall": { "tool": "read_file", "args": {...} },
        "toolResult": { "ok": true, "output": "..." } },
      { "kind": "final", "content": "the final answer text" }
    ],
    "modelOutputs": [
      { "index": 0, "finalization": false, "contentChars": 0, "thinkingChars": 118, "done": true },
      { "index": 1, "finalization": true, "contentChars": 28, "thinkingChars": 64, "done": true }
    ]
    // "error": "..." — 只在 completionStatus 為 "failed" 時出現
  }
}
```

`turns`／`modelOutputs` 由 `core/eventlog.py` 的 `build_turns_and_outputs()`
建立，來源是 [`docs/sessions.zh-TW.md`](sessions.zh-TW.md) 的
`finish_run_from_messages()` 與 `append_history_from_messages()` 已經在
使用的同一個 `new_messages` 清單 — `turns` 對應
`append_history_from_messages()` 的「每個工具呼叫一筆」拆分方式
（`toolResult.ok` 使用相同的 `Error:`／`CANCELLED` 啟發式判斷，現已共用為
`core/messages.py::tool_result_ok()`），而 `modelOutputs` 則是每個
`AIMessage` hop 一筆摘要。與 `docs/sessions.zh-TW.md` 的 `history[]` 一樣，
`toolResult.output` 讀取自對應 `ToolMessage` 的
`additional_kwargs["full_output"]`（退而求其次才用 `.content`）— 也就是
完整、未截斷的工具結果，不論 `core/messages.py` 的 `truncate_tool_output()`
對模型實際看到的內容套用了什麼上限（見
[`docs/tools.zh-TW.md`](tools.zh-TW.md#tool-output-truncation)）。

### `cli_error`

當例外從 `ui/repl.py` 的 `main()` 逃出時記錄（從 `load_llm()` 開始 — 見
「已知限制」）。記錄後一律重新拋出，因此行程的崩潰／結束碼行為不變；
這只是多加一筆紀錄。

```jsonc
{
  "type": "cli_error",
  "data": { "name": "RuntimeError", "message": "..." }  // message 上限約 2000 字元
}
```

## 生命週期

設計與 [`docs/sessions.zh-TW.md`](sessions.zh-TW.md) 非常接近 — 相同的
背景執行緒加佇列的非阻塞模型、相同的「永不讓聊天崩潰」吞掉所有例外的
理念 — 但它是追加 JSONL 行，而不是重寫整份 JSON 文件：

- 一個專用的背景 daemon 執行緒 — 與 `core/sessions.py` 使用的同一個
  `core/queued_writer.py` `QueuedWriter` 包裝，以 `core/eventlog.py` 自己的
  `_append_jsonl()` 建構 — 依序以 FIFO 順序消費佇列，每個項目追加一行
  （`open(path, "a").write(json.dumps(line) + "\n")`）。由於只有單一
  執行緒在寫入，沒有交錯寫入的風險需要防範 — 不需要整檔重寫所需的
  原子替換機制。
- 每個 `log_*()` 函式都只是放入佇列後立即返回 — 緩慢的磁碟 I/O 永遠不會
  阻塞一輪對話 — 並且自行吞掉所有例外，與 `save_session()` 相同。
- **`flush_pending_events()`** 會阻塞直到佇列清空。在 `ui/repl.py` 的單次
  `-p` 分支結尾明確呼叫（與 `flush_pending_writes()` 一起），並透過
  `atexit.register(flush_pending_events)` 處理互動 REPL 的最終離開 —
  包括未捕捉的例外／`KeyboardInterrupt` 傳到腳本頂層的情況，與
  `flush_pending_writes()` 相同。
- **可接受的殘餘風險**：與 [`docs/sessions.zh-TW.md`](sessions.zh-TW.md)
  的說明相同 — 只有在放入佇列與實際寫入之間發生強制終止或直譯器崩潰，
  才會遺失該事件。

## 已知限制

- **`chunk.thinking` 只在本機 Ollama 時才有內容。** `ui/repl.py` 的
  `main()` 在啟動時計算一次 `reasoning_enabled = base_url is None`，並一路
  傳遞 `_run_and_persist()` → `run_turn()` → `stream_response()`，後者在
  設定時會對 `ChatOllama` 的 `.stream()` 呼叫傳入 `reasoning=True`。已於
  2026-08-21 針對 `gemma4:26b-nvfp4` 手動驗證：這確實會把模型的推理分離到
  `additional_kwargs["reasoning_content"]`，而不改變可見的回應，且與
  工具呼叫搭配也正常（以工具呼叫結束的 hop 仍會得到真實的 `thinking`
  文字，例如解釋它*為什麼*要呼叫那個工具）。若載入的模型實際上不支援
  推理模式，第一次嘗試的例外會被視為可能與推理相關：`stream_response()`
  會不帶推理重試一次，並在行程剩餘期間記住不再嘗試
  （`ui/streaming.py` 的 `_reasoning_unsupported`）— 因此真正不支援的
  模型只會在第一個 hop 多付出一次失敗嘗試，而非每輪的每個 hop。透過
  `--base-url` 使用的雲端／自架模型沒有對應機制，因此它們的
  `chunk.thinking` 永遠是空的。
- **`session_started.recoveredInterruptedRuns` 永遠為 `0`。** 本程式碼
  沒有 session 恢復的概念 — 每個行程都會開始全新的 session id／日誌檔，
  所以永遠沒有東西需要「恢復」。
- **沒有 `maxTurns` 欄位。** `ui/turn.py` 的工具呼叫迴圈確實沒有上限 —
  這個 CLI 中不存在此類設定，因此省略該欄位，而非捏造一個。
- **`cli_error` 只涵蓋 session 存在之後的例外** — 即 `ui/repl.py` 的
  `main()` 中從 `load_llm()` 開始。`cli.py` 中 session 之前的 `argparse`
  驗證錯誤（錯誤的 `--model`／`--base-url` 組合、錯誤的 `--image` 路徑）
  在任何 session id 產生之前就已透過 `parser.error()` 乾淨地離開，不會
  記錄在這裡。

## 測試

`tests/test_eventlog.py` 涵蓋每種事件類型的 JSONL 結構（包括
`run_completed` 的 completed／failed 變體、`cli_error` 的訊息長度上限，以及
`session_started.reasoningMode` 的 `"enabled"`／`"unavailable"` 值）、
`build_turns_and_outputs()`（單一 hop 中的單一與多個工具呼叫、最終無工具
呼叫的答案、來自 `additional_kwargs` 的 thinking 字元數）、寫入佇列本身
（非阻塞、FIFO 順序、寫入執行緒在一次失敗寫入後仍存活、空佇列上的
`flush_pending_events()`）、`ui/streaming.py` 的 `stream_response()` 只在
`session_id`／`run_id`／`turn_index` 皆提供時才發出 `model_activity`，以及
一個整合測試，斷言 `load_llm()` 被強制拋出的例外在傳出
`ui.repl.main()` 之前會留下一行 `cli_error` — 全部都將
`core.eventlog.LOGS_DIR` 重新導向到暫存目錄，不依賴 Ollama 或網路。
`tests/test_sessions.py` 與 `tests/test_permission_modes.py` 中實際呼叫
`repl.main()` 的既有測試，也基於同樣理由重新導向 `LOGS_DIR`。

`tests/test_streaming.py` 另外涵蓋 `reasoning=True` 機制本身：要求時會傳入、
預設時省略；第一次嘗試拋出例外時的每行程一次後備機制
（`_reasoning_unsupported`）；後備機制會在後續呼叫中被記住（不會重複
失敗重試）；以及推理嘗試與後備都失敗時仍會回報錯誤並如以往回傳
`None` — 全部使用假的 LLM，不依賴 Ollama。

已於 2026-08-21 手動驗證：針對本機 Ollama 的一次實際 `-p` 執行會產生
`session_started` → `run_started` → 每個 hop 一個 `model_activity` →
`run_completed`，且 `turns`／`modelOutputs`／`stats` 與實際發生的情況
相符（一次 `list_files` 工具呼叫，接著是最終答案）。第二次接上
`reasoning=True` 的實際執行（一個小數比較問題，`9.11` 對 `9.9`）確認了
`session_started.reasoningMode: "enabled"`，以及 `chunk.thinking` 中填有
真正逐步的推理內容，而可見的聊天回應仍保持乾淨。

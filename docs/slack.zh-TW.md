# Slack bot 整合

[English](slack.md) | 繁體中文

`slack_bot.py`（repo 根目錄）是與 `cli.py` 並列的第二個獨立進入點 — 一個
長時間執行的 Socket Mode 行程，讓使用者可以從 Slack 訊息（而非終端機）
觸發同一個 AI agent（相同工具、相同的 `core/` 模型邏輯）。`slack_bot/`
與 `core/`／`ui/` 的關係，見專案 `CLAUDE.md` 的「Architecture」。

## 建立 Slack app 並啟用 Socket Mode

1. 前往 [api.slack.com/apps](https://api.slack.com/apps) → **Create New
   App** → **From scratch**。選擇名稱與 workspace。
2. **Socket Mode**（左側欄）→ 開啟。這會產生 **App-Level Token**
   （`xapp-...`）— 保存起來，填入 `config.json` 的 `slack.app_token`。它所需
   的 scope 見下方「所需權限」。
3. **OAuth & Permissions**（左側欄）→ 在 **Bot Token Scopes** 區段加入下方
   「所需權限」列出的每個 scope。
4. **Event Subscriptions**（左側欄）→ 開啟 → 在 **Subscribe to bot
   events** 加入下方「所需權限」列出的每個事件。
5. **Install App**（OAuth & Permissions 頁面頂端，或側欄的 Install App
   頁面）→ 安裝到你的 workspace。這會產生 **Bot User Token**
   （`xoxb-...`）— 保存起來，填入 `config.json` 的 `slack.bot_token`。
6. 邀請 bot 到你的測試頻道：`/invite @YourBotName`。

## 所需權限

bot 需要的一切都集中在這裡 — 每當有東西不正常時請對照這份清單，因為缺少
scope／事件通常會**無聲**失敗（事件或 API 呼叫根本不會發生；
`slack_bot.py` 自己的輸出中不會出現任何錯誤）。

**App-Level Token scope**（Socket Mode 頁面 — Socket Mode 本身是用這個
token 驗證，而非 Bot Token）：

| Scope | 原因 |
|---|---|
| `connections:write` | Socket Mode WebSocket 連線本身所需。 |

**Bot Token Scopes**（OAuth & Permissions 頁面）：

| Scope | 原因 |
|---|---|
| `app_mentions:read` | 接收 `@mention` 事件。 |
| `chat:write` | 發送與編輯 bot 的回覆。 |
| `channels:history` | 讀取 bot 所在**公開**頻道中的訊息／回覆 — 沒有它，即使已訂閱下方的 `message.channels` 事件，討論串回覆也永遠到不了 bot。 |
| `groups:history` | 同上，用於**私人**頻道 — 只有要在那裡使用 bot 時才需要。 |
| `files:read` | 下載使用者附加在訊息上的檔案（見下方「檔案附件」）。 |

**Event Subscriptions**（Event Subscriptions 頁面 → **Subscribe to bot
events**）：

| 事件 | 原因 |
|---|---|
| `app_mention` | 傳遞 `@mention` 事件 — 開始／延續一個討論串。 |
| `message.channels` | 傳遞**公開**頻道中的訊息（包括討論串回覆）— 「在同一個討論串回覆」要能運作的必要條件。 |
| `message.groups` | 同上，用於**私人**頻道 — 只有要在那裡使用 bot 時才需要。 |

**⚠️ 變更任何 scope 或事件訂閱後，必須將 app 重新安裝到 workspace**
（Install App 頁面 → reinstall）。否則 Slack 不會把變更套用到執行中的
安裝 — bot token 會維持與之前完全相同的行為，直到你重新安裝為止。這是
「我加了權限但還是不行」最常見的單一原因：scope／事件已存在於 app 的
*設定*中，但你 workspace 中*已安裝*的 app 尚未同步更新。之後也請重新啟動
`python3 slack_bot.py`，以便乾淨地重新連線。

## `config.json` schema

將 `config.json.example` 複製為 `config.json`（已被 git 忽略），並填入
`"slack"` 區塊：

```jsonc
{
  "model": "gemma4:e2b", "base_url": "", "api_key": "",
  "slack": {
    "app_token": "xapp-...",
    "bot_token": "xoxb-...",
    "allowed_dir": "/path/to/a/disposable/sandbox/directory",
    "allowed_users": ["U0123ABCXYZ"]
  }
}
```

- `app_token`／`bot_token` — 依序來自上方步驟 2／5 — 設定過程中 Slack 會在
  Bot Token *之前*先給你 App-Level Token（先開 Socket Mode，再 Install
  App），因此這個欄位順序與你實際取得它們的順序一致。兩者皆為必要；
  缺少任一者時 `slack_bot.py` 會以清楚的訊息結束。**手動填寫時很容易搞混**
  — 兩者看起來都是不透明的字串 — 因此若 `app_token` 不是以 `xapp-` 開頭，
  或 `bot_token` 不是以 `xoxb-` 開頭，`cli.py` 會在啟動時發出警告（非致命），
  因為對調的話，否則只會在之後以令人困惑的 Slack API 錯誤浮現
  （`apps.connections.open` 上的 `not_allowed_token_type`，或
  `auth.test()` 回傳 `None` 的 bot user id — 已實際踩過一次）。
- `allowed_dir` — auto-mode 的範圍邊界（見下方「權限模型」）。必要。
- `allowed_users` — 選用。見下方「存取控制」。

`model`／`base_url`／`api_key` 與 `cli.py` 已讀取的頂層鍵相同（見
[`docs/models.zh-TW.md`](models.zh-TW.md)）— 一份模型設定服務所有 Slack
討論串，在啟動時載入一次。

## 存取控制

`allowed_users` 是 Slack user ID 的清單。**非空**：只有這些使用者的訊息會
觸發 bot；其他人一律被無聲忽略（沒有回覆，他們也看不到任何錯誤）。
**空或省略**：任何能在 bot 所在頻道發文的人都能觸發它。

測試時先只放你自己的 ID，之後再移除清單（或清空）把 bot 開放給整個
頻道 — 這是預期的工作流程，而不是單向門。

查詢 Slack user ID：在 Slack 中點某人的個人檔案 → **More** →
**Copy member ID**（形如 `U0123ABCXYZ`）。

## 討論串 = session 模型

每個 Slack **討論串**都是一段獨立的對話：

- 在 bot 尚未擁有的討論串之外 `@mention` bot，會開始一個**新**討論串
  （Slack 會把該 mention 自身的 `ts` 變成新討論串的 `thread_ts`），以及一段
  沒有先前上下文的全新對話。
- 在 bot 已回覆過的討論串中回覆，會延續同一段對話。
- 既沒有 mention bot、也不是在其討論串中回覆的一般頻道訊息，會被完全
  忽略 — bot 永遠不會回應一般的頻道閒聊。

對話上下文只在 `slack_bot.py` 行程存續期間存在記憶體中 — 重新啟動會清除
每個討論串的上下文。不過 `core/sessions.py`／`core/eventlog.py` 對發生事件
的紀錄，仍會和終端機 REPL 完全一樣寫入 `~/.zeppeli/sessions/`／
`~/.zeppeli/logs/` — 遺失的只有記憶體中的即時對話，而非歷史紀錄。

## 回覆語言

無論使用者的訊息以何種語言撰寫，bot 一律以繁體中文回覆。這是 Slack 專屬
行為：`slack_bot/sessions.py` 的 `ThreadRegistry.get_or_create()` 在為新
討論串建立初始 `SystemMessage` 時，會在 `core/agent.py` 共用的
`SYSTEM_PROMPT` 後面附加 `_SLACK_LANGUAGE_INSTRUCTION` 字串 — 終端機 REPL／
`-p` 模式使用未修改的 `SYSTEM_PROMPT`，維持語言中立。目前無法依
workspace／討論串設定；若需要不同的措辭或語言，請直接修改
`_SLACK_LANGUAGE_INSTRUCTION`。

## 回覆格式

Slack 不會渲染 CommonMark Markdown — 它有自己較受限的標記語法
（"mrkdwn"）：用 `*bold*` 而非 `**bold**`、`_italic_` 而非 `*italic*`、
`~strike~` 而非 `~~strike~~`、`<url|label>` 連結而非 `[label](url)`，沒有
`#` 樣式的標題，也沒有表格。這條管線中沒有任何東西會在模型文字到達 Slack
之前轉換它 — `slack_bot/live.py` 的 `SlackLive.update_markdown()`／
`finalize_markdown()` 會把它完全未經處理地送到 `chat.postMessage`／
`chat.update` — 因此 bot 是以與上方「回覆語言」相同的方式被指示的，透過
討論串 `SystemMessage` 的第二個 Slack 專屬附加內容：`slack_bot/sessions.py`
的 `_SLACK_FORMATTING_INSTRUCTION`。終端機 REPL／`-p` 模式不受影響 — 它仍透過
Rich 的 `Markdown` 類別（`ui/live_region.py`）將模型原始文字渲染為
CommonMark，那是完全獨立的程式路徑。

這是基於提示的作法，而非硬性保證：管線中任何地方都沒有程式層級的
Markdown 轉 mrkdwn 轉換器，因此是否符合取決於模型是否真的遵循指示。若實務上
發現它不夠可靠地遵循，下一步會是在 `SlackLive` 發送前加入真正的轉換步驟 —
尚未實作。若要不同措辭，請直接修改 `_SLACK_FORMATTING_INSTRUCTION`。

## 檔案附件

在訊息上附加檔案（例如日誌檔）可讓 bot 連同你的提示一起分析它。若 Slack
回報檔案為 `text/*` mimetype，**或**檔名帶有常見的文字副檔名（`.log`、
`.txt`、`.csv`、`.json`、`.md`、`.yml`／`.yaml`、`.ini`／`.conf`／
`.cfg`、`.env`、`.xml`），即受支援 — 之所以有副檔名檢查，是因為實務上
Slack 常把 `.log` 檔的 mimetype 回報為通用的 `application/octet-stream`，
而非 `text/plain`，只檢查 mimetype 的話會錯誤地拒絕它。圖片、壓縮檔及其他
二進位檔會被拒絕，並在討論串中留下一行說明，而不是無聲地丟棄。

bot 不會把整個檔案塞進提示，而是將它**儲存**到
`<allowed_dir>/slack_attachments/`（命名為 `<slack-file-id>_<filename>`
以避免衝突），並提供模型該檔的路徑、總行數，以及**最後約 200 行**的預覽 —
日誌最近的幾行通常才是相關的部分。刻意不使用點開頭（隱藏）的目錄：
`rg_search`／`glob_files` 預設會略過隱藏檔案／目錄（ripgrep 本身的預設，
以及 Node 的 `fs.promises.glob()`），這會讓儲存的附件即使就在工作區中，
也無聲地無法被搜尋／glob 找到。若模型需要較早的內容，它會在該儲存路徑上
使用既有的 `read_file`／`rg_search` 工具翻閱其餘部分 — 沒有特殊的
「附件」工具；這與磁碟上任何其他檔案走的是完全相同的讀檔路徑。

一則訊息最多可帶 3 個附件；超出的部分會被忽略。超過 50 MB 或下載失敗的
檔案，會和不支援的類型一樣，在討論串中得到同樣的一行拒絕說明。

## 權限模型 — 僅 auto-mode，無互動式核准

bot 一律以等同 `--auto-mode` 的方式執行，範圍限定在 `allowed_dir`：

- `allowed_dir` **內**的 `write_file`／`delete_file` 呼叫會以**零**確認
  自動核准 — 與終端機 `--auto-mode` 相同。
- `allowed_dir` **外**的呼叫一律**自動拒絕**，並在討論串中發布一行說明。
  沒有基於 Slack 的互動式核准（沒有按鈕／表情回應流程）— 這是刻意的範圍
  決定，而非缺少的功能；與終端機 `--auto-mode` 不同，這裡越界的寫入永遠
  不會退回成提示（在 Slack 上無法顯示 Yes/No 選單），因此直接拒絕而不是
  卡住。

**⚠️ 安全考量**：由於範圍內的寫入／刪除不需任何確認，`allowed_users` 中的
任何人 — 或在該清單留空時，任何能在 bot 所在頻道發文的人 — 都能指使在
`allowed_dir` 內任何位置進行真實的檔案寫入／刪除。建議：
- 將 `allowed_dir` 指向可拋棄／沙箱化的目錄，**絕不要**指向家目錄或含有
  機密的 repo。
- 除了單人測試外，保持 `allowed_users` 非空。

## 執行

```bash
python3 slack_bot.py
```

沒有 CLI 旗標 — 完全由 `config.json` 驅動。持續執行直到被中斷（Ctrl+C）。
成功啟動時會印出一行「connected」訊息，包含解析出的 bot user ID 與
`allowed_dir`。

<a id="socket-mode-reconnect-behavior"></a>
## Socket Mode 重新連線行為

`slack_bolt`／`slack_sdk` 會自行無限次重試斷線的 Socket Mode 連線，沒有
退避（backoff），也沒有任何形式的放棄機制（已直接閱讀安裝的
`slack_sdk`／`slack_bolt` 原始碼確認 — 沒有官方的 `max_retries`／放棄參數，
也沒有 `on_connect` callback 可得知某次嘗試是否真的成功）。`slack_bot.py`
在其上加了一個小型的外部監督器，位於 `slack_bot/reconnect.py`：

- **重試間隔**：`RECONNECT_RETRY_INTERVAL = 5` 秒 — 作為
  `AsyncSocketModeHandler` 的 `ping_interval` 傳入，這是函式庫唯一公開、
  乾淨且有文件的旋鈕（它同時也是一般 keepalive ping 的頻率，因此也讓函式庫
  自身的連線過期檢查 — `ping_interval × 4` — 在 20 秒觸發，而非函式庫以
  10 秒預設值推得的 40 秒）。
- **放棄門檻**：`RECONNECT_MAX_FAILURES = 10` 次連續重新連線失敗。由
  `ReconnectFailureCounter` 計數（一個比對兩則已知 slack_sdk 失敗日誌訊息的
  `logging.Handler` — 這是函式庫唯一公開的失敗訊號，因此本質上與
  slack_sdk 目前的日誌措辭有些耦合），並由 `watch_for_reconnect()` 在觀察到
  真正新的 `SocketModeClient.current_session` 時歸零 — 那是函式庫內部
  「這次嘗試成功了」的訊號（只在真正成功連線後才會重新指派），只是從未
  以 callback 形式公開。因此計數器只會因真正的失敗而增加、只會因真正的
  成功而歸零 — 不是盲目的終生計數，也不是基於時間的猜測。
- **放棄時**：關閉 Socket Mode client，`slack_bot.py` 的 `_run()` 拋出例外，
  以清楚的 `"Lost connection to Slack and failed to reconnect after 10
  attempts (5s apart) — giving up."` 錯誤與非零結束碼結束整個行程。沒有
  自動的行程層級重啟 — 若需要，交給外部監督器（systemd、launchd、Docker
  的 restart policy，或手動再執行一次）。
- 最壞情況下放棄所需時間約為 ~20 秒的過期偵測延遲加上 10 × 5 秒 ≈ 70 秒，
  而非固定的 50 秒，因為失敗計時要等函式庫本身察覺連線過期後才開始。

## 已知限制（v1）

- **記憶體中的討論串註冊表沒有上限** — 沒有淘汰／TTL。長時間執行的行程會
  為曾使用過的每個討論串累積一個 `ThreadSession`，只要行程持續執行就一直
  保留。目前的緩解方式是定期重啟（或未來的 TTL 清理）。
- **不支援圖片** — 只處理 `text/*` 附件（見上方「檔案附件」）；Slack 圖片
  上傳尚未接入 `core/images.py` 的 `@path`／`--image` 視覺管線。
- **`slack_attachments/` 從不清理** — 每個下載的附件都會無限期累積在磁碟上，
  與上方討論串註冊表同樣是「v1 不淘汰」的精神。若磁碟用量成為問題，請定期
  手動清除。（已從隱藏的 `.slack_attachments/` 改名 — 見上方「檔案附件」；
  升級前的行程已儲存在舊隱藏路徑下的檔案會保留原處，不會搬移。）
- **沒有互動式核准** — 這是刻意設計（見上方「權限模型」），不是 bug。

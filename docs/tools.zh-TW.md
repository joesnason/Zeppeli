# 工具（Tools）

[English](tools.md) | 繁體中文

Zeppeli 透過 `llm.bind_tools([...])` 向模型公開一組固定的、以 LangChain
`@tool` 裝飾的函式，在 `core/agent.py` 的 `load_llm()` 中接上。本文件說明
每個工具的簽章、行為與實作細節。所有工具程式碼都位於 `core/tools.py` — 這份
程式碼所在的 `core/`／`ui/` 分層，見 `CLAUDE.md` 的「Architecture」章節。

所有工具都定義於 `core/tools.py`：

```python
TOOLS = [list_files, glob_files, rg_search, read_file, tail_file, run_bash, write_file, delete_file]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
SLACK_TOOLS = [t for t in TOOLS if t.name != "run_bash"]
```

`TOOLS` 供給 `load_llm()` 的 `bind_tools()` 呼叫；`TOOLS_BY_NAME` 由
`ui/turn.py` 的 `run_turn()`（以及 `tests/test_tool_call.py` 的 `run_agent()`）
在模型要求呼叫時，用名稱查找並呼叫工具。`SLACK_TOOLS` 是 `slack_bot.py` 用來
取代 `TOOLS` 綁定的清單 — `load_llm()` 正是為此接受選用的 `tools` 參數 —
因為 Slack bot 一律無人值守執行，`run_bash` 的權限提示沒有人能回答
（`SlackLive.ask_menu()` 一律拒絕；見 `docs/slack.zh-TW.md`）。

## 路徑解析

`PATH_ARGS` 中列出的每個工具參數，都會在工具執行前透過 `resolve_paths()`
解析（兩者都在 `core/tools.py`）：

```python
PATH_ARGS = {
    "list_files": ["path"],
    "glob_files": ["cwd"],
    "rg_search": ["path"],
    "read_file": ["path"],
    "tail_file": ["path"],
    "run_bash": ["cwd"],
    "write_file": ["path"],
    "delete_file": ["path"],
}
```

解析規則，依序套用：

1. `~/…` 展開為使用者的家目錄（`pathlib.Path.expanduser()`）。
2. 若結果仍是相對路徑，則與 `initial_cwd` 結合 — 即 CLI 啟動時所在的目錄
   （在 `ui/repl.py` 的 `main()` 中擷取一次），而非模型當下的工作目錄或任何
   每輪狀態。
3. 絕對路徑原樣通過。

這代表模型可以傳入：絕對路徑、`~/foo`，或像 `foo/bar.py` 這樣單純的相對
路徑，不論工具呼叫當下 shell 的 cwd 為何，它都會一致地解析。

<a id="images-are-not-a-tool"></a>
## 圖片不是工具

附加圖片（`@path` mention、`/image <path>` 指令或 `--image` CLI 旗標 — 傳輸
格式見 [`docs/models.zh-TW.md`](models.zh-TW.md#vision--image-input)）從不經過
`TOOLS`／`bind_tools()`。它是訊息*內容*，由 `core/images.py` 的
`build_message_content()` 建立，並直接附加到 `ui/turn.py` 的 `run_turn()` 中
的 `HumanMessage` — 模型從不需要發出工具呼叫來查看附加的圖片，也沒有
`view_image` 工具。

`core/images.py` 的 `resolve_image_path()` 刻意逐條規則地模仿本檔的
`resolve_paths()`（expanduser → 若為相對路徑則結合 `initial_cwd` → 絕對路徑
原樣通過），因此 `@shots/error.png` 與 `read_file({"path": "shots/error.png"})`
工具呼叫會解析到完全相同的絕對路徑。這個等價性也是為什麼 `SYSTEM_PROMPT`
（`core/agent.py`）明確警告模型**不要**對它看到被附加的圖片路徑呼叫
`read_file` — `read_file` 以 `encoding="utf-8", errors="replace"` 開啟（見
下文），會把二進位圖片位元組變成亂碼，而不是拋出乾淨的錯誤。

## Pre-tool hook（權限提示）

某些工具被視為具破壞性，會在執行前經過一個確認 hook。這屬於 `ui/` 的範疇 —
`permission_ask()` 與 hook 註冊表都位於 `ui/permissions.py`：

```python
PRE_TOOL_HOOKS: dict[str, callable] = {
    "write_file": permission_ask,
    "delete_file": permission_ask,
    "run_bash": permission_ask,
}
```

`PRE_TOOL_HOOKS` 的**鍵**是「哪些工具需要把關」的註冊表 — 它的值已不再能
直接呼叫：`permission_ask()` 是 `async` 的，且需要一個只在呼叫時才可取得的
`live` 參數（`ui/live_region.py` 的 `LiveRegion`，或單次 `-p` 模式中的
`SimpleLive`），因此 `build_pre_tool_hooks()` 每次呼叫都會綁定一個新的、
綁有 `live` 的 closure，而非逐字複製 `PRE_TOOL_HOOKS` 的值。這裡 `run_bash`
的項目與其他兩個一樣只是殘留 — 它實際的把關邏輯是一個專用 hook
（`_make_bash_hook`），而非 `permission_ask` 本身，因為它的政策與
`write_file`／`delete_file` 不同（見下方「`run_bash` 的 hook」）。

`run_turn()`（`ui/turn.py`）不會直接讀取 `PRE_TOOL_HOOKS` — 它每輪呼叫一次
`build_pre_tool_hooks(mode, initial_cwd, live)`（就在
`reset_turn_approvals()` 之後），並對每個工具呼叫檢查回傳的 dict。若某個
工具有 hook，會以 `await hook(tool_name, resolved_args, console)` 的方式等待
它，且必須回傳 `True` 該工具才會真正執行（工具呼叫本身透過
`asyncio.to_thread()` 卸載，讓它永遠不會阻塞持久 Application 的事件迴圈 —
見 `ui/CLAUDE.md`）。若回傳 `False`（或使用者取消），該工具會被略過，模型
收到的工具結果是一則明確的
`"[<tool_name>] CANCELLED: the user denied permission..."` 訊息，而非真正的
輸出 — 措辭刻意強烈（並由 `SYSTEM_PROMPT`，`core/agent.py` 中的一條規則加以
強化），讓模型準確回報取消，而不是幻想成功 — 使用較溫和的訊息時曾觀察到它
這樣做。

<a id="permission-modes"></a>
### 權限模式

`build_pre_tool_hooks(mode, initial_cwd, live) -> dict[str, callable]` 是
唯一的分派點，依啟動時選擇的模式（`cli.py` 的 `--yolo-mode`／`--auto-mode`
旗標，預設都不選）決定套用哪些 hook（若有的話）。三個模式常數位於
`ui/permissions.py`：

| 模式 | 常數 | 行為 |
|------|----------|----------|
| approval（預設） | `MODE_APPROVAL` | 為 `write_file`／`delete_file` 綁定一個新的、會呼叫 `permission_ask` 的 closure — 每次呼叫都透過 `permission_ask()` 提示，與上述完全相同。`run_bash` 不屬於這個 closure — 見下文。 |
| yolo | `MODE_YOLO` | 回傳 `{}` — 完全沒有 hook。`hooks.get(tc["name"])` 永遠是 `None`，因此每個工具呼叫（包括 `run_bash`）都不受保護地執行，沒有任何提示。 |
| auto | `MODE_AUTO` | 將 `write_file`／`delete_file` 對應到由 `_make_auto_hook(initial_cwd, live)` 建立的 hook。`run_bash` 仍使用自己的 hook（見下文），而非這個。 |

`_make_auto_hook(initial_cwd, live)` 回傳一個 `async` hook，檢查
`_is_within_cwd(path, initial_cwd)`（兩邊都以 `Path.resolve()` 解析，因此
`..` 片段與 symlink 都能正確處理）：若解析後的路徑在 `initial_cwd` 內，就以
一行暗色說明自動核准、不提示；否則委派給真正的 `permission_ask()`，因此
啟動目錄之外的呼叫仍會得到完整的互動提示（包括輪次／session 記住核准）。

#### `run_bash` 的 hook

`run_bash` 不符合上述寫入／刪除的模式（approval 模式一律詢問；只有 auto
模式檢查範圍）— 它的政策*在 approval 與 auto 模式中相同*（只有 yolo 模式會
略過它），因為它與其說是寫入／刪除意義上的「破壞性」，不如說是「不論模式
都需要範圍／sudo 檢查」。`build_pre_tool_hooks()` 給它專屬的 closure，
`_make_bash_hook(initial_cwd, live)`：

- 當以下**全部**成立時，不提示直接自動執行：解析後的 `cwd` 參數在
  `initial_cwd` 內、`command` 字串中找到的類路徑 token 都沒有解析到
  `initial_cwd` 之外，且指令沒有呼叫 `sudo`。
- 否則顯示與 `permission_ask()` 相同的三選項選單（**Yes**／**Yes, always
  allow (this session)**／**No**），列出觸發的每個原因（工作目錄在工作區外、
  引用的路徑在工作區外，及／或 `sudo`）。
- `sudo` 偵測（`_contains_sudo()`）是一個簡單的 `\bsudo\b` 正規表示式 —
  不是 shell 解析。
- 指令字串本身的工作區逃逸檢查（`_command_escapes_cwd()`）是**啟發式**的，
  不是完整的 shell 解析：它以 `shlex.split()` 把 `command` 切成 token，把
  任何包含 `/` 或以 `~` 開頭的 token 視為路徑候選，以與 `resolve_paths()`
  相同的方式解析每一個，並標出第一個落在 `initial_cwd` 之外的。`/dev/`、
  `/tmp/`、`/var/tmp/` 下的絕對路徑被豁免（常見的重新導向／暫存目標，例如
  `> /dev/null`，不構成有意義的工作區逃逸）。無法解析的引號（`shlex` 的
  `ValueError`）被視為「無法驗證」而一律提示，而非無聲地略過檢查。這無法
  抓到所有混淆手法（例如由 shell 變數組成的路徑）— 與本專案明確的「沒有
  沙箱，只是簡單的範圍確認檢查」設計一致，並非安全邊界。
- 核准以確切的 `command` 字串為鍵（透過 `_key()` 的 `path`-或-`command`
  後備 — `run_bash` 呼叫沒有 `path` 參數），因此「這個 session 一律允許」
  記住的是一個特定指令，而非一個模式。
- Slack bot 完全無法使用 — 見上方 `SLACK_TOOLS`。

`python3 cli.py -p "<prompt>"`（單次模式，見 README）走的是與一般 REPL 輪次
完全相同的 `run_turn(..., mode)` 呼叫 — 單次呼叫沒有任何依模式的特殊處理。
透過 `-p` 實際操作三種模式的檢查清單，見
[`docs/manual-testing.zh-TW.md`](manual-testing.zh-TW.md)。

`permission_ask()` 會先檢查這個確切的呼叫是否已被核准（見下方「核准紀錄」）；
若是，就以一行暗色說明直接 `return True`，而不提示。否則它顯示與以往相同的
互動選單 — 三個選項、相同行為 — 但*機制*隨著持久工具列的渲染重寫而改變：
它現在是工具列所在的同一個持久 `Application` 上的 Layout 內 modal 疊層
（`live.ask_menu()`，`ui/live_region.py`），而非第二個巢狀的 `Application`
（舊作法為何無法與持久工具列共存，見 `ui/CLAUDE.md` 的「Pre-Tool Hooks」
章節）：

- 印出待執行的動作（`write to` 或 `delete`）與目標路徑。
- 方向鍵在 **Yes**／**Yes, always allow (this session)**／**No** 之間移動
  `▶` 選取游標（預設：**Yes**，即 `state["idx"] = 0`）。
- `Enter` 確認反白的選項。
- `Esc` 把游標移到 **No** 並確認 — 效果與用方向鍵往下移到 **No** 再按
  `Enter` 相同，因此最後一個畫面會在選單關閉前明顯顯示選取落在 **No** 上。
- `Ctrl-C` 離開整個 TUI（渲染重寫後全 app 一致的單一含義 — 見 `ui/CLAUDE.md`
  的「UI」章節），而不只是拒絕該提示。
- 選擇 **Yes** 或 **Yes, always allow (this session)** 會在回傳 `True` 之前
  記錄一筆核准（分別為輪次範圍或 session 範圍）。選擇 **No** 回傳 `False`，
  不記錄任何東西。

### 核准紀錄

`ui/permissions.py` 以兩個記憶體中的 set 追蹤核准，以
**`(tool_name, path)`** 為鍵 — 對於沒有 `path` 參數的 `run_bash` 則是
`(tool_name, command)` — 因此核准一個檔案的寫入（或一個確切的指令），不會
核准另一個檔案的寫入（或另一個指令）：

```python
_session_approved: set[tuple[str, str]] = set()
_turn_approved: set[tuple[str, str]] = set()
```

- **輪次範圍** — 由 `reset_turn_approvals()` 清除，`run_turn()`
  （`ui/turn.py`）在每輪開始時呼叫一次，因此它永遠不會延續到下一次使用者輸入
- **session 範圍** — 從不清除；只在行程存續期間存在，不寫入磁碟

拒絕從不被記錄，因此被拒絕的呼叫下次出現時會再次提示。

這個 hook 機制是通用的 — 新的破壞性工具只要在 `PRE_TOOL_HOOKS` 中加入一個
項目就能加入，不需修改 `run_turn()`。該項目會透過 `build_pre_tool_hooks()`
自動參與全部三種權限模式 — 不需要另外依模式註冊。

<a id="tool-output-truncation"></a>
## 工具輸出截斷

與上方的 pre-tool hook 一樣，這是在 `ui/turn.py` 的 `run_turn()` 中套用的
橫切機制 — 而非每個工具各自的上限。它一致地作用於**每一個**工具結果（全部
八個工具）以及 CANCELLED 權限拒絕訊息，就在每個結果變成送回模型的
`ToolMessage` 內容之前：

```python
full_output = str(result)
messages.append(ToolMessage(
    content=truncate_tool_output(full_output),
    tool_call_id=tc["id"],
    additional_kwargs={"full_output": full_output},
))
```

`truncate_tool_output()`（`core/messages.py`）套用兩條獨立的規則，兩條都會
檢查，也都可能套用：

1. **行數規則**：若文字超過 40 行，保留前 20 行與後 20 行，中間放一個
   `[truncated N lines]` 標記（`N` = 被省略的行數）。
2. **字元規則**：針對步驟 1 的結果檢查 — 若仍超過 2400 字元，保留前 1200 與
   後 1200 字元，中間放一個 `[truncated N chars]` 標記（`N` = 被省略的
   字元數）。

兩條規則是獨立的，而非互斥：35 行但總共 5000 字元（許多短行）的文字，即使
行數規則從未觸發，仍會被字元截斷；而觸發行數規則的文字，若保留的 40 行仍
夠長，之後仍可能觸發字元規則 — 在那種情況下，字元截斷可能落在（並移除）
行數標記本身上，因為它是對行數規則產出的結果做粗暴的切割。

這是疊加在下述每個工具自身上限之上的通用後盾，而非取代它們 — `rg_search`
自己的 `max_bytes` 上限、`read_file` 的分頁，以及 `tail_file` 自己以
`max_bytes` 為界的視窗都仍會先套用，只有在它們的輸出仍然很大時，這個上限
才會進一步修剪。它也是 `list_files`、`glob_files`、`write_file` 與
`delete_file` 的*唯一*上限，這些工具沒有自己的上限。

截斷只影響模型看到的內容。未截斷的原始內容保存在
`ToolMessage.additional_kwargs["full_output"]` 中，`core/sessions.py` 的
`append_history_from_messages()` 與 `core/eventlog.py` 的
`build_turns_and_outputs()` 都會優先讀取它而非 `.content`（對於任何沒有
`full_output` 的 `ToolMessage`，例如 `tests/test_tool_call.py` 自行直接建構的，
則退回 `.content`）— 因此對話紀錄（`~/.zeppeli/sessions/`）與事件日誌
（`~/.zeppeli/logs/`）永遠記錄完整、未截斷的工具輸出，即使模型本身只看到
修剪過的版本。

## 搜尋工具

### `list_files(path=".")`

透過呼叫 `ls -la` 列出 `path` 下的檔案與目錄。

```python
subprocess.run(["ls", "-la", path], capture_output=True, text=True)
```

成功時回傳 stdout，若 `ls` 以非零結束則回傳 `"Error: <stderr>"`。

### `glob_files(pattern, cwd=".")`

尋找符合 glob `pattern` 的檔案，支援以 `**` 遞迴比對。實作方式是呼叫
Node.js 並使用 `node:fs/promises` 的 `glob()` 非同步迭代器：

```js
const { glob } = require('node:fs/promises');
(async () => {
  const results = [];
  for await (const f of glob(pattern, { cwd })) results.push(f);
  console.log(results.join('\n') || '(no matches)');
})().catch(e => { process.stderr.write(e.message + '\n'); process.exit(1); });
```

pattern 與 cwd 在傳給 `node -e` 之前，會以 JSON 編碼（`json.dumps`）嵌入
產生的腳本中。需要 `PATH` 上有 Node.js 22+（這是唯一依賴 Node.js 的工具 —
其他全部都是純 Python 或內建執行檔）。

### `rg_search(pattern, path=".", glob="", max_bytes=50000)`

以 ripgrep 搜尋檔案內容。要呼叫哪個 `rg` 執行檔（`RG_BIN`）由
`core/tools.py` 的 `_find_rg_bin()` 在匯入時解析一次：

1. `PATH` 上系統安裝的 `rg`（`shutil.which("rg")`），若存在 — 不論平台
   都優先，因此較新／系統版本永遠優先於內建版本。
2. 否則，使用符合當前 `(platform.system(), platform.machine())` 的內建
   執行檔 — `bin/rg-darwin-arm64`（macOS Apple Silicon）或
   `bin/rg-linux-x86_64`（Linux x86-64），兩者皆為 ripgrep v15.1.0，因此
   各平台的行為／輸出格式完全相同。在這兩種平台／架構組合上都不需任何設定。
3. 否則 `RG_BIN` 為 `None` — `rg_search()` 會回傳友善的
   `"Error: ripgrep ('rg') isn't available for this platform. Install
   it — ..."` 訊息（附上各作業系統正確的安裝指令），而不嘗試執行任何東西。

```python
cmd = [RG_BIN, "--no-heading", "--color=never", pattern, path]
if glob:
    cmd += ["--glob", glob]
```

`pattern` 支援 ripgrep 的正規表示式語法。`glob` 依檔名篩選（例如 `*.py`）。
乾淨的空結果時回傳 `"(no matches)"`，若 ripgrep 以結束碼 2 離開（它代表
真正錯誤的慣例，相對於代表「沒有符合」的結束碼 1），則回傳 `"Error: ..."`。

`subprocess.run(cmd, ...)` 本身包在 `try/except OSError` 中 — 一個存在但
實際上無法啟動的執行檔（錯誤平台的可執行格式、損毀的檔案、權限問題）會在
那裡拋出例外，而不是產生 `returncode`，以前會一路未被捕捉地傳上去讓整個
行程崩潰（實際發生過：一台 Linux 機器執行舊的、只有 macOS 版的單一
`bin/rg`，遇到 `OSError: [Errno 8] Exec format error`）。現在它會被捕捉，
並以一般的 `"Error: couldn't run ripgrep at <path>: ..."` 工具結果字串回傳。

輸出上限為 `max_bytes`（預設 50 000，以 UTF-8 編碼後的位元組長度計算）：
對大型檔案（例如有許多 `FAILED`／`error:` 行的建置日誌）使用寬泛的 pattern，
否則可能在一次呼叫中回傳數萬個 token，這 — 特別是對小 context 的雲端模型 —
可能超出模型的 context window，並在 litellm 深處拋出未處理的
`ContextWindowExceededError`。若原始輸出超過 `max_bytes`，會在該位元組偏移
處切斷，並附加一行 `[Output truncated at <n> bytes — narrow the pattern or
glob to see fewer, more targeted matches.]` 說明，告訴模型縮小下一次呼叫的
範圍，而不是無聲地遺失符合項。與 `read_file` 不同，這裡沒有分頁／offset
機制 — `rg_search` 目前不支援從截斷點之後繼續；縮小 `pattern`／`glob` 是看到
其餘部分的唯一方法。`ui/streaming.py` 的 `stream_response()` 也會捕捉並回報
（而非因此崩潰）任何仍然漏過來的模型呼叫例外，包括這一個 — 見其模組
docstring。

### `read_file(path, offset=0, limit=400, max_lines=10000, max_bytes=98304)`

以有界的區塊讀取檔案，讓大型檔案無法在一次呼叫中撐爆模型的 context window。

- `offset` 從 0 起算，計算以迴圈 `f.readline()` 已略過的行數（不是基於
  seek，因此每次呼叫是 O(offset)，而非 O(1)）。
- `limit` 被限制在每次呼叫最多 400 行的硬上限（`limit = min(limit, 400)`）。
- 讀取迴圈會提早停止 — 在達到 `limit` 之前 — 若會超過 `max_lines`（預設
  10 000 行）或 `max_bytes`（預設 98 304 = 96 KiB，以每行 UTF-8 編碼後的
  位元組長度累加計算）。
- 檔案以 `encoding="utf-8", errors="replace"` 開啟，因此非 UTF-8 位元組不會
  拋出例外；它們會被替換，而不會讓工具崩潰。

回傳格式為標頭、原始行內容與頁尾：

```
[File: <path> | lines <start>–<end> | <bytes> bytes]
<...file content...>
[More available: use offset=<end> to continue]
```

頁尾有三種變體之一，告訴模型如何分頁：

- `[Stopped: max_lines limit reached — use offset=<n> to continue]` /
  `[Stopped: max_bytes limit reached — use offset=<n> to continue]`
  當硬上限截斷了讀取時 — 對下一個 offset 的說明與下方「more available」
  頁尾一樣明確（因硬上限停止時仍有更多可讀；模型不應該只能從「at line
  <n>」去推斷這點 — 以前這裡較不明確，曾導致一個小型本機模型無法可靠地
  讀過這個點）。
- `[More available: use offset=<n> to continue]` 當檔案還有更多行，但沒有
  觸及上限（亦即先達到 `limit`）時。
- `[End of file]` 當已經沒有東西可讀時。

單獨一行就超過 `max_bytes` 的情況（例如建置日誌中一條巨大的 classpath／
指令行）會被**截斷但仍然包含** — 附上 `[line truncated, N more bytes]`
標記 — 而不是回傳空內容。它也仍算作一行已消耗的行，因此下一個 `offset`
一定會前進越過它。若沒有這點，比 `max_bytes` 大的行會讓 `lines` 為空、
`end_line` 等於該次呼叫自己的 `offset`，於是之後在同一 offset 的每次呼叫都
會永遠重複相同的 `max_bytes` 停止，無法讀過去（在一次真實的建置日誌分析
正好遇到這點後修正）。

`path` 也可以是 `skill://<name>`／`skill://<name>/<相對路徑>` URI：
`resolve_paths()` 不會處理這類值，`read_file()` 會在一般的分頁讀取之前，
（透過 `core/skills.py` 的 `resolve_skill_uri()`）把它們對應到該 skill
資料夾中的真實檔案。此時標頭為 `[File: skill://<name> | skill dir: <abs
dir> | lines ...]`。只有 `read_file` 支援 `skill://` — 見
[`docs/skills.zh-TW.md`](skills.zh-TW.md)。

錯誤：`FileNotFoundError` 與超過檔案結尾的 `offset` 都會回傳
`[read_file] Error: ...` 字串，而非拋出例外，因為工具結果必須是模型可讀的
字串。

### `tail_file(path, lines=100, max_bytes=98304)`

讀取檔案的最後 `lines` 行 — 例如日誌中最新的項目 — 而不把整個檔案載入
記憶體，因此即使是數 GB 的檔案也能保持快速且記憶體有界。這是與 `read_file`
分開的工具，而不是它的一種模式：`read_file` 的 `offset`／`limit` 是向前、
可續讀的分頁契約，而 tail 讀取是真正不同的演算法（從結尾 seek，向後有界
讀取）— 把兩者合併成一個函式，會迫使 `offset`／頁尾語意依模式旗標而有兩種
不同含義。

演算法：seek 到 `max(0, filesize - max_bytes)`，向前讀到 EOF（最多
`max_bytes` 位元組，以二進位模式開啟以讓 seek 精確到位元組），以
`errors="replace"` 解碼（與 `read_file` 的作法一致），然後：

```python
filesize = p.stat().st_size
seek_pos = max(0, filesize - max_bytes)
with open(path, "rb") as f:
    f.seek(seek_pos)
    raw = f.read()
```

- 若 `seek_pos > 0`（視窗不是從檔案真正的開頭開始），該視窗的第一行很可能
  是不完整的 — 被 seek 從行中間切斷 — 因此會被丟棄，就像真正的 `tail` 在
  向後 seek 後丟棄不完整的開頭行一樣。這是無條件的，即使在罕見的恰好落在
  行邊界的 seek 上也一樣：一個已知、可接受的少一行偏差，比冒著留下損毀片段
  的風險安全。
- 若視窗中完全沒有換行（`max_bytes` 小於檔案實際的最後一行），整個視窗就是
  那一個不完整的行 — 它會被整個丟棄，而不是保留為截斷的片段（不同於
  `read_file` 對超大單行的處理）。這裡沒有可能像 `read_file` 舊 bug 那樣卡在
  迴圈中的 offset／分頁狀態，因此解法就是「用更大的 `max_bytes` 再呼叫一次」，
  頁尾會明確這麼說。
- 保留剩餘內容的最後 `lines` 行。

回傳格式仿照 `read_file` 的標頭／頁尾樣式：

```
[File: <path> | last <N> lines | <bytes> bytes]
<...file content...>
[Showing last <N> lines]
```

頁尾有三種變體之一：

- `[Showing last N lines]` — 找到了所要求的完整行數。
- `[Beginning of file reached — file has only M lines]` — 視窗到達了檔案
  真正的開頭（`seek_pos == 0`），且檔案總共確實少於 `lines` 行；不是截斷
  造成的假象。
- `[Only found M of requested N lines within the last max_bytes bytes of
  the file — increase max_bytes to search further back]` — `max_bytes`
  視窗（在丟棄不完整首行之後）不足 `lines` 行，但在它之前還有更多檔案內容。

錯誤：檔案不存在、路徑是目錄，或任何其他例外，都會回傳
`[tail_file] Error: ...` 字串而非拋出例外，與 `read_file`／`delete_file`
的風格一致。

## Shell 執行工具

### `run_bash(command, cwd=".", timeout=120)`

透過 `bash -c`（或 `direnv exec ... bash -c` — 見下方「direnv／`.envrc`」）
執行任意 shell 指令：

```python
cmd = [DIRENV_BIN, "exec", cwd, "bash", "-c", command] if DIRENV_BIN else ["bash", "-c", command]
result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
```

- `cwd`（預設 `"."`，與其他所有 `PATH_ARGS` 項目一樣解析）是指令執行的
  工作目錄。
- `timeout`（預設 120 秒）若時間內未完成，會終止行程並回傳
  `"Error: command timed out after <n>s"`。
- `subprocess.run(...)` 包在 `try/except OSError` 中（與 `rg_search` 相同的
  慣例）— 啟動失敗會回傳 `"Error: couldn't run bash: ..."`，而不會讓行程崩潰。
- 回傳合併的 `stdout`+`stderr`（去除前後空白，上限 50 000 位元組 — 與
  `rg_search` 相同的位元組上限風格），若兩者皆為空則回傳 `"(no output)"`。
  若結束碼非零，會加上前綴 `(exit code <n>)\n<output>`，讓模型不必從輸出中
  找線索就能看出指令失敗。

這**不是沙箱** — 工具本身對指令能做什麼沒有任何限制。唯一的把關是上方
「Pre-tool hook」所述的 pre-tool hook：工作目錄或引用路徑落在目前工作區
之外、或呼叫 `sudo` 的指令，會先提示使用者核准；在範圍內、非 `sudo` 的指令
則立即執行，與其他工具相同。在 Slack bot 中不會綁定給模型（`SLACK_TOOLS`
排除了它）— Slack bot 一律無人值守執行，且 `SlackLive.ask_menu()` 一律拒絕，
因此綁定到 Slack 的 `run_bash` 一旦需要提示，就永遠無法真正執行。

#### direnv／`.envrc`

`DIRENV_BIN = shutil.which("direnv")` 在匯入時解析一次，與 `RG_BIN` 完全
相同。只要它有設定，`run_bash` 就會包裝指令：

```python
cmd = [DIRENV_BIN, "exec", cwd, "bash", "-c", command] if DIRENV_BIN else ["bash", "-c", command]
```

三個刻意的限制，全部都對應 direnv 自己的實際行為，而不是重新實作或覆寫它：

- **不自動安裝。** 若 `PATH` 上沒有 `direnv`，`run_bash` 使用一般的
  `bash -c` — 它不存在就代表使用者根本沒有使用這個工作流程。
- **不繞過 `direnv allow`。** `.envrc` 是否真的被載入，完全由 direnv 自己
  決定。
- **不自行尋找 `.envrc`。** `direnv exec` 已經實作了真正的向上目錄查找與
  allow／deny 檢查 — 在這裡重新實作，會有悄悄偏離 direnv 實際規則的風險。

**被封鎖的 `.envrc` 後備** — 已針對真正的 `direnv`（v2.37.1）驗證，而非
假設：當 `.envrc` 存在但尚未 `direnv allow` 時，`direnv exec` **不會**只是
略過載入它然後照樣執行指令 — 它會拒絕執行任何東西，以非零結束並在 stderr
印出 `direnv: error <path>/.envrc is blocked. Run 'direnv allow' to approve
its content`。由於指令無論如何都必須執行（使用者的需求是「不要載入環境」，
而不是「不要執行指令」），`run_bash` 會偵測這則特定訊息（非零結束時 stderr
中同時出現 `"is blocked"` 與 `"direnv allow"`），並以一般的
`["bash", "-c", command]` 重試 — 刻意不使用 direnv，讓環境確實維持未載入，
與 direnv 自己拒絕做的事一致：

```python
if DIRENV_BIN and result.returncode != 0 and "is blocked" in result.stderr and "direnv allow" in result.stderr:
    result, error = _run_subprocess(["bash", "-c", command], cwd, timeout)
```

在一般（已允許或沒有 `.envrc`）的情況下，`direnv exec` 自己的 stderr（例如
它的「loading ~/.../.envrc」說明）會流經與其他指令輸出相同的合併
stdout+stderr 擷取，與 `run_bash` 執行的其他任何東西一樣，對模型／使用者
可見。

## 檔案編輯工具

`write_file`／`delete_file` 由上述的 `permission_ask` pre-tool hook 把關 —
使用者必須互動核准每一次寫入或刪除。

### `write_file(path, content)`

建立新檔案，或取代既有檔案的全部內容。

```python
p = pathlib.Path(path)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(content, encoding="utf-8")
```

中間目錄會自動建立。沒有部分編輯或追加模式 — 每次呼叫都會取代完整的檔案
內容。回傳 `"Wrote <n> bytes to <path>"` 或 `"Error: ..."`。

### `delete_file(path)`

刪除單一檔案。作為安全防護，明確拒絕刪除目錄，因為通用的遞迴刪除工具若
公開給模型會危險得多：

```python
if not p.exists():
    return f"[delete_file] Error: file not found: {path}"
if p.is_dir():
    return f"[delete_file] Error: {path} is a directory, not a file"
p.unlink()
```

## 新增工具

1. 在 `core/tools.py` 中以 `@tool` 定義它，並給它清楚的 docstring — 模型看到
   的工具描述就是這個 docstring。
2. 把它加到 `core/tools.py` 的 `TOOLS` 清單 — 光這樣就會更新
   `TOOLS_BY_NAME` 與 `load_llm()` 的 `bind_tools()` 呼叫，因此 `cli.py`、
   `ui/turn.py` 與 `tests/test_tool_call.py` 都會自動採用它。
3. 在 `SYSTEM_PROMPT`（`core/agent.py`）中描述它，讓模型知道何時該使用它。
4. 若它接受檔案系統路徑參數，在 `PATH_ARGS`（`core/tools.py`）中加入一個
   項目，讓 `resolve_paths()` 正規化它。
5. 若它具破壞性／不可逆，在 `PRE_TOOL_HOOKS`（`ui/permissions.py`）中註冊它
   （重用 `permission_ask`，或撰寫一個具有相同
   `(tool_name, args, console) -> bool` 簽章的新 hook）。它接著會透過
   `build_pre_tool_hooks()` 自動參與全部三種權限模式（approval／yolo／
   auto）— 不需要額外的依模式接線。
6. 在 `docs/tools.md` 中為它撰寫文件（並同步更新 `docs/tools.zh-TW.md`）。

# 測試：權限模式

[English](manual-testing.md) | 繁體中文

權限模式系統（approval／auto／yolo）有兩層測試涵蓋：一個不依賴 Ollama 的
自動化腳本，以及一份針對實際模型的手動檢查清單。先執行自動化腳本 — 它瞬間
完成且能抓到邏輯回歸 — 再走一遍手動檢查清單以取得端到端的信心，特別是在
修改 `ui/permissions.py`、`ui/turn.py` 或 `cli.py` 的參數解析之後。

（`--base-url`／`--model`／`--api-key` 雲端模型設定，以及
`core.agent.load_llm()` 的後端分支 — 與權限模式是不同的議題 — 請改看
`tests/test_model_config.py` 與 [`docs/models.zh-TW.md`](models.zh-TW.md)。）

## 自動化：`tests/test_permission_modes.py`

```bash
pytest tests/test_permission_modes.py
```

不需要執行任何 Ollama 行程 — 這個腳本從不匯入 `core` 也不碰模型，只直接
使用 `cli._parse_args()` 與 `ui/permissions.py` 的內部機制。在 pytest 下
執行（fixture — `monkeypatch`、`tmp_path`、來自 `tests/conftest.py` 的共用
`tmp_zeppeli_dirs` — 取代了舊的手寫 monkeypatch／還原樣板），失敗時以非零
結束碼離開，因此可以安全地接入 CI。

它涵蓋：

- 三種模式下的 `build_pre_tool_hooks(mode, cwd)` — yolo 回傳 `{}`，approval
  回傳真正的 `PRE_TOOL_HOOKS` 對應，auto 為 `write_file`／`delete_file`
  回傳同一個共用 hook
- `_is_within_cwd()` 路徑範圍判斷的邊界情況：相對路徑在內、絕對路徑在內、
  絕對路徑在外，以及 `..` 逃逸到外部
- auto-mode hook 的分支 — 在 cwd 內自動核准，完全不呼叫 `permission_ask`
  （以一個被呼叫就拋出例外的 spy 證明）；在 cwd 外則委派給
  `permission_ask` 並回傳它的結果（以一個分別回傳 `True` 與 `False` 的假物件
  證明）
- `cli.py` 的 argparse：同時給 `--yolo-mode --auto-mode` 會拋出
  `SystemExit(2)`；`-p`／`--prompt` 能正確解析（短形式、長形式、預設
  `None`，以及與模式旗標組合）
- `confirm_auto_mode_trust()` 的 index → bool 對應（透過 monkeypatch 的
  `_arrow_menu` 測 Yes／No／Ctrl+C／Esc）以及 `ui.repl.main()` 的信任閘門 —
  在互動 auto mode 中拒絕會完全跳過 `load_llm()`，而單次 `-p` 模式則從不呼叫
  `confirm_auto_mode_trust()`

預期 `24 passed`，結束碼 `0`。這裡失敗代表權限分派邏輯本身壞了 — 先修好它，
再去管下面的手動檢查清單。

## 自動化：`tests/test_images.py`

```bash
pytest tests/test_images.py
```

不依賴 Ollama／網路。涵蓋 `core/images.py` 的 `@` mention 解析、路徑解析、
縮小（受 Pillow 控制 — 若未安裝 Pillow，回報 `[SKIP]` 而非 `[FAIL]`）、
content-block 結構、`run_turn(images=...)` 整合、`cli.py --image` 驗證，
以及 `ui/repl.py` 中的 `/image` 暫存指令。其中兩個測試直接呼叫已安裝的
`langchain_ollama`／`langchain_litellm` 套件，確認兩個後端都接受本專案傳送
的確切 block 結構（若任一套件未安裝則 `[SKIP]`）— 見
[`docs/models.zh-TW.md`](models.zh-TW.md#vision--image-input)。

安裝完整 `requirements.txt` 時預期 `44 passed`（若在不完整的 venv 中觸發了
任何 Pillow／langchain 選用條件，pytest 會將其回報為 `skipped`，而不是算進
失敗）。這裡失敗代表圖片管線壞了 — 在進行下面的手動檢查清單之前先修好它。

## 手動：針對實際模型

這份檢查清單的其餘部分涵蓋真實情況 — 端到端、針對實際的 Ollama 模型，使用
`-p`／`--prompt`（見 [`docs/tools.zh-TW.md`](tools.zh-TW.md#permission-modes)）
一次執行一輪，不必待在 REPL 裡。

從 repo 根目錄執行每個指令。在進行下一項之前，清掉它建立的任何暫存檔
（`rm scratch_test.txt` 等）— 這些都不應該被 commit。

### 1. 預設（approval）模式 — 在 cwd 內會提示

```bash
python3 cli.py -p "Create a file called scratch_test.txt containing the text hello"
```

預期：出現互動式方向鍵選單（`AI wants to write to:
.../scratch_test.txt`，選項 **Yes**／**Yes, always allow (this
session)**／**No**，預設 **Yes**）。在預設值上按 Enter 會寫入檔案，AI 回報
成功。之後行程立即回到 shell — 不會出現 REPL 提示。

重新執行並改按 `Esc`：游標會在選單關閉前明顯跳到 **No**，AI 回報寫入已被
取消 — 與用方向鍵移到 **No** 再按 Enter 的結果相同。

### 2. `--yolo-mode` — 從不提示

```bash
python3 cli.py --yolo-mode -p "Create a file called scratch_test.txt containing the text hello"
```

預期：完全沒有提示 — 檔案立即寫入，接著行程結束。

### 3. `--auto-mode` — 在 cwd 內自動核准

```bash
python3 cli.py --auto-mode -p "Create a file called scratch_test.txt containing the text hello"
```

預期：**沒有信任提示**（因為是 `-p`，維持完全非互動 — 見下方 #7），一行
暗色說明 `✓ auto-approved (auto-mode): write_file →
.../scratch_test.txt`，沒有提示，檔案已寫入。

### 4. `--auto-mode` — 在 cwd 外退回提示

```bash
python3 cli.py --auto-mode -p "Create a file at /tmp/scratch_test_outside.txt containing the text hello"
```

預期：出現真正的互動選單（與 approval 模式相同），因為 `/tmp/...` 解析後
位於啟動目錄之外。

### 5. `-p` 恰好執行一輪後結束

```bash
time python3 cli.py --yolo-mode -p "What is 2 + 2?"
echo "exit code: $?"
```

預期：印出答案，然後直接回到 shell — 從不出現 `>` REPL 提示，`time` 顯示
大約一次模型往返的時間，結束碼 `0`。

### 6. Shift+Tab 在 session 中切換 Manual ↔ Auto

```bash
python3 cli.py
```

預期：工具列一開始是 `Model: <name>  |  Manual mode`（青綠色）。在輸入提示
列按 Shift+Tab：標籤／顏色立即變成 `auto mode`（琥珀色），不需按 Enter。
接著在啟動目錄內執行一次寫入（例如 `Create a file called scratch_test.txt
containing the text hello`）— 它會以一行暗色說明自動核准、沒有提示，確認
*下一輪*有採用切換後的模式。再按一次 Shift+Tab：變回 `Manual mode`（青綠色）。
現在以 `--yolo-mode` 啟動並按 Shift+Tab — 標籤維持 `yolo mode`；在那裡切換
沒有作用。

另請注意，Shift+Tab 切換本身永遠不會顯示下方的信任提示 — 那只在啟動時出現。

### 7. `--auto-mode`（互動）— 信任提示把關進入

```bash
python3 cli.py --auto-mode
```

預期，在任何其他事情之前（沒有模型載入延遲，也還沒有工具列）：

```
Zeppeli requires permission to read, edit, and execute files here.

> Yes, I trust this folder
  No, exit
```

在預設值（**Yes, I trust this folder**）上按 Enter：正常進入 REPL，工具列
顯示 `auto mode`。重新執行並選擇 **No, exit**（或 Ctrl+C）：印出 `Bye!` 並
直接回到 shell — 沒有載入模型，沒有 REPL。再重新執行一次並改按 `Esc`：游標
會在畫面關閉前明顯跳到 **No, exit**，然後行為完全相同 — `Bye!`、沒有載入
模型、沒有 REPL。重新執行 `python3 cli.py`（不加 `--auto-mode`）：確認提示
**不會**出現 — Manual 模式直接進入 REPL。

### 8. 打字中按 Esc 清空輸入列

```bash
python3 cli.py
```

預期：輸入部分訊息（不按 Enter），例如 `hello world` — 按 `Esc`，整行
完全清空，游標回到開頭，**瞬間完成，沒有可察覺的延遲**（`ui/repl.py` 中
特地把 session 的 `app.timeoutlen` 設為 `0`，讓這不會落後於 prompt_toolkit
預設 1 秒的 Esc／Alt 組合鍵判別等待 — 這個 app 本身沒有使用任何 Alt 組合鍵
綁定，所以沒有什麼需要等待；若這點回歸，先檢查那一行）。也試試在輸入 `/`
指令到一半時按 — 工具列下方的斜線指令提示行會隨著被清空的文字一起消失。在
已經空白的行上再按一次 `Esc`：什麼都不會發生，沒有錯誤，沒有崩潰。之後
確認 Shift+Tab 模式切換仍正常運作不受影響。

### 9. 工具列顯示模型的 context window

```bash
python3 cli.py
```

預期：工具列第三行顯示 `Session ID: … | Ctx: 0 k / yy k`，其中 `yy` 是已
載入模型的實際 context window — 與 `ollama show <model>` 自己回報的 context
length 交叉比對（或執行 `python3 -c "import ollama;
print(ollama.show('<model>').modelinfo)"` 並尋找 `*.context_length` 鍵）。
送出一則訊息，確認 `/` 之前的 `Ctx:` 數字會隨用量更新，而 `/ yy k` 視窗
維持不變。

接著執行 `python3 cli.py --base-url <url> --model <name>`（雲端／litellm
模型），確認工具列只顯示 `Ctx: xx k`，**沒有** `/ yy k` 後綴 — 非 Ollama
後端沒有對應的查詢機制。

### 10. 針對真實視覺端點的 `-p --image`

```bash
python3 cli.py --base-url http://<vllm-host>:8000/v1 \
  --model hosted_vllm/qwen3.6-27b-awq-int4 \
  -p "這張圖裡有什麼？" --image ./shot.png
```

預期：一個真正描述圖片內容的回應（而不是通用的「我看不到圖片」回答）。見
[`docs/models.zh-TW.md`](models.zh-TW.md#vision--image-input)。

### 11. REPL 中的 `@path` mention

```bash
python3 cli.py --base-url <url> --model hosted_vllm/qwen3.6-27b-awq-int4
> 這張圖有什麼問題 @shots/error.png
```

預期：在你橘色的輸入行下方回顯一行 `[image: shots/error.png]`，以及一個以
實際圖片為依據的回應。也試試拖放的路徑（會產生以 `\ ` 跳脫的空格，例如
`@Screen\ Shot\ 2026-08-17.png`）— 確認它能正確解析。接著送出一則包含
`@tool`（沒有圖片副檔名）的訊息，確認它*不會*被當作附件 — 沒有
`[image: ...]` 行，文字不變。

### 12. `/image` 暫存

```bash
> /image shots/other.png
> 那這張呢
```

預期：`/image shots/other.png` 印出一行暗色說明 `Staged: shots/other.png (1
image)`，且**不**算作一輪（沒有模型呼叫）。下一則訊息（`那這張呢`）會顯示
`[image: ...]` 回顯並包含該圖片。*再下一則*訊息則沒有附加圖片 — 暫存會被
消耗，不會黏著。也試試單獨的 `/image`（列出目前暫存的內容，或顯示用法）與
`/image clear`。

### 13. `@` 之後的 Tab 補全

```bash
python3 cli.py
```

輸入 `@` 加上部分路徑並按 Tab。預期：補全選單提供相符的檔案／目錄（只篩選
目錄與圖片副檔名），且底部工具列不會改變高度或留下空白行殘影 — 這是此功能
風險最高的部分（見 `ui/completion.py` 以及 `ui/repl.py` 中
`reserve_space_for_menu`／`complete_while_typing` 的註解）。確認輸入一般
文字（沒有 `@`）時從不跳出選單。

### 14. 純文字模型 + 圖片 → 友善的錯誤，而非崩潰

```bash
python3 cli.py --image shot.png
> describe this
```

使用不支援視覺的模型／端點（例如使用純文字 tag 的一般本機 `ChatOllama`）。
預期：透過 `_format_model_error()` 的不支援視覺提示（見
[`docs/models.zh-TW.md`](models.zh-TW.md#what-happens-with-a-text-only-model)）
顯示一行易讀的紅字，而非原始 traceback — 且 REPL 存活；後續的純文字訊息
仍能運作。

### 15. 不存在／過大／損毀的圖片 → 在任何模型呼叫之前報錯

```bash
> what's in @nonexistent.png
> /image /path/to/huge_50mb_photo.png
```

預期：在任何 spinner／模型呼叫*之前*印出紅色的 `Error: image not found: ...`
（或 `too large`／`unsupported image type`），且訊息歷史不變 — 透過
`@mention` 給的錯誤附件不會消耗該輪，也不會被當作純文字送出（見
`ui/turn.py` 中 `run_turn()` 的 `ImageError` 處理）。

### 16. 工具呼叫與串流期間工具列保持固定（渲染重寫）

```bash
python3 cli.py
> read the largest file in this repo and summarize it
```

詢問會迫使至少一次工具呼叫的問題。預期：底部工具列（`Model: ... | mode`、
`Session ID: ... | Ctx: ...`、斜線指令提示）在*整段*時間內都明顯存在並持續
重繪 — 經過 `[tool: ...]` 回顯、工具實際執行，以及串流的 Markdown 回應 —
從不消失到一般的捲動緩衝中，也從不留下凍結／過時的畫面。這正是這次重寫
（`ui/repl.py` 的持久 `Application`、`ui/live_region.py`）要修正的核心行為；
完整的工具列規格見 `ui/CLAUDE.md` 的「UI」章節。

### 17. 權限選單在輪次中正確顯示，並在之後恢復輸入

```bash
python3 cli.py
> create a file called scratch.txt with "hello" in it
```

在預設（approval）模式下。預期：Yes／Yes-always／No 方向鍵選單就地渲染
（相同的三個選項、相同的預設 **Yes**、與之前相同的 Esc 跳到 **No** 慣例），
工具列在旁持續渲染，而且 — 選擇選項後 — 輸入列以及一般的打字／補全對下一則
訊息完全恢復可用（確認 `ui/live_region.py` 的 `LiveRegion.ask_menu()` 即使
不再產生第二個 `Application`，仍能正確恢復焦點／按鍵綁定）。

### 18. 任何時候按 Ctrl+C 都乾淨地離開 — 絕不出現原始 traceback

在三種狀態下試按 Ctrl+C：在提示列閒置時、送出會觸發緩慢工具呼叫的訊息後
立刻按（工具執行中），以及回應仍在串流時。預期每次都有相同的乾淨關閉
（行程結束，沒有 traceback）— 這是相對於重寫之前的刻意改變，當時輪次中按
Ctrl+C 依設計是未捕捉的崩潰；現在整個持久 Application／輪次 task 會優雅地
拆除，在磁碟上留下與以前崩潰時相同的、誠實的 `"running"` 狀態 session 紀錄
（見 `ui/repl.py` 的 `_run_and_persist()` docstring）。

### 19. 單次 `-p` 模式不受影響

```bash
python3 cli.py -p "list the files in this directory"
```

預期沒有工具列（在 `-p` 模式中從不適用）、與重寫之前相同的即時更新串流
Markdown／spinner 渲染（`ui/live_region.py` 的 `SimpleLive` 透過一般的
`rich.live.Live`／`console.status()` 渲染，這條路徑完全不涉及
`Application`），以及對真正錯誤相同的結束碼／崩潰行為。

### 這裡任何變更後也值得重新檢查

- 修改 `ui/permissions.py` 的 `_arrow_menu`（由 `permission_ask` 與
  `confirm_auto_mode_trust` 共用）之後，重新驗證上方 #1 的行為仍完全相同 —
  相同的三個選項、相同的預設 **Yes**、相同的 Ctrl+C 視為拒絕，且 Esc 仍會在
  確認前明顯把游標移到 **No**。

- 取消訊息的修正：在 approval 或 auto-mode 中，對 cwd 外的寫入／刪除選擇
  **No**，確認 AI 的最終回覆說該動作已被取消 — 而不是說成功了（見
  `core/agent.py` 的 `SYSTEM_PROMPT` 以及 `ui/turn.py` 中的 `CANCELLED`
  `ToolMessage`）。
- 同時給 `--yolo-mode --auto-mode` 仍會以 argparse 錯誤非零結束，即使有
  `-p`：`python3 cli.py --yolo-mode --auto-mode -p "x"; echo $?` → 結束碼 `2`。

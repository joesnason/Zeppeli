# 模型：本機 Ollama vs. 透過 litellm 的雲端模型

[English](models.md) | 繁體中文

`core/agent.py` 的 `load_llm(model, base_url, api_key)` 是決定要建立哪個
後端的唯一位置：

```python
def load_llm(model: str | None = None, base_url: str | None = None, api_key: str | None = None):
    if base_url:
        from langchain_litellm import ChatLiteLLM  # lazy: keep litellm optional

        kwargs = {"model": model, "api_base": base_url}
        if api_key:
            kwargs["api_key"] = api_key
        return ChatLiteLLM(**kwargs).bind_tools(TOOLS)
    return ChatOllama(model=model or MODEL).bind_tools(TOOLS)
```

- `base_url` 為真值 → 透過
  [`langchain-litellm`](https://github.com/langchain-ai/langchain-litellm) 的
  `ChatLiteLLM` 使用雲端／自架模型。`langchain_litellm` 是在這個分支*內部*
  匯入，而非模組層級 — 因此 `langchain-litellm`／`litellm` 實質上維持選用：
  只使用本機 Ollama 的情況完全不需要安裝它們或讓它們能正常運作。
- 否則 → 本機 `ChatOllama`，`model` 退回 `MODEL` 常數 — 即目前的行為，
  不變。
- `api_key` 只有在為真值時才會加入 kwargs；永遠不會以 `api_key=None` 傳入。
- `model` 原樣傳遞 — **不會自動加上 `openai/` 前綴**（見下文）。

兩個分支都以相同方式呼叫 `.bind_tools(TOOLS)`，因此綁定後，所有下游
（`ui/turn.py` 的工具呼叫迴圈、`ui/streaming.py` 的逐 token 渲染）都針對
同一個 LangChain chat-model 介面運作。不過有一個結構差異會洩漏出來：chunk
的 `.content` 本身。`ChatOllama` 的 chunk 給的是純 `str`；某些經 litellm
路由的後端則給 content block 的 list — 見下方「`AIMessageChunk.content`
為 block 的 list」。`ui/streaming.py` 的 `_extract_text()` 會在 Markdown
渲染前將兩種結構正規化，因此 `stream_response()` 的呼叫者不需在意載入的是
哪個後端。

## CLI 旗標、環境變數後備與 config.json

三個與雲端相關的設定，每一個都可以來自旗標、環境變數或 `config.json`，
依此優先順序（`cli.py` 的 `_resolve_base_url()`／`_resolve_model()`／
`_resolve_api_key()`）：

| 設定 | 旗標 | 環境變數 | `config.json` 鍵 |
|---|---|---|---|
| Base URL | `--base-url URL` | `LITELLM_BASE_URL` | `base_url` |
| 模型 | `--model NAME` | `LITELLM_MODEL` | `model` |
| API key | `--api-key KEY` | `LITELLM_API_KEY` | `api_key` |

`cli.py` 的 `_resolve_config(args)` 解析這三者（旗標 > 環境變數 >
`config.json` > `None`），並驗證已解析的 `base_url` 不會在沒有已解析
`model` 的情況下出現 — 不論 `base_url` 來自哪一層，雲端端點都沒有合理的
預設模型字串。若發生這種情況，它會透過與 `--yolo-mode`／`--auto-mode`
互斥群組相同的機制報錯：`parser.error(...)`、`SystemExit(2)`，並將用法印到
stderr。

`config.json`（repo 根目錄、`cli.py` 旁邊，已被 git 忽略 — 複製
`config.json.example` 開始）是最低優先層，用於你不想每次都以旗標輸入或
以環境變數匯出的本機／每台機器測試預設值 — `model`／`base_url`／`api_key`
的任何子集都有效，包括空的 `{}` 或根本沒有檔案。`cli.py` 的
`_load_config_file()` 永不拋出例外：檔案不存在時保持沉默（常見情況），而
無法讀取的檔案、無效的 JSON、不是物件的 JSON，或無法辨識的頂層鍵，都會
各自在 stderr 印出一行警告，並退回旗標／環境變數／預設值解析，彷彿檔案
不存在（或對於未知鍵，彷彿只有那些鍵不存在）— 永遠不會 `SystemExit`，
因為一個過時／損毀的便利檔案不應該阻擋之後的每次啟動。可辨識的鍵若值不是
字串，則該鍵會被無聲地視為不存在。

若 `api_key` 解析為 `None`（沒有旗標、沒有 `LITELLM_API_KEY`、沒有
`config.json` 項目），就完全不會傳任何東西給 `ChatLiteLLM` — litellm 接著
會在內部退回它自己的 provider 專屬環境變數（例如 `openai/` 前綴模型的
`OPENAI_API_KEY`）。key 從來不是必要的。

只有 `--model`（沒有 `--base-url`／`LITELLM_BASE_URL`）時，會覆寫本機
Ollama 模型 tag — 這是 CLI 層級的覆寫，以前只能直接編輯 `core/agent.py`
中的 `MODEL` 常數才能做到。

## 自訂端點的 `openai/` 前綴慣例

litellm 是從 `model` 字串的前綴判斷要使用哪個 provider adapter，而非單看
`base_url`。對於自訂／自架的 OpenAI 相容端點，有文件記載的慣例
（docs.litellm.ai）是：

```bash
python3 cli.py --base-url https://your-endpoint.example.com --model openai/gpt-4o-mini --api-key sk-...
```

- `--model` 需要 `openai/` 前綴 — 本專案**不會**替你加上。
- `--base-url` **不應**帶結尾的 `/v1`。
- 環境變數等價寫法：`LITELLM_BASE_URL=https://your-endpoint.example.com LITELLM_MODEL=openai/gpt-4o-mini LITELLM_API_KEY=sk-... python3 cli.py`。

## 已知上游注意事項 — 請先針對你的實際端點測試

`langchain-litellm` 的 `ChatLiteLLM` 有尚未解決的上游 issue，正好落在本
專案依賴的 `bind_tools()` + 串流 + `usage_metadata` 組合上：

- [langchain-litellm#51](https://github.com/langchain-ai/langchain-litellm/issues/51) — 對於 Bedrock 後端的模型，`bind_tools().astream()` 會錯誤地路由到 OpenAI 的 `vector_stores` API。
- [langchain-litellm#52](https://github.com/langchain-ai/langchain-litellm/issues/52) — 串流期間 Anthropic prompt-cache 的 token 欄位會從 `usage_metadata` 中被移除（非串流呼叫時存在）。

兩者都集中在 Bedrock 路由與 Anthropic prompt-caching 欄位 — 一般的 OpenAI
相容自訂端點很可能不受影響，但仍需針對你實際指向的端點驗證。這裡刻意
**不**為此撰寫繞道程式；請把它當作「自己測一次」的項目，而不是本專案程式碼
中的已知 bug。

### `AIMessageChunk.content` 為 block 的 list

這一個*有*在 `ui/streaming.py` 中處理。`ChatOllama` 的 chunk 一律以純
`str` 提供 `.content`。某些經 litellm 路由的後端（在 `ChatLiteLLM` 背後
接 Anthropic 風格 API 時觀察到）則以 content block 的 list 提供
`.content`，例如 `[{"type": "text", "text": "..."}]`，有時還混雜非文字
block（`tool_use` 等）。在處理這點之前，`stream_response()` 會把那個 list
直接指派給它的 `accumulated` 字串，而 `RichMarkdown(accumulated)` 會在第一個
chunk 就拋出 `TypeError: Input data should be a string, not <class 'list'>`。

`_extract_text(content)` 會把任一結構（`str`，或由 `str`／
`{"type": "text", "text": ...}` dict 組成的 `list`，忽略其他 block 類型）
攤平成純文字；`stream_response()` 對每個 chunk 都呼叫它，而非直接使用
`chunk.content`。由 `tests/test_streaming.py` 涵蓋，不需要實際端點。

<a id="vision--image-input"></a>
## 視覺／圖片輸入（Vision / image input）

`core/images.py` 從圖片清單建立 `HumanMessage` 內容 — 它如何被觸發
（`@path` mention、`/image` 指令、`--image` 旗標）見
[`docs/tools.zh-TW.md`](tools.zh-TW.md#images-are-not-a-tool)。本文件關注的
有趣之處在於：**同一個 content-block 格式不需修改就能在兩個後端上運作**：

```python
{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
```

- `ChatOllama`（`langchain_ollama/chat_models.py` 的
  `_convert_messages_to_ollama_messages()`）辨識這個確切結構，自行去除
  `data:...;base64,` 前綴，並把原始 base64 放入 Ollama 自己的 `images` 欄位。
- `ChatLiteLLM`（`langchain_litellm` 的 `_convert_message_to_dict()`）不會把
  `"image_url"` 辨識為 `langchain_core` 較新的型別化資料 content block 之一，
  因此會落入「標準文字 block 或其他無法辨識的 dict 格式原樣傳遞」的分支 —
  該 block 會逐位元組地按建構時的樣子到達 litellm，進而到達 `hosted_vllm/`
  端點。已直接針對已安裝的套件驗證（而不只是閱讀原始碼）— 見
  `tests/test_images.py` 的 `test_litellm_message_conversion_passes_list_content`
  — 並以擷取實際 HTTP 請求本文的模擬 OpenAI 相容伺服器做了端到端驗證。

因此，為 `hosted_vllm/qwen3.6-27b-awq-int4` 的執行附加圖片，除了上方
「`openai/` 前綴慣例」已涵蓋的內容外，**不需要任何**額外設定 — `model` 原樣
傳遞，所以 `--model hosted_vllm/qwen3.6-27b-awq-int4 --base-url
http://host:8000/v1` 本來就能正確路由；唯一新增的只是附加圖片。

沒有附加圖片時，`build_message_content()` 回傳純 `str` — 絕不是只有一個
元素的 list。這很重要，因為 `ChatOllama` 的 list 內容分支會在它看到的每個
文字部分前面加上 `\n`；若一律回傳 list，會無聲地為每個既有的純文字提示
加上開頭換行。

### 縮小與大小上限

編碼前，`core/images._downscale()`（延遲匯入 `PIL`）會把長邊縮到 `1568px`
（永不放大），並重新編碼為 PNG（若來源有 alpha）或 JPEG，逐步降低品質／
尺寸，直到 base64 酬載小於 `MAX_ENCODED_BYTES`（4 MB）。`MAX_SOURCE_BYTES`
（20 MB）會在開啟之前就拒絕過大的原始檔。`MAX_IMAGES`（每則訊息 4 張）×
`MAX_ENCODED_BYTES` 代表單一請求最多可攜帶約 16 MB 的圖片資料 — 對 vLLM 本身
沒問題，但若端點前面有反向代理（例如 nginx），請檢查它的
`client_max_body_size` — 常見的 `1m` 預設值會在請求到達 vLLM 之前就以不透明
的錯誤拒絕它。

若未安裝 Pillow，`_downscale()` 會在檔案小於 2 MB 時退回原樣傳送原始檔，
對於更大的檔案則拋出清楚的 `ImageError`（要求 `pip3 install Pillow`）— 它
不會讓整個功能硬性失敗。

<a id="what-happens-with-a-text-only-model"></a>
### 遇到純文字模型時會怎樣

若載入的模型不懂 `image_url` 內容（非視覺的 OpenAI 相容端點，或純文字的
Ollama tag），provider 會回傳錯誤，並經由與其他所有模型呼叫失敗相同的路徑
浮現 — `ui/streaming.py` 的 `stream_response()` 捕捉它並呼叫
`_format_model_error()`。其中有一個分支會比對圖片拒絕的措辭（`"image"` 加上
`"not support"`／`"unsupported"`／`"invalid"`／`"no vision"`），回傳一行
建議改用視覺模型的提示，而不是傾印原始的 provider 錯誤。

## `usage_metadata` 與工具列的 `Ctx: xx k` 計數器

`ui/streaming.py` 的 `_update_ctx()` 本來就能容忍沒有 `usage_metadata` 的
回應：

```python
def _update_ctx(response):
    usage = getattr(response, "usage_metadata", None)
    if usage:
        _ctx_state["tokens"] = usage["input_tokens"]
```

若雲端後端沒有在串流 chunk 上填入 `usage_metadata`（見上方注意事項），工具
列的 `Ctx: xx k` 計數器只會停在最後已知的值而不更新 — 不會崩潰，也不需修改
程式。

## Context window 查詢與工具列的 `/ yy k` 後綴

`core/agent.py` 的 `get_context_window(model)` 會在工具列的 `Ctx: xx k`
那一行後面附加 `/ yy k` 後綴，在目前用量旁顯示已載入模型的 context window
（最大 token 數）。它使用官方的 `ollama` PyPI 套件（`ollama.show(model)` →
Ollama 的 `/api/show`），**而非** `langchain_ollama`／`ChatOllama` —
`ChatOllama` 沒有透過自己的介面公開這項 metadata；只有原始的 `/api/show`
回應才帶有它，位於 `modelinfo` dict（來自 JSON `model_info` 欄位的別名）。
兩者都指向 `ChatOllama` 隱含使用的同一個 Ollama 主機（`OLLAMA_HOST` 環境
變數或 localhost 預設值），但它們是不同的套件、不同的呼叫。

`modelinfo` 對 context window 沒有固定的欄位名稱 — Ollama 會發出帶家族前綴
的鍵（`"llama.context_length"`、`"gemma3.context_length"` 等，依模型架構
而定），因此 `get_context_window()` 會掃描第一個以 `.context_length` 結尾的
鍵，而不是以已知的鍵索引：

```python
modelinfo = info.modelinfo or {}
for key, value in modelinfo.items():
    if key.endswith(".context_length"):
        return int(value)
```

這**只適用於本機 Ollama** — 透過 `--base-url` 載入的雲端／自架模型沒有對應
機制（`load_llm()` 的 litellm 分支不是 Ollama 伺服器），因此 `ui/repl.py`
的 `main()` 只在 `base_url` 為假值時才呼叫 `get_context_window()`。每個行程
只取得**一次**，就在建立互動式 `PromptSession` 之前 — 不是每輪、也不是每次
工具列重繪（工具列 callback 只讀取快取的結果）— 而在單次 `-p` 模式中完全
不取得，因為那裡沒有工具列可顯示。任何失敗（Ollama 無法連線、未知的模型
tag、非預期的回應結構）都會讓函式回傳 `None` 而非拋出例外；工具列接著只會
省略該後綴。

由 `tests/test_model_config.py` 以假的 `ollama` 模組（不依賴實際 Ollama）
涵蓋鍵比對與錯誤處理邏輯。**未**自動涵蓋的是：對你實際執行的模型，Ollama
真正的 `/api/show` 回應是否仍遵循此處假設的 `<family>.context_length` 鍵
慣例 — 在信賴工具列數字之前，請針對實際的 Ollama 執行個體手動驗證
（`ollama show <model>` 或 `python3 -c "import ollama;
print(ollama.show('<model>').modelinfo)"`），精神上與上方的 litellm 注意
事項類似。

<a id="turn-level-context-compaction-25-turn-sliding-window"></a>
## 輪次層級的 context 壓縮（25 輪滑動視窗）

記憶體中的 `messages` 清單（`ui/repl.py` 的 `main()`）會在行程整個生命週期中
持續成長 — 每輪都往上追加，沒有任何東西會修剪它。若不加以控制，長時間的
對話最終可能超出模型的 context window。`core/messages.py` 的
`compact_messages()` 透過為模型建立對話的**輪次視窗視圖**來處理此問題，
而從不碰正式清單本身。

一個**輪次**是一則 `HumanMessage` 加上其後的所有內容（來自 `ui/turn.py` 的
`run_turn()` 的 `AIMessage`／`ToolMessage` hop），直到但不包括下一則
`HumanMessage`。一旦對話超過 `_MAX_TURNS`（25）輪，`compact_messages()`
只回傳：

- 第一輪（`_KEEP_FIRST_TURNS = 1`）— 使用者對這段對話的原始意圖，以及
- 最近的 24 輪（`_KEEP_LAST_TURNS = 24`）

— 總共 25 輪份的訊息。任何前導內容（第一則 `HumanMessage` 之前的所有東西 —
實務上就是一定存在的 `SystemMessage`）會完整保留，且不計入 25 輪。在門檻
以下或剛好等於門檻時，對話原樣回傳。

**為何單位是完整的輪次，而非原始訊息數**：一個 hop 的
`AIMessage(tool_calls=[...])` 後面一定緊跟著一或多則對應的 `ToolMessage`
（以 `tool_call_id` 配對）。天真的「保留最後 N 則原始訊息」切片可能落在
配對中間 — 丟掉 `AIMessage` 卻留下孤立的 `ToolMessage`，或反之。
LangChain 的 Ollama／litellm 訊息轉換器不會在 client 端驗證這點，但 OpenAI
相容的雲端 API（經由 litellm 到達）會在伺服器端拒絕格式錯誤的序列。由於
一輪永遠是完整、自成一體的單位，以輪次分組讓配對被切斷在結構上不可能發生 —
一輪要嘛整個保留，要嘛整個丟棄。

壓縮是**無聲的**：不會插入任何標記或說明告訴模型有輪次被省略（不同於
`truncate_tool_output()` 的 `[truncated N lines/chars]` 說明 — 見上文）。
它也獨立於、且疊在每個工具輸出上限*之上*：`truncate_tool_output()` 仍會在
被保留的輪次內、於單一工具結果層級執行；`compact_messages()` 則在更上一層、
整個訊息清單層級運作。

接在每次模型呼叫都必經的單一關卡上：`ui/streaming.py` 的
`stream_response()` 每個 hop 呼叫一次 `compact_messages(messages)`，就在它
兩個 `.stream()` 呼叫點（主要嘗試與推理後備重試）之前，並把結果 — 絕不是
`messages` 本身 — 傳給模型。這為何對 session／事件日誌持久化是安全的（它們
只讀取正式、未壓縮的 `messages` 清單），見
[`docs/sessions.zh-TW.md`](sessions.zh-TW.md#message--history-conversion)。

由 `tests/test_compaction.py` 涵蓋：在門檻及以上的輪次計數／分組正確性、
前導內容處理、多 hop 工具呼叫輪次在被保留時完整存活、不被保留時整體丟棄、
不修改輸入清單，以及一個 `stream_response()` 整合測試，確認模型收到的是
壓縮視圖，而呼叫者自己的 `messages` 清單維持不變。

## 基於預算的第二層壓縮（摘要中段）

上方的第一層是固定的結構上限（25 輪），而非 token 預算 — 一段大量使用工具
的對話仍可能維持在 25 輪以下，卻攜帶遠超過較小 context 模型實際能容納的
文字。`core/messages.py` 的 `compact_messages_to_budget()` 是第二道、更積極
的層，在第一層*之後*、於第一層已壓縮的視圖上、在同一個 `ui/streaming.py` 的
`stream_response()` 關卡內執行：

```python
view = _compact_messages(messages)
view = _compact_messages_to_budget(view, context_window)
```

**觸發條件**是粗略的 token 數估計，而非真正的 tokenizer：所有訊息
`.content` 的總字元數（透過 `extract_text()`），加上任何
`AIMessage.tool_calls` 酬載的 JSON 表示（它也是真實請求酬載的一部分），
除以 4（`_CHARS_PER_TOKEN` — 「1 token ≈ 4 字元」的啟發式）。這個估計會與
`context_window` 的 80%（`_BUDGET_TRIGGER_RATIO`）比較 — 該值由
`core/agent.py` 的 `get_context_window()` 一路傳下來（僅限本機 Ollama）—
或在無法取得時（雲端／litellm 模型，或查詢失敗）使用 256,000 token 的
`_DEFAULT_CONTEXT_WINDOW`。`256_000` 使用 `k = 1000`，與本檔其他地方工具列
自身的 `tokens // 1000` 顯示慣例一致 — 這是有文件記載的假設，並非綁定任何
特定模型的數字。

**若被觸發**，視圖會進一步壓縮為只剩第一輪（`_BUDGET_KEEP_FIRST_TURNS = 1`）
與最近 6 輪（`_BUDGET_KEEP_LAST_TURNS = 6`）— 刻意比第一層的 24 更緊，因為
這只在第一層自己的 25 輪視窗已經太多時才會觸發。兩者之間各輪的每則訊息
（「被丟棄的中段」）會依原始順序攤平為**每則原始訊息一個項目符號**（而非
每輪一個），並打包成單一合成的 `HumanMessage`（role `"user"`）：一行標頭
（`"[Earlier conversation summarized to fit context budget]"`），後接編號的
項目符號，每個都以原始 LangChain role 標記，使用與 `docs/sessions.zh-TW.md`
為對話紀錄記載的相同對應（`HumanMessage` → `user:`、`AIMessage` →
`assistant:`、`ToolMessage` → `tool:`）。`ToolMessage` 項目會透過從被摘要的
`AIMessage.tool_calls` 項目建立的查表來解析其工具名稱（它只帶有
`tool_call_id`），並寫成 `Previous tool result for <name>: <content>`。帶有
`tool_calls` 的 `AIMessage` 會將它們渲染為真正的 JSON（`json.dumps`）—
`{"tool": "...", "args": {...}}`，若一個 hop 做了多個呼叫則為 JSON 陣列 —
與此功能自己的範例一致，而非 Python 的 `repr()`。與第一層不同，這**不是
無聲的**：它是被移除內容的明確、逐項紀錄，讓模型對被省略的上下文仍保有
一些認知。

每個項目符號顯示的內容是從第一層已壓縮的 `.content`（而非
`additional_kwargs["full_output"]`）建立，**只有在超過 650 字元**
（`_SUMMARY_TRUNCATE_THRESHOLD`）時才截斷為前 500（`_SUMMARY_KEEP_HEAD`）+
後 150（`_SUMMARY_KEEP_TAIL`）字元 — 與上方 `truncate_tool_output()` 相同的
條件式截斷慣例，透過同類的 `_truncate_for_summary()` 輔助函式（相同的括號
說明樣式，較小的保留量以適合精簡的單行項目）。短的項目保持完整。

**可接受的限制**：若第一層的視圖已只有 7 輪或更少
（`_BUDGET_KEEP_FIRST_TURNS + _BUDGET_KEEP_LAST_TURNS`），就沒有可以摘要掉的
「中段」，除非也動到保證保留的第一輪或最近 6 輪 — 這種情況下第二層什麼都
不做，即使估計的 token 數仍超出預算。

**需要注意的行為變更**：為了讓單次 `-p` 模式（而不只是互動 REPL）也能讓
第二層拿到真正的預算，`ui/repl.py` 的 `main()` 現在只要 `base_url` 為假值
就會無條件呼叫 `get_context_window()` — 以前這個呼叫只發生在互動路徑上
（在單次模式提早返回之後），所以 `-p` 模式完全不會做這個呼叫。這會讓每次
使用本機 Ollama 模型的單次呼叫多一次本機 `ollama.show()` 往返。

由 `tests/test_compaction.py` 涵蓋：門檻以下／剛好等於門檻時不變、觸發後的
第一輪＋最近 6 輪＋摘要結構、≤7 輪的下限情況、摘要中的 role 標記與工具名稱
解析（包括多工具呼叫 hop 渲染為一個項目而非兩個）、650 字元的條件式截斷
（`_truncate_for_summary()` 兩個分支）、`context_window=None` 預設後備路徑、
不修改輸入清單，以及一個 `stream_response()` 整合測試，確認觸發時面向模型的
視圖反映第二層的結構，而呼叫者的 `messages` 清單維持不變。

## 測試

`tests/test_model_config.py` 涵蓋所有旗標／環境變數／config.json 解析邏輯
以及 `load_llm()` 的分支，不依賴 Ollama 或網路 — 見
[`docs/manual-testing.zh-TW.md`](manual-testing.zh-TW.md)。

針對真實雲端端點的實際端到端往返（`--base-url`／`--model`／`--api-key`，
確認設定解析與串流都能對真實 provider 運作）已於 2026-08-13 手動端到端
驗證 — 這仍需要只有你擁有的憑證，因此不是本 repo 的自動化測試能自行涵蓋的：

```bash
python3 cli.py --base-url <real-url> --model openai/<real-model> --api-key <key> -p "What is 2 + 2?"
```

視覺路徑另外需要一次實際傳送圖片位元組的往返 — 上方的模擬伺服器檢查
（`tests/test_images.py`）證明請求格式正確，但不能證明真實的 vLLM／qwen
後端會接受並讀取它：

```bash
python3 cli.py --base-url http://<vllm-host>:8000/v1 \
  --model hosted_vllm/qwen3.6-27b-awq-int4 \
  -p "這張圖裡有什麼？" --image ./shot.png
```

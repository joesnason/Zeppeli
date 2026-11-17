# Skills

[English](skills.md) | 繁體中文

**skill** 是一個資料夾，裡面放著特定任務的說明（以及任何附屬檔案，例如
腳本），讓模型可以在需要時取用。session 開始時，Zeppeli 會把每個可用
skill 的名稱與說明列在 system prompt 中；當請求符合某個 skill 時，模型會用
`read_file(path="skill://<name>")` 讀取完整說明。所有邏輯都在
`core/skills.py`（沒有 UI 依賴）。

## skill 放在哪裡

| 層級 | 目錄 |
|---|---|
| 使用者 | `~/.zeppeli/skills/<skill-name>/SKILL.md` |
| 專案 | `<啟動目錄>/.zeppeli/skills/<skill-name>/SKILL.md` |

- **資料夾名稱就是 skill 名稱** — frontmatter 中不同的 `name:` 不會用於查找。
- 兩個層級會合併；名稱衝突時**專案層優先**。
- 沒有 `SKILL.md`（或無法讀取）的資料夾會被略過。
- 對 Slack bot 而言，專案層是 `<allowed_dir>/.zeppeli/skills/`。

## `SKILL.md` 格式

```markdown
---
name: mail-request-checker
description: "這個 skill 做什麼、何時使用。Use when: ..."
disable-model-invocation: false   # 選用
---

# 完整說明（Markdown）
...執行 `python3 ./scripts/validate_mail.py <dir>`...
```

| 欄位 | 意義 |
|---|---|
| `name` | skill 名稱（僅供參考；以資料夾名稱為準）。 |
| `description` | skill 的用途與適用時機 — 這是模型一開始唯一看得到的部分，請寫得具體。多行的值會合併成一行。 |
| `disable-model-invocation` | `true` 會完全隱藏該 skill：不列在 system prompt 中，也無法透過 `skill://` 讀取。省略時**預設為 `false`**。 |

frontmatter 以 `yaml.safe_load` 解析（`parse_frontmatter()`）。缺少、未結束
或無效的 frontmatter 永遠不會拋出例外 — 會被視為空的（skill 仍會被列出，
只是沒有說明）。其他鍵（例如 `argument-hint`）允許存在並會被忽略。

## 生命週期

- **每個 session 掃描一次**：`ui/repl.py` 的 `main()` 在建立
  `SystemMessage` 之前呼叫 `load_skills(initial_cwd)`（互動 REPL 與單次
  `-p` 皆然）；`slack_bot.py` 在啟動時呼叫一次，每個新討論串的
  `SystemMessage` 都使用同一份清單（`slack_bot/sessions.py`）。新增或修改
  skill 會在下次啟動時生效。
- `build_skills_prompt()` 會在 `SYSTEM_PROMPT` 之後、`Working directory:`
  那一行之前附加一個「Available skills」區段（每個 skill 一行
  `- <name>: <description>`，加上 `skill://` 的使用方式）。沒有 skill 時回傳
  `""`，因此 prompt 與之前逐位元組相同。
- 掃描到的 skill 保存在模組層級的註冊表（`_active_skills`）中，因為
  `read_file()` 是一個沒有 session 上下文的一般 `@tool`。

## `skill://` URI

只有 `read_file` 看得懂它們：

| URI | 讀取 |
|---|---|
| `skill://<name>`（或 `skill://<name>/`） | `<skill 資料夾>/SKILL.md` |
| `skill://<name>/<相對路徑>` | `<skill 資料夾>/<相對路徑>`，例如 `skill://mail-request-checker/scripts/validate_mail.py` |

- `core/tools.py` 的 `resolve_paths()` 不會處理 `skill://` 的值（否則
  `pathlib` 會把它們變成 `<cwd>/skill:/<name>`）；`read_file()` 透過
  `resolve_skill_uri()` 解析它們，接著使用一般的分頁讀取（`offset`／
  `limit`／`max_bytes` 都仍然適用）。
- 標頭同時顯示 URI 與真實資料夾：
  `[File: skill://<name> | skill dir: /abs/path | lines 1–42 | ... bytes]`。
  當 skill 要求以 `run_bash` 執行它的腳本時，模型會使用這個絕對路徑。
- 錯誤以一般的 `[read_file] Error: ...` 字串回傳：未知或已停用的 skill
  （訊息會列出可用的 skill）、跳出 skill 資料夾的路徑（例如
  `skill://a/../b/SKILL.md` — 在 `Path.resolve()` 之後檢查），或檔案不存在。

## 安全注意事項

- skill 的文字是模型會遵循的指示。只安裝你信任的 skill — 特別是不是你寫的
  repo 中的專案層 skill。
- 執行 skill 的腳本會經過 `run_bash` 一般的 hook：使用者層 skill 位於工作區
  之外，因此在 approval／auto 模式下執行它的腳本會詢問核准（yolo 模式從不
  詢問）。Slack bot 完全沒有 `run_bash`。

## 測試

`tests/test_skills.py` 涵蓋 frontmatter 解析、掃描（合併、專案層優先於
使用者層、資料夾名稱即 skill 名稱、缺少 `SKILL.md`、
`disable-model-invocation`）、prompt 區段、`skill://` 解析（預設
`SKILL.md`、子路徑、`..` 逃逸、未知 skill）、`read_file`／`resolve_paths`
整合，以及 `ui.repl.main()` 把該區段放進 system message — 不依賴 Ollama
或網路。共用的 `tmp_zeppeli_dirs` fixture 也會重新導向 `USER_SKILLS_DIR`，
因此真實的 `~/.zeppeli/skills` 永遠不會滲入測試。

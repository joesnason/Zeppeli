# Testing: permission modes

Two layers of test coverage for the permission-mode system (approval/auto/
yolo): an automated script with no Ollama dependency, and a manual checklist
against the live model. Run the automated script first — it's instant and
catches logic regressions — then walk the manual checklist for end-to-end
confidence, especially after touching `ui/permissions.py`, `ui/turn.py`, or
`cli.py`'s argument parsing.

(For `--base-url`/`--model`/`--api-key` cloud-model config and
`core.agent.load_llm()`'s backend branching — a separate concern from
permission modes — see `test_model_config.py` and
[`docs/models.md`](models.md) instead.)

## Automated: `test_permission_modes.py`

```bash
python3 test_permission_modes.py
```

No Ollama process needs to be running — the script never imports `core`
or touches the model, only `cli._parse_args()` and `ui/permissions.py`'s
internals directly. Framework-free (`assert` + print, matching
`test_tool_call.py`'s style), but unlike that script it exits non-zero on
failure, so it's safe to wire into CI.

It covers, with plain `[PASS]`/`[FAIL] <name>: <error>` output per test and
an `N/M passed` summary:

- `build_pre_tool_hooks(mode, cwd)` for all three modes — yolo returns `{}`,
  approval returns the real `PRE_TOOL_HOOKS` mapping, auto returns one
  shared hook for both `write_file`/`delete_file`
- `_is_within_cwd()` path-containment edge cases: relative-inside,
  absolute-inside, absolute-outside, and `..`-escape-outside
- the auto-mode hook's branching — inside cwd auto-approves without ever
  calling `permission_ask` (proven via a spy that raises if called);
  outside cwd delegates to `permission_ask` and returns whatever it returns
  (proven via a fake returning both `True` and `False`)
- `cli.py`'s argparse: `--yolo-mode --auto-mode` together raises
  `SystemExit(2)`; `-p`/`--prompt` parse correctly (short form, long form,
  default `None`, and combined with a mode flag)
- `confirm_auto_mode_trust()`'s index → bool mapping (Yes/No/Ctrl+C/Esc via
  a monkeypatched `_arrow_menu`) and `ui.repl.main()`'s trust gate —
  declining in interactive auto mode skips `load_llm()` entirely, and
  one-shot `-p` mode never calls `confirm_auto_mode_trust()` at all

Expect `19/19 passed`, exit code `0`. A failure here means the permission
dispatch logic itself broke — fix that before bothering with the manual
checklist below.

## Manual: against the live model

The rest of this checklist covers the real thing — end-to-end, against the
live Ollama model, using `-p`/`--prompt` (see
[`docs/tools.md`](tools.md#permission-modes)) to run one turn at a time
without needing to sit in the REPL.

Run each command from the repo root. Clean up any scratch file it creates
before moving to the next one (`rm scratch_test.txt`, etc.) — none of this
should get committed.

### 1. Default (approval) mode — prompts inside cwd

```bash
python3 cli.py -p "Create a file called scratch_test.txt containing the text hello"
```

Expect: the interactive arrow-key menu appears (`AI wants to write to:
.../scratch_test.txt`, options **Yes** / **Yes, always allow (this
session)** / **No**, default **Yes**). Pressing Enter on the default writes
the file and the AI reports success. The process exits to the shell right
after — no REPL prompt appears.

Re-run and press `Esc` instead: the cursor visibly jumps down to **No**
before the menu closes, and the AI reports the write was cancelled — same
outcome as arrowing to **No** and pressing Enter.

### 2. `--yolo-mode` — never prompts

```bash
python3 cli.py --yolo-mode -p "Create a file called scratch_test.txt containing the text hello"
```

Expect: no prompt at all — the file is written immediately, then the
process exits.

### 3. `--auto-mode` — auto-approves inside cwd

```bash
python3 cli.py --auto-mode -p "Create a file called scratch_test.txt containing the text hello"
```

Expect: **no trust prompt** (that's `-p`, which stays fully non-interactive
— see #7 below), a dim note `✓ auto-approved (auto-mode): write_file →
.../scratch_test.txt`, no prompt, file written.

### 4. `--auto-mode` — falls back to a prompt outside cwd

```bash
python3 cli.py --auto-mode -p "Create a file at /tmp/scratch_test_outside.txt containing the text hello"
```

Expect: the real interactive menu appears (identical to approval-mode's),
since `/tmp/...` resolves outside the launch directory.

### 5. `-p` exits after exactly one turn

```bash
time python3 cli.py --yolo-mode -p "What is 2 + 2?"
echo "exit code: $?"
```

Expect: prints the answer, then returns straight to the shell — no `>` REPL
prompt ever appears, `time` shows roughly one model round-trip, exit code
`0`.

### 6. Shift+Tab toggles Manual ↔ Auto mid-session

```bash
python3 cli.py
```

Expect: toolbar starts at `Model: <name>  |  Manual mode` (teal). Press
Shift+Tab at the input prompt: the label/color flip immediately to `auto
mode` (amber), with no Enter needed. Then run a write inside the launch dir
(e.g. `Create a file called scratch_test.txt containing the text hello`) —
it auto-approves with a dim note, no prompt, confirming the *next* turn
picked up the toggled mode. Press Shift+Tab again: flips back to `Manual
mode` (teal). Now launch with `--yolo-mode` and press Shift+Tab — the label
stays `yolo mode`; the toggle is a no-op there.

Also note that the Shift+Tab toggle itself never shows the trust prompt
below — that's launch-time-only.

### 7. `--auto-mode` (interactive) — trust prompt gates entry

```bash
python3 cli.py --auto-mode
```

Expect, before anything else (no model-loading delay, no toolbar yet):

```
Zeppeli requires permission to read, edit, and execute files here.

> Yes, I trust this folder
  No, exit
```

Press Enter on the default (**Yes, I trust this folder**): proceeds
normally into the REPL, toolbar shows `auto mode`. Re-run and select **No,
exit** (or Ctrl+C): prints `Bye!` and returns straight to the shell — no
model load, no REPL. Re-run once more and press `Esc` instead: the cursor
visibly jumps to **No, exit** before the screen closes, then behaves
identically — `Bye!`, no model load, no REPL. Re-run `python3 cli.py` (no
`--auto-mode`): confirm the prompt does **not** appear — Manual mode starts
straight into the REPL.

### 8. Esc clears the input line mid-typing

```bash
python3 cli.py
```

Expect: type a partial message (no Enter), e.g. `hello world` — press `Esc`
and the line goes fully empty, cursor back at the start, **instantly, no
perceptible delay** (the session's `app.timeoutlen` is set to `0` in
`ui/repl.py` specifically so this doesn't lag behind prompt_toolkit's
default 1s Esc/Alt-combo disambiguation wait — this app doesn't use any
Alt-combo bindings itself, so there's nothing to wait for; if this
regresses, check that line first). Try mid-typing a `/` command too — the
slash-command hint lines below the toolbar disappear along with the
cleared text. Press `Esc` again on the now-empty line: nothing happens, no
error, no crash. Confirm Shift+Tab mode toggling still
works unaffected afterward.

### Also worth re-checking after any change here

- After touching `ui/permissions.py`'s `_arrow_menu` (shared by
  `permission_ask` and `confirm_auto_mode_trust`), re-verify #1 above still
  behaves identically — same three options, same default **Yes**, same
  Ctrl+C-as-deny, and Esc still visibly moving the cursor to **No** before
  confirming it.

- The cancellation-message fix: in approval or auto-mode, choose **No** on a
  write/delete outside cwd and confirm the AI's final reply says the action
  was cancelled — not that it succeeded (see `core/agent.py`'s
  `SYSTEM_PROMPT` and the `CANCELLED` `ToolMessage` in `ui/turn.py`).
- `--yolo-mode --auto-mode` together still exits non-zero with an argparse
  error, even with `-p` present: `python3 cli.py --yolo-mode --auto-mode -p "x"; echo $?` → exit code `2`.

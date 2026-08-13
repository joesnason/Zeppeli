# Testing: permission modes

Two layers of test coverage for the permission-mode system (approval/auto/
yolo): an automated script with no Ollama dependency, and a manual checklist
against the live model. Run the automated script first — it's instant and
catches logic regressions — then walk the manual checklist for end-to-end
confidence, especially after touching `ui/permissions.py`, `ui/turn.py`, or
`cli.py`'s argument parsing.

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

Expect `14/14 passed`, exit code `0`. A failure here means the permission
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

Expect: a dim note `✓ auto-approved (auto-mode): write_file →
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

### Also worth re-checking after any change here

- The cancellation-message fix: in approval or auto-mode, choose **No** on a
  write/delete outside cwd and confirm the AI's final reply says the action
  was cancelled — not that it succeeded (see `core/agent.py`'s
  `SYSTEM_PROMPT` and the `CANCELLED` `ToolMessage` in `ui/turn.py`).
- `--yolo-mode --auto-mode` together still exits non-zero with an argparse
  error, even with `-p` present: `python3 cli.py --yolo-mode --auto-mode -p "x"; echo $?` → exit code `2`.

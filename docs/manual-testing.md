# Manual testing: permission modes

`python3 test_permission_modes.py` covers the permission-mode *logic*
without touching the model. This checklist covers the real thing —
end-to-end, against the live Ollama model, using `-p`/`--prompt` (see
[`docs/tools.md`](tools.md#permission-modes)) to run one turn at a time
without needing to sit in the REPL.

Run each command from the repo root. Clean up any scratch file it creates
before moving to the next one (`rm scratch_test.txt`, etc.) — none of this
should get committed.

## 1. Default (approval) mode — prompts inside cwd

```bash
python3 cli.py -p "Create a file called scratch_test.txt containing the text hello"
```

Expect: the interactive arrow-key menu appears (`AI wants to write to:
.../scratch_test.txt`, options **Yes** / **Always allow (this session)** /
**No**, default **No**). Selecting **Yes** writes the file and the AI
reports success. The process exits to the shell right after — no REPL
prompt appears.

## 2. `--yolo-mode` — never prompts

```bash
python3 cli.py --yolo-mode -p "Create a file called scratch_test.txt containing the text hello"
```

Expect: no prompt at all — the file is written immediately, then the
process exits.

## 3. `--auto-mode` — auto-approves inside cwd

```bash
python3 cli.py --auto-mode -p "Create a file called scratch_test.txt containing the text hello"
```

Expect: a dim note `✓ auto-approved (auto-mode): write_file →
.../scratch_test.txt`, no prompt, file written.

## 4. `--auto-mode` — falls back to a prompt outside cwd

```bash
python3 cli.py --auto-mode -p "Create a file at /tmp/scratch_test_outside.txt containing the text hello"
```

Expect: the real interactive menu appears (identical to approval-mode's),
since `/tmp/...` resolves outside the launch directory.

## 5. `-p` exits after exactly one turn

```bash
time python3 cli.py --yolo-mode -p "What is 2 + 2?"
echo "exit code: $?"
```

Expect: prints the answer, then returns straight to the shell — no `>` REPL
prompt ever appears, `time` shows roughly one model round-trip, exit code
`0`.

## Also worth re-checking after any change here

- The cancellation-message fix: in approval or auto-mode, choose **No** on a
  write/delete outside cwd and confirm the AI's final reply says the action
  was cancelled — not that it succeeded (see `core/agent.py`'s
  `SYSTEM_PROMPT` and the `CANCELLED` `ToolMessage` in `ui/turn.py`).
- `--yolo-mode --auto-mode` together still exits non-zero with an argparse
  error, even with `-p` present: `python3 cli.py --yolo-mode --auto-mode -p "x"; echo $?` → exit code `2`.

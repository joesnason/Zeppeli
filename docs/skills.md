# Skills

English | [繁體中文](skills.zh-TW.md)

A **skill** is a folder of task-specific instructions (plus any supporting
files, e.g. scripts) that the model can pull in on demand. At session start
Zeppeli lists every available skill's name and description in the system
prompt; when a request matches one, the model reads the full instructions
with `read_file(path="skill://<name>")`. All logic lives in
`core/skills.py` (no UI dependencies).

## Where skills live

| Level | Directory |
|---|---|
| User | `~/.zeppeli/skills/<skill-name>/SKILL.md` |
| Project | `<launch directory>/.zeppeli/skills/<skill-name>/SKILL.md` |

- The **folder name is the skill name** — a different `name:` in the
  frontmatter is ignored for lookup purposes.
- Both levels are merged; on a name clash the **project level wins**.
- A folder without a `SKILL.md` (or one that can't be read) is skipped.
- For the Slack bot, the project level is `<allowed_dir>/.zeppeli/skills/`.

## `SKILL.md` format

```markdown
---
name: mail-request-checker
description: "What the skill does and when to use it. Use when: ..."
disable-model-invocation: false   # optional
---

# Full instructions (Markdown)
...run `python3 ./scripts/validate_mail.py <dir>`...
```

| Field | Meaning |
|---|---|
| `name` | Skill name (informational; the folder name is authoritative). |
| `description` | What the skill is for and when to use it — this is the only part the model sees up front, so make it specific. Multi-line values are collapsed to one line. |
| `disable-model-invocation` | `true` hides the skill entirely: not listed in the system prompt and not readable via `skill://`. **Default `false`** when omitted. |

The frontmatter is parsed with `yaml.safe_load` (`parse_frontmatter()`).
Missing, unterminated, or invalid frontmatter never raises — it's treated
as empty (the skill is still listed, with no description). Other keys
(e.g. `argument-hint`) are allowed and ignored.

## Lifecycle

- **Scanned once per session**: `ui/repl.py`'s `main()` calls
  `load_skills(initial_cwd)` right before building the `SystemMessage`
  (both the interactive REPL and one-shot `-p`); `slack_bot.py` calls it
  once at startup, and every new thread's `SystemMessage` uses that same
  list (`slack_bot/sessions.py`). Adding or editing a skill takes effect on
  the next launch.
- `build_skills_prompt()` appends an "Available skills" section (one
  `- <name>: <description>` line per skill, plus how to use `skill://`)
  after `SYSTEM_PROMPT` and before the `Working directory:` line. With no
  skills it returns `""`, so the prompt is byte-identical to before.
- The discovered skills are kept in a module-level registry
  (`_active_skills`) because `read_file()` is a plain `@tool` with no
  session context.

## `skill://` URIs

Only `read_file` understands them:

| URI | Reads |
|---|---|
| `skill://<name>` (or `skill://<name>/`) | `<skill folder>/SKILL.md` |
| `skill://<name>/<relative path>` | `<skill folder>/<relative path>`, e.g. `skill://mail-request-checker/scripts/validate_mail.py` |

- `core/tools.py`'s `resolve_paths()` leaves `skill://` values untouched
  (otherwise `pathlib` would collapse them into `<cwd>/skill:/<name>`);
  `read_file()` resolves them via `resolve_skill_uri()` and then uses its
  normal paginated read (`offset`/`limit`/`max_bytes` all still apply).
- The header shows both the URI and the real folder:
  `[File: skill://<name> | skill dir: /abs/path | lines 1–42 | ... bytes]`.
  The model uses that absolute path when a skill tells it to run one of
  its scripts with `run_bash`.
- Errors come back as normal `[read_file] Error: ...` strings: unknown or
  disabled skill (the message lists available skills), a path escaping the
  skill folder (e.g. `skill://a/../b/SKILL.md` — checked after
  `Path.resolve()`), or a missing file.

## Security notes

- A skill's text is instructions the model will follow. Only install
  skills you trust — especially project-level ones in a repo you didn't
  write.
- Running a skill's script goes through `run_bash`'s normal hook: a
  user-level skill lives outside the workspace, so its scripts prompt for
  approval in approval/auto mode (yolo mode never asks). The Slack bot has
  no `run_bash` at all.

## Testing

`tests/test_skills.py` covers frontmatter parsing, discovery (merge,
project-over-user precedence, folder-name-as-name, missing `SKILL.md`,
`disable-model-invocation`), the prompt section, `skill://` resolution
(default `SKILL.md`, sub-paths, `..` escape, unknown skill), `read_file`/
`resolve_paths` integration, and `ui.repl.main()` putting the section into
the system message — no Ollama or network dependency. The shared
`tmp_zeppeli_dirs` fixture also redirects `USER_SKILLS_DIR`, so a real
`~/.zeppeli/skills` never leaks into tests.

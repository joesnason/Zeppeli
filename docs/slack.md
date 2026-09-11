# Slack bot integration

`slack_bot.py` (repo root) is a second, independent entry point alongside
`cli.py` — a long-running Socket Mode process that lets people trigger
the same AI agent (same tools, same `core/` model logic) from Slack
messages instead of a terminal. See "Architecture" in the project
`CLAUDE.md` for how `slack_bot/` fits alongside `core/`/`ui/`.

## Creating the Slack app and enabling Socket Mode

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New
   App** → **From scratch**. Pick a name and workspace.
2. **Socket Mode** (left sidebar) → toggle it on. This generates the
   **App-Level Token** (`xapp-...`) — save it for `config.json`'s
   `slack.app_token`. See "Required permissions" below for the scope it
   needs.
3. **OAuth & Permissions** (left sidebar) → add every scope listed under
   "Required permissions" below, in the **Bot Token Scopes** section.
4. **Event Subscriptions** (left sidebar) → toggle on → add every event
   listed under "Required permissions" below, in **Subscribe to bot
   events**.
5. **Install App** (top of OAuth & Permissions, or the sidebar's Install
   App page) → install to your workspace. This mints the **Bot User
   Token** (`xoxb-...`) — save it for `config.json`'s `slack.bot_token`.
6. Invite the bot to your test channel: `/invite @YourBotName`.

## Required permissions

Everything the bot needs, in one place — cross-reference this whenever
something isn't working, since a missing scope/event usually fails
**silently** (the event or API call never happens; no error appears
anywhere in `slack_bot.py`'s own output).

**App-Level Token scope** (Socket Mode page — this token, not the Bot
Token, is what Socket Mode itself authenticates with):

| Scope | Why |
|---|---|
| `connections:write` | Required for the Socket Mode WebSocket connection itself. |

**Bot Token Scopes** (OAuth & Permissions page):

| Scope | Why |
|---|---|
| `app_mentions:read` | Receive `@mention` events. |
| `chat:write` | Post and edit the bot's replies. |
| `channels:history` | Read messages/replies in **public** channels the bot is in — without it, thread replies never reach the bot even if the `message.channels` event below is subscribed. |
| `groups:history` | Same, for **private** channels — only needed if you'll use the bot there. |
| `files:read` | Download files people attach to a message (see "File attachments" below). |

**Event Subscriptions** (Event Subscriptions page → **Subscribe to bot
events**):

| Event | Why |
|---|---|
| `app_mention` | Deliver `@mention` events — starts/continues a thread. |
| `message.channels` | Deliver messages (including thread replies) in **public** channels — required for "reply in the same thread" to work at all. |
| `message.groups` | Same, for **private** channels — only needed if you'll use the bot there. |

**⚠️ After changing any scope or event subscription, you must
reinstall the app to the workspace** (Install App page → reinstall).
Slack does not apply the change to a running installation otherwise —
the bot token keeps behaving exactly as before until you do this. This
is the single most common cause of "I added the permission and it still
doesn't work": the scope/event exists in the app's *configuration* but
the *installed* app in your workspace hasn't been updated to match yet.
Restart `python3 slack_bot.py` afterward too, for a clean reconnect.

## `config.json` schema

Copy `config.json.example` to `config.json` (git-ignored) and fill in
the `"slack"` block:

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

- `app_token`/`bot_token` — from steps 2/5 above, in that order — Slack
  hands you the App-Level Token *before* the Bot Token during setup
  (Socket Mode toggle, then Install App), so this field order matches
  the order you'll actually obtain them in. Both required;
  `slack_bot.py` exits with a clear message if either is missing.
  **Easy to mix up by hand** — both are opaque-looking strings — so
  `cli.py` warns at startup (not fatal) if `app_token` doesn't start
  with `xapp-` or `bot_token` doesn't start with `xoxb-`, since a swap
  otherwise only surfaces later as a confusing Slack API error
  (`not_allowed_token_type` on `apps.connections.open`, or a `None` bot
  user id from `auth.test()` — hit for real once already).
- `allowed_dir` — the auto-mode containment boundary (see "Permission
  model" below). Required.
- `allowed_users` — optional. See "Access control" below.

`model`/`base_url`/`api_key` are the same top-level keys `cli.py` already
reads (see [`docs/models.md`](models.md)) — one model config serves every
Slack thread, loaded once at startup.

## Access control

`allowed_users` is a list of Slack user IDs. **Non-empty**: only those
users' messages trigger the bot; everyone else is silently ignored (no
reply, no error visible to them). **Empty or omitted**: anyone who can
post in a channel the bot is in can trigger it.

Start with just your own ID for testing, then remove the list (or empty
it) later to open the bot up to the whole channel — that's the intended
workflow, not a one-way door.

To find a Slack user ID: click a person's profile in Slack → **More** →
**Copy member ID** (looks like `U0123ABCXYZ`).

## Thread = session model

Each Slack **thread** is one independent conversation:

- `@mention`ing the bot outside a thread it already owns starts a **new**
  thread (Slack turns that mention's own `ts` into the new thread's
  `thread_ts`) and a fresh conversation with no prior context.
- Replying inside a thread the bot already responded in continues that
  same conversation.
- A plain channel message that neither mentions the bot nor replies in
  one of its threads is ignored entirely — the bot never responds to
  general channel chatter.

Conversational context lives only in memory for the life of the
`slack_bot.py` process — restarting it clears every thread's context.
The `core/sessions.py`/`core/eventlog.py` records of what happened
still land in `~/.zeppeli/sessions/`/`~/.zeppeli/logs/` exactly like the
terminal REPL, though — only the live in-memory conversation is lost,
not the historical record.

## Reply language

The bot always replies in Traditional Chinese (繁體中文), regardless of
what language the user's message is written in. This is Slack-specific:
`slack_bot/sessions.py`'s `ThreadRegistry.get_or_create()` appends a
`_SLACK_LANGUAGE_INSTRUCTION` string to `core/agent.py`'s shared
`SYSTEM_PROMPT` when it builds a new thread's initial `SystemMessage` —
the terminal REPL/`-p` mode use `SYSTEM_PROMPT` unmodified and stay
language-neutral. Not currently configurable per-workspace/thread; change
`_SLACK_LANGUAGE_INSTRUCTION` directly if you need different wording or
language.

## File attachments

Attaching a file to a message (e.g. a log file) lets the bot analyze it
alongside your prompt. A file is supported if Slack reports it as a
`text/*` mimetype, **or** its filename has a well-known text extension
(`.log`, `.txt`, `.csv`, `.json`, `.md`, `.yml`/`.yaml`, `.ini`/`.conf`/
`.cfg`, `.env`, `.xml`) — the extension check exists because Slack often
reports a `.log` file's mimetype as the generic `application/octet-stream`
rather than `text/plain` in practice, which a mimetype-only check would
wrongly reject. Images, archives, and other binaries are refused with a
one-line note in the thread, not silently dropped.

Rather than dumping the whole file into the prompt, the bot **saves it**
to `<allowed_dir>/.slack_attachments/` (named `<slack-file-id>_<filename>`
to avoid collisions) and gives the model the file's path, its total line
count, and a preview of its **last ~200 lines** — a log's most recent
lines are usually the relevant ones. If the model needs earlier content,
it uses its own existing `read_file`/`rg_search` tools on that saved
path to page through the rest — no special "attachment" tool exists;
this is exactly the same file-reading path any other file on disk goes
through.

A message can carry up to 3 attachments; anything beyond that is
ignored. A file over 50 MB, or one that fails to download, gets the same
one-line refusal in the thread as an unsupported type.

## Permission model — auto-mode only, no interactive approval

The bot always runs in the equivalent of `--auto-mode`, scoped to
`allowed_dir`:

- A `write_file`/`delete_file` call **inside** `allowed_dir` auto-approves
  with **zero** confirmation — same as terminal `--auto-mode`.
- A call **outside** `allowed_dir` is always **automatically refused**,
  with a one-line explanation posted to the thread. There is no
  Slack-based interactive approval (no button/reaction flow) — this was
  a deliberate scope decision, not a missing feature; unlike terminal
  `--auto-mode`, an out-of-bounds write here never falls back to a
  prompt (there's no way to show a Yes/No menu over Slack), so it's
  refused outright instead of hanging.

**⚠️ Security consideration**: because in-bounds writes/deletes need zero
confirmation, anyone on `allowed_users` — or anyone who can post in a
channel the bot is in, if that list is left empty — can direct real
file writes/deletes anywhere inside `allowed_dir`. Recommendations:
- Point `allowed_dir` at a disposable/sandboxed directory, **never** a
  home directory or a repo containing secrets.
- Keep `allowed_users` non-empty except for solo testing.

## Running it

```bash
python3 slack_bot.py
```

No CLI flags — purely `config.json`-driven. Runs until interrupted
(Ctrl+C). Prints a one-line "connected" message on successful startup,
including the resolved bot user ID and `allowed_dir`.

## Known limitations (v1)

- **Unbounded in-memory thread registry** — no eviction/TTL. A very
  long-running process will accumulate one `ThreadSession` per thread
  ever used, for as long as the process stays up. A periodic restart
  (or a future TTL sweep) is the mitigation for now.
- **No image support** — only `text/*` attachments are handled (see
  "File attachments" above); Slack image uploads aren't wired into
  `core/images.py`'s `@path`/`--image` vision pipeline.
- **`.slack_attachments/` never gets cleaned up** — every downloaded
  attachment accumulates on disk indefinitely, same "no eviction in v1"
  spirit as the thread registry above. Periodically clear it out by hand
  if disk usage becomes a concern.
- **No interactive approval** — by design (see "Permission model"
  above), not a bug.

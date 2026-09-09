"""Slack event wiring: registers `app_mention` and `message` handlers on a
Bolt `AsyncApp`-shaped object (duck-typed — only needs an `.event(name)`
decorator, so this module never imports `slack_bolt` itself and stays
testable with a plain stub app).

Trigger rule (per the approved plan): only two kinds of events ever
dispatch a turn —
  - `app_mention`: starts a new thread/session if not already in one
    (Slack sets the mention's own `ts` as the new `thread_ts`), or
    continues an existing one if the mention was itself posted inside a
    thread the bot owns.
  - `message` whose `thread_ts` matches a thread the bot already owns
    (i.e. someone replying in a thread the bot previously responded in).
Everything else — plain channel messages, DMs, edits/joins (`subtype`
set), the bot's own messages (`bot_id` set) — is ignored. An
access-control check (slack_bot/access.py) additionally gates both paths.
"""

import io
import re

from rich.console import Console

from .access import is_allowed
from .attachments import MAX_ATTACHMENTS_PER_MESSAGE, AttachmentError, attachments_dir, process_attachment
from .live import SlackLive
from .runner import run_and_persist

_MENTION_RE = re.compile(r"^\s*<@([A-Za-z0-9]+)>\s*")


def _strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove a leading `<@BOTID>` token (Slack's mention markup) from the
    start of an app_mention event's text, if present."""
    m = _MENTION_RE.match(text or "")
    if m and m.group(1) == bot_user_id:
        return text[m.end():]
    return text or ""


def register_handlers(app, *, llm_with_tools, initial_cwd: str, allowed_users,
                       registry, bot_user_id: str, model_name, mode: str,
                       bot_token: str, http_session,
                       context_window: int | None = None):
    """Registers the two handlers on `app` and also returns them directly
    (as `(handle_app_mention, handle_message)`) so tests can invoke them
    without a real Bolt app/event dispatch loop."""

    async def _process_attachments(client, channel: str, thread_ts: str, files: list[dict]) -> str:
        """Downloads/saves each supported attachment (see
        slack_bot/attachments.py), returning the notes to fold into the
        turn's text. An unsupported/oversized/failed file posts its own
        one-line explanation directly to the thread instead — one bad
        attachment never blocks the rest of the message or the other
        attachments."""
        notes = []
        dest_dir = attachments_dir(initial_cwd)
        for file_info in files[:MAX_ATTACHMENTS_PER_MESSAGE]:
            try:
                notes.append(await process_attachment(
                    file_info, bot_token=bot_token, session=http_session, dest_dir=dest_dir,
                ))
            except AttachmentError as e:
                await client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts,
                    text=f':warning: Couldn\'t use attachment "{file_info.get("name", "?")}": {e}',
                )
        return "\n\n".join(notes)

    async def _dispatch(event, client, *, thread_ts: str, text: str) -> None:
        user = event.get("user")
        if user is None or user == bot_user_id or not is_allowed(user, allowed_users):
            return
        files = event.get("files") or []
        if files:
            attachment_notes = await _process_attachments(client, event["channel"], thread_ts, files)
            if attachment_notes:
                text = f"{text}\n\n{attachment_notes}" if text else attachment_notes
        thread_session = await registry.get_or_create(thread_ts, initial_cwd, model_name)
        async with thread_session.lock:
            live = SlackLive(client, event["channel"], thread_ts)
            console = Console(file=io.StringIO())
            await run_and_persist(llm_with_tools, thread_session, text, console, live,
                                   initial_cwd, mode, context_window)

    @app.event("app_mention")
    async def handle_app_mention(event, client):
        thread_ts = event.get("thread_ts") or event["ts"]
        text = _strip_bot_mention(event.get("text"), bot_user_id)
        await _dispatch(event, client, thread_ts=thread_ts, text=text)

    @app.event("message")
    async def handle_message(event, client):
        if event.get("subtype") is not None or event.get("bot_id") is not None:
            return  # edits/joins/bot posts (including our own) — never a fresh user turn
        thread_ts = event.get("thread_ts")
        if thread_ts is None or thread_ts not in registry.known_thread_ts():
            return  # not a reply inside a thread the bot already owns
        await _dispatch(event, client, thread_ts=thread_ts, text=event.get("text") or "")

    return handle_app_mention, handle_message

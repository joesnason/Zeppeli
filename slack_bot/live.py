"""SlackLive: the Slack-backed implementation of the `live` contract that
`ui/turn.py`'s `run_turn()` / `ui/streaming.py`'s `stream_response()` /
`ui/permissions.py`'s `permission_ask()` already call against (satisfied,
for the terminal, by `ui/live_region.py`'s `LiveRegion`/`SimpleLive`).
Neither of those is reusable here (both ultimately need a real TTY for
their menu), so this is the one genuinely new adapter this integration
needs — none of `ui/turn.py`/`ui/streaming.py`/`ui/permissions.py` change.

Contract (all five called by name, duck-typed, no shared base class):
    start_spinner(label: str = "Thinking...") -> None
    stop_spinner() -> None
    update_markdown(markdown_text: str) -> None
    finalize_markdown(markdown_text: str) -> None
    async ask_menu(options: list[str], default_idx: int = 0) -> int | None

The first four are called *synchronously* (never awaited) from inside
`ui/streaming.py`'s `_consume_stream()`, which itself runs on the bot's
asyncio event loop — so the only way to make an actual Slack API call
from them is to schedule a task (`asyncio.create_task`), never to block.
`ask_menu()` alone is `async def` and is awaited directly.

One instance is constructed per Slack thread reply (see
slack_bot/handlers.py) and is not reused across turns.
"""

import asyncio
import sys
import time


class SlackLive:
    def __init__(self, client, channel: str, thread_ts: str, throttle_seconds: float = 1.5):
        self._client = client
        self._channel = channel
        self._thread_ts = thread_ts
        self._throttle_seconds = throttle_seconds

        # `_message_ts` is the Slack timestamp of the reply bubble
        # currently being edited for the *current* hop. `run_turn()` may
        # run multiple hops per hit (model call -> tool call -> model
        # call again); finalize_markdown() resets this to None so the
        # next hop starts a fresh bubble instead of continuing to edit
        # one that's already been promoted to "final" — mirroring the
        # terminal UI's own per-hop "promote live window to permanent
        # scrollback, then clear it" behavior (see ui/live_region.py).
        self._message_ts: str | None = None
        self._latest_text = ""
        # None sentinel = "never sent yet" -> first call always goes
        # through unthrottled. Deliberately not 0.0 — time.monotonic()
        # can legitimately equal 0.0 (e.g. a fake clock in tests), and
        # 0.0 is falsy, which would silently defeat an `if self._last_send_time:` check.
        self._last_send_time: float | None = None

        # Serializes actual Slack API calls so scheduled update_markdown()
        # sends and a later finalize_markdown()/ask_menu() send can never
        # land out of order, even though update_markdown() schedules its
        # send as a fire-and-forget task rather than awaiting it directly.
        self._send_lock = asyncio.Lock()
        self._pending_task: asyncio.Task | None = None

    # -- the five `live` contract methods -----------------------------

    def start_spinner(self, label: str = "Thinking...") -> None:
        self.update_markdown(f"_{label}_")

    def stop_spinner(self) -> None:
        pass  # next update_markdown()/finalize_markdown() call replaces the placeholder

    def update_markdown(self, markdown_text: str) -> None:
        # Always keep the buffer current, even when throttled — this is
        # what makes finalize_markdown() correct regardless of how many
        # prior calls were suppressed.
        self._latest_text = markdown_text
        now = time.monotonic()
        if self._last_send_time is not None and now - self._last_send_time < self._throttle_seconds:
            return  # buffered no-op: too soon since the last real Slack API call
        self._last_send_time = now
        self._pending_task = asyncio.create_task(self._send(markdown_text))

    def finalize_markdown(self, markdown_text: str) -> None:
        # Unconditionally sends the true final text, ignoring the
        # throttle window entirely, and marks the bubble finished so the
        # next hop (if any) starts a new one — reset synchronously here
        # (not inside _send()) so a hop that follows immediately isn't
        # throttled by *this* bubble's send timing before _send() even
        # runs (it's scheduled as a fire-and-forget task, not awaited).
        self._latest_text = markdown_text
        self._pending_task = asyncio.create_task(self._send(markdown_text, finish_bubble=True))
        self._last_send_time = None

    async def ask_menu(self, options: list[str], default_idx: int = 0) -> int | None:
        """Always denies (decision: no real Slack-interactive approval —
        see docs/slack.md). ui/permissions.py's MODE_AUTO hook only calls
        this for a path OUTSIDE the sanctioned `allowed_dir`, so this is
        the rare/exceptional path, not the common one. Posts a standalone
        explanatory line (not an edit of the streaming bubble) so the
        refusal is visible rather than silent."""
        async with self._send_lock:
            await self._safe_post(
                ":no_entry: A tool wanted to write to or delete a file "
                "outside the sanctioned directory. Slack mode can't ask "
                "for interactive approval, so this was automatically "
                "refused — nothing was changed."
            )
        # "No" is always the last option, by convention (mirrors
        # ui/permissions.py's _arrow_menu()'s Escape-key behavior).
        return len(options) - 1

    # -- extra methods, not part of the standard `live` contract --------

    async def post_error(self, text: str) -> None:
        """Post a standalone error line to the thread — used by
        slack_bot/runner.py when a run fails outright (e.g. the model
        call itself raised), since there's no terminal to fall back on
        the way ui/streaming.py's stream_response() has via `console`."""
        async with self._send_lock:
            await self._safe_post(f":warning: {text}")

    async def flush(self) -> None:
        """Await the most recently scheduled send so the true final text
        is guaranteed to have landed in Slack before the caller proceeds.
        Only slack_bot/runner.py calls this, once, right after run_turn()
        returns — never passed into run_turn()/stream_response()/
        permission_ask() themselves, which know nothing about it."""
        if self._pending_task is not None:
            await self._pending_task

    # -- internals -------------------------------------------------------

    async def _send(self, text: str, finish_bubble: bool = False) -> None:
        async with self._send_lock:
            if self._message_ts is None:
                ts = await self._safe_post(text)
                if ts is not None:
                    self._message_ts = ts
            else:
                await self._safe_update(text)
            if finish_bubble:
                self._message_ts = None

    async def _safe_post(self, text: str) -> str | None:
        try:
            resp = await self._client.chat_postMessage(
                channel=self._channel, thread_ts=self._thread_ts, text=text,
            )
            return resp["ts"]
        except Exception as e:
            # A transient Slack API failure shouldn't take down the bot's
            # event loop or connection — log and move on.
            print(f"warning: Slack chat.postMessage failed: {e}", file=sys.stderr)
            return None

    async def _safe_update(self, text: str) -> None:
        try:
            await self._client.chat_update(channel=self._channel, ts=self._message_ts, text=text)
        except Exception as e:
            print(f"warning: Slack chat.update failed: {e}", file=sys.stderr)

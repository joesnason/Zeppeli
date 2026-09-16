"""Bounded-retry supervisor for slack_bolt's Socket Mode reconnect loop.

slack_bolt/slack_sdk retry a dropped Socket Mode connection forever,
spaced `ping_interval` seconds apart, with no backoff and no built-in
give-up mechanism — confirmed by reading the installed slack_sdk==3.44.1 /
slack_bolt==1.30.0 source directly (`.venv/lib/python3.12/site-packages/`):
neither `SocketModeClient.connect()` nor `.monitor_current_session()`
(`slack_sdk/socket_mode/aiohttp/__init__.py`) ever count failures or stop
retrying, and there's no `max_retries`/give-up parameter anywhere in either
package. There's also no `on_connect`/`on_reconnect` callback hook to
learn "did this attempt actually succeed" from directly.

This module adds that missing piece from the outside, in two parts:

- `ReconnectFailureCounter` — a logging.Handler that counts consecutive
  reconnect-failure log records (matched by message text — the only
  failure signal the library exposes at all) and calls a callback once a
  threshold is hit.
- `watch_for_reconnect()` — a background poller that resets the counter
  whenever `SocketModeClient.current_session` changes identity. slack_sdk
  itself only reassigns that attribute after a successful `ws_connect()`
  (`aiohttp/__init__.py`'s `connect()`, ~line 377) — it's the library's own
  internal "this attempt actually succeeded" signal, just never exposed as
  a callback — so this is a real success check, not a guess or a timer.

Together: the counter only climbs on a genuine failure log and is only
ever reset on a genuine successful (re)connect — not a blind lifetime
failure count, and not a pure time-based heuristic.

Caveat: the failure-matching in `ReconnectFailureCounter` depends on
slack_sdk's current log message wording (`_FAILURE_MARKERS` below) — a
future slack_sdk release could reword these and silently stop being
detected, since there is no more stable API to hook into.
"""

import asyncio
import logging

# The two distinct reconnect-failure log call sites in slack_sdk==3.44.1's
# SocketModeClient (slack_sdk/socket_mode/aiohttp/__init__.py):
#   - connect(), ~line 412: logger.exception(f"Failed to connect (error: {e}); Retrying...")
#     — fires for the *initial* connection, before any session exists.
#   - monitor_current_session(), ~line 205: logger.error(f"Failed to check the current
#     session ({session_id}) or reconnect to the server (error: ..., message: {e})")
#     — fires for reconnects after a session was already established (this is the
#     message reported in the original bug: "Failed to check the current session
#     (s_XXXXXXXX) or reconnect to the server...").
_FAILURE_MARKERS = ("Failed to check the current session", "Failed to connect")


class ReconnectFailureCounter(logging.Handler):
    """Counts consecutive Socket Mode reconnect-failure log records and
    calls `on_give_up()` once `max_failures` is reached. Only ever counts
    up — pair with `watch_for_reconnect()` to reset it on a real success.
    """

    def __init__(self, max_failures: int, on_give_up):
        super().__init__(level=logging.ERROR)
        self.count = 0
        self._max_failures = max_failures
        self._on_give_up = on_give_up

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if any(marker in message for marker in _FAILURE_MARKERS):
            self.count += 1
            if self.count >= self._max_failures:
                self._on_give_up()

    def reset(self) -> None:
        self.count = 0


async def watch_for_reconnect(client, counter: ReconnectFailureCounter, poll_interval: float = 2.0) -> None:
    """Resets `counter` to 0 whenever `client.current_session` changes
    identity — proof of a genuine successful (re)connect (see this
    module's docstring). Runs until `client.closed` is set."""
    last_session = client.current_session
    while not client.closed:
        await asyncio.sleep(poll_interval)
        if client.current_session is not last_session:
            last_session = client.current_session
            counter.reset()

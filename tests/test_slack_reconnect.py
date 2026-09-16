"""Automated tests for slack_bot/reconnect.py — the bounded-retry
supervisor for slack_bolt's Socket Mode reconnect loop (ReconnectFailureCounter,
watch_for_reconnect()). No real Slack/network dependency: ReconnectFailureCounter
is exercised via a plain logging.Logger, and watch_for_reconnect() via a fake
client object (SimpleNamespace). Exits non-zero on failure.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from slack_bot.reconnect import ReconnectFailureCounter, watch_for_reconnect


def _make_logger(name: str, counter: ReconnectFailureCounter) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(counter)
    logger.setLevel(logging.ERROR)
    return logger


# --- ReconnectFailureCounter -------------------------------------------------

def test_counter_increments_on_matching_failure_message():
    counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
    logger = _make_logger("test.reconnect.increments", counter)
    logger.error("Failed to check the current session (s_abc) or reconnect to the server (error: X, message: Y)")
    assert counter.count == 1


def test_counter_ignores_non_matching_message():
    counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
    logger = _make_logger("test.reconnect.ignores", counter)
    logger.error("some unrelated error")
    assert counter.count == 0


def test_counter_matches_initial_connect_failure_message_too():
    counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
    logger = _make_logger("test.reconnect.initial", counter)
    logger.exception("Failed to connect (error: ClientConnectorDNSError); Retrying...")
    assert counter.count == 1


def test_counter_ignores_below_error_level():
    counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
    logger = _make_logger("test.reconnect.level", counter)
    logger.setLevel(logging.DEBUG)
    logger.warning("Failed to check the current session (s_abc) or reconnect to the server")
    assert counter.count == 0


def test_on_give_up_fires_exactly_at_threshold_not_before():
    calls = []
    counter = ReconnectFailureCounter(max_failures=3, on_give_up=lambda: calls.append(True))
    logger = _make_logger("test.reconnect.threshold", counter)
    for _ in range(2):
        logger.error("Failed to check the current session (s_x) or reconnect to the server")
    assert calls == []
    logger.error("Failed to check the current session (s_x) or reconnect to the server")
    assert calls == [True]


def test_on_give_up_fires_again_on_further_failures_past_threshold():
    # emit() doesn't gate re-firing once past threshold — count keeps
    # climbing and on_give_up is called every time count >= max_failures.
    calls = []
    counter = ReconnectFailureCounter(max_failures=1, on_give_up=lambda: calls.append(True))
    logger = _make_logger("test.reconnect.refire", counter)
    logger.error("Failed to connect (error: X); Retrying...")
    logger.error("Failed to connect (error: X); Retrying...")
    assert calls == [True, True]


def test_reset_zeroes_the_count():
    counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
    logger = _make_logger("test.reconnect.reset", counter)
    logger.error("Failed to check the current session (s_x) or reconnect to the server")
    logger.error("Failed to check the current session (s_x) or reconnect to the server")
    assert counter.count == 2
    counter.reset()
    assert counter.count == 0


# --- watch_for_reconnect() ---------------------------------------------------

def test_watch_for_reconnect_resets_counter_on_new_session():
    async def _run():
        counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
        counter.count = 5
        client = SimpleNamespace(current_session=object(), closed=False)
        task = asyncio.create_task(watch_for_reconnect(client, counter, poll_interval=0.01))
        await asyncio.sleep(0.03)
        assert counter.count == 5  # no change yet — session hasn't changed

        client.current_session = object()  # simulate a fresh successful (re)connect
        await asyncio.sleep(0.03)
        assert counter.count == 0

        client.closed = True
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(_run())


def test_watch_for_reconnect_exits_when_client_closed():
    async def _run():
        counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
        client = SimpleNamespace(current_session=object(), closed=True)
        # closed is already True — the loop should exit essentially immediately.
        await asyncio.wait_for(watch_for_reconnect(client, counter, poll_interval=0.01), timeout=1)

    asyncio.run(_run())


def test_watch_for_reconnect_does_not_reset_when_session_object_unchanged():
    async def _run():
        counter = ReconnectFailureCounter(max_failures=10, on_give_up=lambda: None)
        counter.count = 3
        same_session = object()
        client = SimpleNamespace(current_session=same_session, closed=False)
        task = asyncio.create_task(watch_for_reconnect(client, counter, poll_interval=0.01))
        await asyncio.sleep(0.05)
        assert counter.count == 3  # identical object each poll — never "changed"
        client.closed = True
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(_run())

"""Automated tests for the slack_bot/ package: access-control allowlist
logic, SlackLive's throttled/ordered Slack updates, ThreadRegistry's
get-or-create + locking behavior, and handlers.py's event-dispatch rules.
No live Slack/network dependency anywhere — a fake async Slack client and
a fake Bolt-`App`-shaped stub stand in for real slack_bolt/slack_sdk
objects. Style matches test_streaming.py/test_permission_modes.py.
"""

import asyncio

import slack_bot.attachments as attachments_module
import slack_bot.live as live_module
from slack_bot.access import is_allowed
from slack_bot.attachments import AttachmentError, _is_supported, _sanitize_filename, process_attachment
from slack_bot.handlers import _strip_bot_mention, register_handlers
from slack_bot.live import SlackLive
from slack_bot.sessions import ThreadSession, ThreadRegistry


def _run(coro):
    return asyncio.run(coro)


class _FakeSlackClient:
    """Records every chat.postMessage/chat.update call; mints a new `ts`
    per post so SlackLive's post-vs-update branching is observable."""

    def __init__(self):
        self.calls = []
        self._next_ts = 1

    async def chat_postMessage(self, channel, text, thread_ts=None):
        ts = f"ts{self._next_ts}"
        self._next_ts += 1
        self.calls.append(("post", channel, thread_ts, ts, text))
        return {"ts": ts}

    async def chat_update(self, channel, ts, text):
        self.calls.append(("update", channel, None, ts, text))


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeHTTPSession:
    """Mirrors just enough of aiohttp.ClientSession's shape for
    process_attachment(): `.get(url, headers=...)` returns an async
    context manager (not a coroutine — matches real aiohttp usage)."""

    def __init__(self, status: int = 200, body: bytes = b""):
        self.status = status
        self.body = body
        self.calls = []  # list of (url, headers)

    def get(self, url, headers=None):
        self.calls.append((url, headers))
        return _FakeHTTPResponse(self.status, self.body)


# --- slack_bot/access.py ----------------------------------------------------

def test_is_allowed_empty_list_allows_anyone():
    assert is_allowed("U123", []) is True
    assert is_allowed("U123", None) is True


def test_is_allowed_nonempty_list_restricts_to_members():
    assert is_allowed("U123", ["U123", "U456"]) is True
    assert is_allowed("U999", ["U123", "U456"]) is False


# --- slack_bot/live.py (SlackLive) ------------------------------------------

def test_slack_live_throttles_repeat_updates_but_keeps_latest_text(monkeypatch):
    fake_time = {"t": 0.0}
    monkeypatch.setattr(live_module.time, "monotonic", lambda: fake_time["t"])
    client = _FakeSlackClient()
    sl = SlackLive(client, "C1", "T1", throttle_seconds=1.5)

    async def body():
        sl.update_markdown("first")
        await sl._pending_task
        assert len(client.calls) == 1
        assert client.calls[0][0] == "post"

        sl.update_markdown("second")  # same fake time -> throttled, buffered only
        assert len(client.calls) == 1
        assert sl._latest_text == "second"

        fake_time["t"] = 2.0  # advance past the throttle window
        sl.update_markdown("third")
        await sl._pending_task
        assert len(client.calls) == 2
        assert client.calls[1][0] == "update"  # same message bubble, edited not reposted
        assert client.calls[1][4] == "third"

    _run(body())


def test_slack_live_finalize_sends_true_final_text_and_starts_new_bubble_next_hop(monkeypatch):
    fake_time = {"t": 0.0}
    monkeypatch.setattr(live_module.time, "monotonic", lambda: fake_time["t"])
    client = _FakeSlackClient()
    sl = SlackLive(client, "C1", "T1", throttle_seconds=1.5)

    async def body():
        sl.update_markdown("partial")
        await sl._pending_task
        sl.update_markdown("still throttled, buffered only")  # no fake_time advance -> throttled
        assert len(client.calls) == 1

        sl.finalize_markdown("the real final text")
        await sl.flush()
        assert len(client.calls) == 2
        assert client.calls[1] == ("update", "C1", None, "ts1", "the real final text")
        assert sl._message_ts is None  # bubble reset for the next hop

        # A second hop (e.g. after a tool call) posts a brand-new message,
        # not an edit of the finalized one.
        sl.update_markdown("hop 2")
        await sl._pending_task
        assert len(client.calls) == 3
        assert client.calls[2][0] == "post"
        assert client.calls[2][3] == "ts2"

    _run(body())


def test_slack_live_ask_menu_always_denies_and_posts_standalone_notice():
    client = _FakeSlackClient()
    sl = SlackLive(client, "C1", "T1")

    async def body():
        idx = await sl.ask_menu(["Yes", "Yes, always allow (this session)", "No"], default_idx=0)
        assert idx == 2
        assert len(client.calls) == 1
        assert client.calls[0][0] == "post"
        assert "automatically refused" in client.calls[0][4]
        # Must not touch the streaming bubble's message_ts.
        assert sl._message_ts is None

    _run(body())


def test_slack_live_post_error_posts_a_warning_line():
    client = _FakeSlackClient()
    sl = SlackLive(client, "C1", "T1")

    async def body():
        await sl.post_error("something went wrong")
        assert len(client.calls) == 1
        assert "something went wrong" in client.calls[0][4]

    _run(body())


# --- slack_bot/sessions.py (ThreadRegistry) ---------------------------------

def test_thread_registry_get_or_create_returns_same_session_for_same_thread(tmp_zeppeli_dirs):
    registry = ThreadRegistry()

    async def body():
        s1 = await registry.get_or_create("T1", "/tmp", "gemma4:e2b")
        s2 = await registry.get_or_create("T1", "/tmp", "gemma4:e2b")
        assert s1 is s2
        s3 = await registry.get_or_create("T2", "/tmp", "gemma4:e2b")
        assert s3 is not s1
        assert registry.known_thread_ts() == {"T1", "T2"}

    _run(body())


def test_thread_registry_concurrent_get_or_create_same_thread_creates_exactly_one(tmp_zeppeli_dirs):
    registry = ThreadRegistry()

    async def body():
        results = await asyncio.gather(*[
            registry.get_or_create("T1", "/tmp", "m") for _ in range(5)
        ])
        assert len({id(r) for r in results}) == 1

    _run(body())


def test_thread_session_lock_serializes_within_one_thread():
    session = ThreadSession(messages=[], session_id="s1", history_session=None)
    order = []

    async def task(name, delay):
        async with session.lock:
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    async def body():
        await asyncio.gather(task("a", 0.02), task("b", 0.0))

    _run(body())
    assert order == ["a-start", "a-end", "b-start", "b-end"]


def test_different_thread_sessions_dont_block_each_other():
    s1 = ThreadSession(messages=[], session_id="s1", history_session=None)
    s2 = ThreadSession(messages=[], session_id="s2", history_session=None)
    order = []

    async def task(name, session, delay):
        async with session.lock:
            order.append(f"{name}-start")
            await asyncio.sleep(delay)
            order.append(f"{name}-end")

    async def body():
        await asyncio.gather(task("a", s1, 0.05), task("b", s2, 0.0))

    _run(body())
    # b shares no lock with a, so it finishes first despite starting second.
    assert order.index("b-end") < order.index("a-end")


# --- slack_bot/handlers.py ---------------------------------------------------

class _FakeApp:
    def __init__(self):
        self.handlers = {}

    def event(self, name):
        def deco(fn):
            self.handlers[name] = fn
            return fn
        return deco


class _FakeRegistry:
    def __init__(self, known=None):
        self._known = set(known or ())
        self.get_or_create_calls = []

    def known_thread_ts(self):
        return self._known

    async def get_or_create(self, thread_ts, initial_cwd, model_name):
        self.get_or_create_calls.append(thread_ts)
        self._known.add(thread_ts)
        return ThreadSession(messages=[], session_id="s", history_session=None)


def _register(monkeypatch, registry, allowed_users=None, initial_cwd="/tmp",
              bot_token="xoxb-test", http_session=None):
    import slack_bot.handlers as handlers_module
    calls = []

    async def fake_run_and_persist(*a, **k):
        calls.append((a, k))

    monkeypatch.setattr(handlers_module, "run_and_persist", fake_run_and_persist)
    app = _FakeApp()
    handle_mention, handle_message = register_handlers(
        app, llm_with_tools=None, initial_cwd=initial_cwd, allowed_users=allowed_users,
        registry=registry, bot_user_id="UBOT1", model_name="m", mode="auto",
        bot_token=bot_token, http_session=http_session,
    )
    return handle_mention, handle_message, calls


def test_strip_bot_mention_removes_leading_mention_token():
    assert _strip_bot_mention("<@UBOT1> hello there", "UBOT1") == "hello there"
    assert _strip_bot_mention("hello, no mention here", "UBOT1") == "hello, no mention here"
    assert _strip_bot_mention(None, "UBOT1") == ""


def test_app_mention_from_disallowed_user_is_ignored(monkeypatch):
    registry = _FakeRegistry()
    handle_mention, _, calls = _register(monkeypatch, registry, allowed_users=["UALLOWED1"])
    event = {"user": "UOTHER1", "text": "<@UBOT1> hi", "ts": "111.1", "channel": "C1"}
    _run(handle_mention(event, client=object()))
    assert calls == []


def test_app_mention_from_bot_itself_is_ignored(monkeypatch):
    registry = _FakeRegistry()
    handle_mention, _, calls = _register(monkeypatch, registry, allowed_users=None)
    event = {"user": "UBOT1", "text": "<@UBOT1> hi", "ts": "111.1", "channel": "C1"}
    _run(handle_mention(event, client=object()))
    assert calls == []


def test_app_mention_from_allowed_user_starts_new_thread(monkeypatch):
    registry = _FakeRegistry()
    handle_mention, _, calls = _register(monkeypatch, registry, allowed_users=["UME1"])
    event = {"user": "UME1", "text": "<@UBOT1> hi there", "ts": "111.1", "channel": "C1"}
    _run(handle_mention(event, client=object()))
    assert len(calls) == 1
    assert registry.get_or_create_calls == ["111.1"]  # no thread_ts on event -> its own ts is the thread
    assert calls[0][0][2] == "hi there"  # user_input arg with the mention stripped


def test_message_with_no_matching_thread_is_ignored(monkeypatch):
    registry = _FakeRegistry(known=())
    _, handle_message, calls = _register(monkeypatch, registry, allowed_users=None)
    event = {"user": "UME1", "text": "hi", "thread_ts": "111.1", "channel": "C1"}
    _run(handle_message(event, client=object()))
    assert calls == []


def test_message_with_bot_id_is_ignored(monkeypatch):
    registry = _FakeRegistry(known={"111.1"})
    _, handle_message, calls = _register(monkeypatch, registry, allowed_users=None)
    event = {"user": None, "bot_id": "B1", "text": "hi", "thread_ts": "111.1", "channel": "C1"}
    _run(handle_message(event, client=object()))
    assert calls == []


def test_message_reply_in_known_thread_from_allowed_user_dispatches(monkeypatch):
    registry = _FakeRegistry(known={"111.1"})
    _, handle_message, calls = _register(monkeypatch, registry, allowed_users=["UME1"])
    event = {"user": "UME1", "text": "follow up", "thread_ts": "111.1", "channel": "C1"}
    _run(handle_message(event, client=object()))
    assert len(calls) == 1
    assert calls[0][0][2] == "follow up"


# --- slack_bot/attachments.py -----------------------------------------------

def test_is_supported_checks_text_mimetype_prefix():
    assert _is_supported({"mimetype": "text/plain"}) is True
    assert _is_supported({"mimetype": "text/csv"}) is True
    assert _is_supported({"mimetype": "image/png"}) is False
    assert _is_supported({}) is False


def test_is_supported_falls_back_to_known_text_extension():
    # Regression: Slack reports a .log file's mimetype as the generic
    # application/octet-stream rather than text/plain in practice, which
    # a mimetype-only check would wrongly reject.
    assert _is_supported({"mimetype": "application/octet-stream", "name": "build.log"}) is True
    assert _is_supported({"mimetype": "application/octet-stream", "name": "notes.txt"}) is True
    assert _is_supported({"mimetype": "application/octet-stream", "name": "app.exe"}) is False
    assert _is_supported({"mimetype": "application/octet-stream"}) is False  # no name to fall back on


def test_sanitize_filename_strips_path_traversal_and_unsafe_chars():
    assert _sanitize_filename("../../etc/passwd") == "passwd"
    assert _sanitize_filename("weird?<>name.txt") == "weird___name.txt"
    assert _sanitize_filename("") == "attachment"


def test_process_attachment_happy_path_saves_file_and_builds_note(tmp_path):
    session = _FakeHTTPSession(status=200, body=b"line1\nline2\nline3\n")
    file_info = {
        "id": "F123", "name": "error.log", "mimetype": "text/plain",
        "size": 18, "url_private_download": "https://files.slack.com/f123",
    }

    async def body():
        note = await process_attachment(
            file_info, bot_token="xoxb-test", session=session, dest_dir=tmp_path,
        )
        assert "3 lines total" in note
        assert "line3" in note
        saved = tmp_path / "F123_error.log"
        assert saved.exists()
        assert saved.read_text() == "line1\nline2\nline3\n"
        assert session.calls == [("https://files.slack.com/f123", {"Authorization": "Bearer xoxb-test"})]

    _run(body())


def test_process_attachment_rejects_unsupported_mimetype_without_network_call(tmp_path):
    session = _FakeHTTPSession(status=200, body=b"whatever")
    file_info = {"id": "F1", "name": "photo.png", "mimetype": "image/png"}

    async def body():
        try:
            await process_attachment(file_info, bot_token="t", session=session, dest_dir=tmp_path)
            assert False, "expected AttachmentError"
        except AttachmentError as e:
            assert "unsupported" in str(e)
        assert session.calls == []  # never even attempted a download

    _run(body())


def test_process_attachment_rejects_oversized_declared_size_without_network_call(tmp_path):
    session = _FakeHTTPSession(status=200, body=b"whatever")
    file_info = {
        "id": "F1", "name": "big.log", "mimetype": "text/plain",
        "size": attachments_module.MAX_DOWNLOAD_BYTES + 1,
        "url_private_download": "https://files.slack.com/big",
    }

    async def body():
        try:
            await process_attachment(file_info, bot_token="t", session=session, dest_dir=tmp_path)
            assert False, "expected AttachmentError"
        except AttachmentError as e:
            assert "too large" in str(e)
        assert session.calls == []

    _run(body())


def test_process_attachment_rejects_oversized_after_download_and_leaves_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(attachments_module, "MAX_DOWNLOAD_BYTES", 10)
    session = _FakeHTTPSession(status=200, body=b"this body is more than 10 bytes long")
    file_info = {
        "id": "F1", "name": "big.log", "mimetype": "text/plain",
        "url_private_download": "https://files.slack.com/big",  # no declared "size" -> only post-download check applies
    }

    async def body():
        try:
            await process_attachment(file_info, bot_token="t", session=session, dest_dir=tmp_path)
            assert False, "expected AttachmentError"
        except AttachmentError as e:
            assert "too large" in str(e)
        assert list(tmp_path.iterdir()) == []  # no partial file left behind

    _run(body())


def test_process_attachment_raises_on_non_200_status(tmp_path):
    session = _FakeHTTPSession(status=404, body=b"")
    file_info = {
        "id": "F1", "name": "gone.log", "mimetype": "text/plain",
        "url_private_download": "https://files.slack.com/gone",
    }

    async def body():
        try:
            await process_attachment(file_info, bot_token="t", session=session, dest_dir=tmp_path)
            assert False, "expected AttachmentError"
        except AttachmentError as e:
            assert "404" in str(e)

    _run(body())


# --- slack_bot/handlers.py: attachment integration --------------------------

def test_message_with_supported_attachment_folds_note_into_user_input(monkeypatch, tmp_path):
    import slack_bot.handlers as handlers_module

    async def fake_process_attachment(file_info, *, bot_token, session, dest_dir):
        return f"NOTE:{file_info['name']}"

    monkeypatch.setattr(handlers_module, "process_attachment", fake_process_attachment)
    registry = _FakeRegistry(known={"111.1"})
    _, handle_message, calls = _register(
        monkeypatch, registry, allowed_users=["UME1"], initial_cwd=str(tmp_path),
    )
    event = {
        "user": "UME1", "text": "check this out", "thread_ts": "111.1", "channel": "C1",
        "files": [{"id": "F1", "name": "error.log", "mimetype": "text/plain"}],
    }
    _run(handle_message(event, client=object()))
    assert len(calls) == 1
    assert calls[0][0][2] == "check this out\n\nNOTE:error.log"


def test_message_with_unsupported_attachment_posts_warning_and_still_dispatches(monkeypatch, tmp_path):
    import slack_bot.handlers as handlers_module

    async def fake_process_attachment(file_info, *, bot_token, session, dest_dir):
        raise AttachmentError("unsupported file type")

    monkeypatch.setattr(handlers_module, "process_attachment", fake_process_attachment)
    registry = _FakeRegistry(known={"111.1"})
    _, handle_message, calls = _register(
        monkeypatch, registry, allowed_users=["UME1"], initial_cwd=str(tmp_path),
    )
    client = _FakeSlackClient()
    event = {
        "user": "UME1", "text": "look at this", "thread_ts": "111.1", "channel": "C1",
        "files": [{"id": "F1", "name": "photo.png", "mimetype": "image/png"}],
    }
    _run(handle_message(event, client=client))
    assert len(calls) == 1
    assert calls[0][0][2] == "look at this"  # unchanged — no note to append
    assert len(client.calls) == 1
    assert client.calls[0][0] == "post"
    assert "photo.png" in client.calls[0][4]

"""Per-Slack-thread session state: one LangChain `messages` list + one
`core/sessions.py` `StoredSession` per thread, matching the decision that
each Slack thread is its own independent conversation (mirrors
`ui/repl.py`'s single per-process session, just keyed by thread instead
of by process).

Lifecycle: an unbounded in-memory dict for v1, no eviction — flagged as a
known limitation in docs/slack.md rather than built speculatively. A
process restart clears all in-memory conversational context (though the
on-disk core/sessions.py + core/eventlog.py records survive, same as the
terminal REPL).

Each thread's system message is core/agent.py's shared SYSTEM_PROMPT plus
a Slack-only language instruction (_SLACK_LANGUAGE_INSTRUCTION below) —
Slack replies are always in Traditional Chinese, regardless of the
terminal REPL/-p mode's language-neutral default. See docs/slack.md.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field

from langchain_core.messages import SystemMessage

from core.agent import SYSTEM_PROMPT
from core.sessions import StoredSession, create_session, save_session

# Slack-only addition to the shared SYSTEM_PROMPT — appended here rather
# than in core/agent.py so the terminal REPL/-p mode (which share that
# same constant) are unaffected; Slack replies should always be in
# Traditional Chinese regardless of what language the user writes in.
_SLACK_LANGUAGE_INSTRUCTION = (
    "\n\nYou are being used through Slack. Always reply in Traditional "
    "Chinese (繁體中文), regardless of what language the user's message is "
    "written in."
)


@dataclass
class ThreadSession:
    messages: list
    session_id: str
    history_session: StoredSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: float = field(default_factory=time.monotonic)


class ThreadRegistry:
    """Maps a Slack `thread_ts` to its `ThreadSession`. The registry lock
    only ever guards the dict's get-or-create step (fast, no I/O) — it is
    never held across a run_turn() call, so different threads' turns run
    fully concurrently; only messages within the *same* thread serialize,
    via that ThreadSession's own `lock`."""

    def __init__(self):
        self._threads: dict[str, ThreadSession] = {}
        self._registry_lock = asyncio.Lock()

    def known_thread_ts(self) -> set[str]:
        return set(self._threads.keys())

    async def get_or_create(self, thread_ts: str, initial_cwd: str, model: str | None) -> ThreadSession:
        async with self._registry_lock:
            session = self._threads.get(thread_ts)
            if session is None:
                full_id = str(uuid.uuid4())
                messages = [SystemMessage(
                    content=SYSTEM_PROMPT + _SLACK_LANGUAGE_INSTRUCTION
                    + f"\n\nWorking directory: {initial_cwd}"
                )]
                history_session = create_session(full_id, initial_cwd, model)
                save_session(history_session)
                session = ThreadSession(messages=messages, session_id=full_id, history_session=history_session)
                self._threads[thread_ts] = session
            return session

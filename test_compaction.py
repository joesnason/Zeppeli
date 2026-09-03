"""Automated tests for core/messages.py's compact_messages() — the
turn-level context-window compaction applied to the view sent to the model
(via ui/streaming.py's stream_response()), never to the canonical `messages`
list ui/repl.py's _run_and_persist() slices for session/event-log
persistence. Style matches test_truncation.py. No Ollama/network
dependency, exits non-zero on failure.

A "turn" is one HumanMessage plus everything that follows it (AIMessage/
ToolMessage hops) up to but not including the next HumanMessage. Turns, not
raw messages, are the unit compact_messages() counts/drops, specifically so
a hop's AIMessage(tool_calls=[...]) is never separated from its matching
ToolMessage(s) — see core/messages.py's docstring for the full rationale.
"""

import io
import sys

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from rich.console import Console

from core.messages import compact_messages
from ui.streaming import stream_response


def _turn(n):
    """One simple turn: HumanMessage(f"user {n}") + AIMessage(f"reply {n}")."""
    return [HumanMessage(content=f"user {n}"), AIMessage(content=f"reply {n}")]


def _tool_call_turn(n):
    """One multi-hop turn: HumanMessage, then an AIMessage(tool_calls=[...])
    followed by its matching ToolMessage, then a final AIMessage."""
    return [
        HumanMessage(content=f"user {n}"),
        AIMessage(content="", tool_calls=[{"name": "list_files", "args": {}, "id": f"call_{n}"}]),
        ToolMessage(content="ok", tool_call_id=f"call_{n}"),
        AIMessage(content=f"reply {n}"),
    ]


def _build(n_turns, turn_builder=_turn, preamble=True):
    messages = [SystemMessage(content="sys")] if preamble else []
    for i in range(n_turns):
        messages.extend(turn_builder(i))
    return messages


def test_at_or_below_threshold_returns_all_turns_unchanged():
    messages = _build(25)
    result = compact_messages(messages)
    assert len(result) == len(messages)
    for a, b in zip(result, messages):
        assert a is b


def test_above_threshold_compacts_to_first_plus_latest_24():
    messages = _build(30)
    result = compact_messages(messages)
    texts = [getattr(m, "content", "") for m in result]
    assert "user 0" in texts  # first turn kept
    for i in range(1, 6):
        assert f"user {i}" not in texts  # turns 1-5 dropped
    for i in range(6, 30):
        assert f"user {i}" in texts  # latest 24 turns (6-29) kept


def test_preamble_always_retained_and_uncounted():
    messages = _build(40)
    result = compact_messages(messages)
    assert isinstance(result[0], SystemMessage)
    assert result[0] is messages[0]
    # 40 turns compacts to 1 + 24 = 25 turns' worth of messages (2 per
    # simple turn) plus the 1 preamble message.
    assert len(result) == 1 + 25 * 2


def test_no_human_message_returns_unchanged():
    assert compact_messages([]) == []
    only_system = [SystemMessage(content="sys")]
    result = compact_messages(only_system)
    assert len(result) == 1
    assert result[0] is only_system[0]


def test_multihop_tool_call_turn_survives_intact_when_kept():
    # 30 turns, with the oldest kept turn (index 6) being a multi-hop
    # tool-call turn — right at the edge of the drop/keep boundary.
    messages = [SystemMessage(content="sys")]
    for i in range(30):
        messages.extend(_tool_call_turn(i) if i == 6 else _turn(i))
    result = compact_messages(messages)

    tool_msgs = [m for m in result if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    tm = tool_msgs[0]
    ai_with_calls = [m for m in result if isinstance(m, AIMessage) and m.tool_calls]
    assert len(ai_with_calls) == 1
    assert any(tc["id"] == tm.tool_call_id for tc in ai_with_calls[0].tool_calls)


def test_dropped_turn_with_tool_calls_is_dropped_as_a_whole_unit():
    # Same construction, but the tool-call turn is at index 3 — inside the
    # dropped region (turns 1-5 when there are 30 turns total).
    messages = [SystemMessage(content="sys")]
    for i in range(30):
        messages.extend(_tool_call_turn(i) if i == 3 else _turn(i))
    result = compact_messages(messages)
    assert not any(isinstance(m, ToolMessage) for m in result)
    assert not any(isinstance(m, AIMessage) and m.tool_calls for m in result)


def test_does_not_mutate_input_list():
    messages = _build(30)
    before_len = len(messages)
    before_ids = [id(m) for m in messages]
    compact_messages(messages)
    assert len(messages) == before_len
    assert [id(m) for m in messages] == before_ids


def test_returned_view_message_objects_are_shared_not_deep_copied():
    messages = _build(30)
    result = compact_messages(messages)
    # The first kept message (preamble) and the last message (latest turn)
    # should be the exact same objects, not copies.
    assert result[0] is messages[0]
    assert result[-1] is messages[-1]


class _RecordingLLM:
    """Stands in for llm_with_tools: records the exact `messages` list
    .stream() was called with (self.received), and yields a trivial
    one-chunk response."""

    def __init__(self):
        self.received = None

    def stream(self, messages, reasoning=None):
        self.received = messages
        return iter([AIMessageChunk(content="ok")])


def test_stream_response_passes_compacted_view_to_model_not_full_messages():
    messages = _build(30)
    original_len = len(messages)
    llm = _RecordingLLM()
    console = Console(file=io.StringIO())

    stream_response(llm, messages, console)

    assert llm.received is not None
    assert llm.received is not messages
    human_count = sum(1 for m in llm.received if isinstance(m, HumanMessage))
    assert human_count == 25  # compacted to 25 turns, not 30
    assert len(messages) == original_len  # caller's own list untouched


TESTS = [
    test_at_or_below_threshold_returns_all_turns_unchanged,
    test_above_threshold_compacts_to_first_plus_latest_24,
    test_preamble_always_retained_and_uncounted,
    test_no_human_message_returns_unchanged,
    test_multihop_tool_call_turn_survives_intact_when_kept,
    test_dropped_turn_with_tool_calls_is_dropped_as_a_whole_unit,
    test_does_not_mutate_input_list,
    test_returned_view_message_objects_are_shared_not_deep_copied,
    test_stream_response_passes_compacted_view_to_model_not_full_messages,
]


if __name__ == "__main__":
    failures = []
    for t in TESTS:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failures.append(t.__name__)

    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)

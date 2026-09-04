"""Automated tests for core/messages.py's two-tier context compaction and
its ui/streaming.py stream_response() integration. Neither tier mutates,
or is ever visible in, the canonical `messages` list ui/repl.py's
_run_and_persist() slices for session/event-log persistence. Style matches
test_truncation.py. No Ollama/network dependency, exits non-zero on
failure.

Tier 1 — compact_messages(): a "turn" is one HumanMessage plus everything
that follows it (AIMessage/ToolMessage hops) up to but not including the
next HumanMessage. Turns, not raw messages, are the unit compact_messages()
counts/drops, specifically so a hop's AIMessage(tool_calls=[...]) is never
separated from its matching ToolMessage(s) — see core/messages.py's
docstring for the full rationale.

Tier 2 — compact_messages_to_budget(): runs on tier 1's already-compacted
view; if the estimated token count still exceeds 80% of the model's
context window (or a 256k default when unknown), compacts further to the
first turn + latest 6 turns, replacing everything else with one
synthesized summary HumanMessage — a numbered, per-original-message bullet
list, role-labeled (user/assistant/tool), each item truncated to its first
500 + last 150 characters if it exceeds 650.
"""

import asyncio
import io
import sys

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from rich.console import Console

from core.messages import compact_messages, compact_messages_to_budget, _truncate_for_summary
from ui.live_region import SimpleLive
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


def _big_turn(n, size=400):
    """One simple turn like _turn(), but with a longer AIMessage.content —
    used to push the estimated token count over a small test budget."""
    return [HumanMessage(content=f"user {n}"), AIMessage(content="x" * size)]


def _multi_tool_call_turn(n):
    """One multi-hop turn like _tool_call_turn(), but with two tool_calls
    in a single AIMessage hop (and their two matching ToolMessages)."""
    return [
        HumanMessage(content=f"user {n}"),
        AIMessage(content="", tool_calls=[
            {"name": "list_files", "args": {}, "id": f"call_{n}_a"},
            {"name": "read_file", "args": {"path": "x"}, "id": f"call_{n}_b"},
        ]),
        ToolMessage(content="ok1", tool_call_id=f"call_{n}_a"),
        ToolMessage(content="ok2", tool_call_id=f"call_{n}_b"),
        AIMessage(content=f"reply {n}"),
    ]


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


def test_budget_below_threshold_returns_unchanged():
    messages = _build(5)  # small turn count, tiny content
    result = compact_messages_to_budget(messages, context_window=1_000_000)
    assert len(result) == len(messages)
    for a, b in zip(result, messages):
        assert a is b


def test_budget_above_threshold_produces_first_plus_latest_6_plus_summary():
    messages = [SystemMessage(content="sys")]
    for i in range(10):
        messages.extend(_big_turn(i, size=400))
    # context_window=1000 -> budget = 800 tokens = 3200 chars; 10 turns x
    # ~400+ chars each comfortably exceeds that.
    result = compact_messages_to_budget(messages, context_window=1000)

    # preamble(1) + first turn(2 msgs) + summary(1 msg) + latest 6 turns(12 msgs)
    assert len(result) == 1 + 2 + 1 + 6 * 2
    assert isinstance(result[0], SystemMessage)
    assert result[1].content == "user 0"  # first turn retained verbatim

    summary = result[3]
    assert isinstance(summary, HumanMessage)
    assert "[Earlier conversation summarized" in summary.content
    for i in range(1, 4):  # turns 1-3 are the dropped middle
        assert f"user {i}" in summary.content

    texts = [getattr(m, "content", "") for m in result]
    for i in range(4, 10):  # turns 4-9 are the latest 6, kept verbatim
        assert f"user {i}" in texts


def test_budget_floor_case_seven_or_fewer_turns_returns_unchanged_even_over_budget():
    messages = [SystemMessage(content="sys")]
    for i in range(7):
        messages.extend(_big_turn(i, size=1000))  # force well over budget
    result = compact_messages_to_budget(messages, context_window=1000)
    assert len(result) == len(messages)
    for a, b in zip(result, messages):
        assert a is b


def test_budget_summary_role_labels_and_tool_name_resolution():
    messages = [SystemMessage(content="sys")]
    for i in range(10):
        messages.extend(_tool_call_turn(i) if i == 2 else _big_turn(i, size=400))
    result = compact_messages_to_budget(messages, context_window=1000)
    summary = next(m for m in result if isinstance(m, HumanMessage)
                    and "[Earlier conversation" in m.content)
    body = summary.content
    assert "user: user 2" in body
    assert '"tool": "list_files"' in body  # real JSON, not Python repr
    assert "tool: Previous tool result for list_files: ok" in body
    assert "assistant: reply 2" in body


def test_budget_summary_multi_tool_call_hop_renders_as_one_bullet():
    messages = [SystemMessage(content="sys")]
    for i in range(10):
        messages.extend(_multi_tool_call_turn(i) if i == 2 else _big_turn(i, size=400))
    result = compact_messages_to_budget(messages, context_window=1000)
    summary = next(m for m in result if isinstance(m, HumanMessage)
                    and "[Earlier conversation" in m.content)
    assistant_bullets = [ln for ln in summary.content.split("\n")
                         if "assistant:" in ln and "list_files" in ln]
    assert len(assistant_bullets) == 1  # one bullet, not two
    assert "read_file" in assistant_bullets[0]


def test_summary_truncate_short_text_unchanged():
    text = "x" * 650
    assert _truncate_for_summary(text) == text


def test_summary_truncate_long_text_has_head_tail_and_marker():
    text = "x" * 500 + "y" * 300 + "z" * 150  # 950 chars, over the 650 threshold
    result = _truncate_for_summary(text)
    assert result.startswith("x" * 500)
    assert result.endswith("z" * 150)
    omitted = len(text) - 500 - 150
    assert f"[truncated {omitted} chars]" in result


def test_budget_context_window_none_falls_back_to_default():
    # Small conversation, nowhere near 256_000 * 0.8 tokens (~819_200
    # chars) — must NOT trigger, proving the fallback is a large sane
    # number rather than 0/None being treated as "no budget."
    messages = _build(10)
    result = compact_messages_to_budget(messages, context_window=None)
    assert len(result) == len(messages)
    for a, b in zip(result, messages):
        assert a is b


def test_budget_does_not_mutate_input_list():
    messages = [SystemMessage(content="sys")]
    for i in range(10):
        messages.extend(_big_turn(i, size=400))
    before_len = len(messages)
    before_ids = [id(m) for m in messages]
    compact_messages_to_budget(messages, context_window=1000)
    assert len(messages) == before_len
    assert [id(m) for m in messages] == before_ids


class _RecordingLLM:
    """Stands in for llm_with_tools: records the exact `messages` list
    .astream() was called with (self.received), and yields a trivial
    one-chunk response."""

    def __init__(self):
        self.received = None

    async def astream(self, messages, reasoning=None):
        self.received = messages
        yield AIMessageChunk(content="ok")


def test_stream_response_passes_compacted_view_to_model_not_full_messages():
    messages = _build(30)
    original_len = len(messages)
    llm = _RecordingLLM()
    console = Console(file=io.StringIO())
    live = SimpleLive(console)

    asyncio.run(stream_response(llm, messages, console, live))

    assert llm.received is not None
    assert llm.received is not messages
    human_count = sum(1 for m in llm.received if isinstance(m, HumanMessage))
    assert human_count == 25  # compacted to 25 turns, not 30
    assert len(messages) == original_len  # caller's own list untouched


def test_stream_response_applies_budget_tier_when_context_window_given():
    messages = [SystemMessage(content="sys")]
    for i in range(10):
        messages.extend(_big_turn(i, size=400))
    original_len = len(messages)
    llm = _RecordingLLM()
    console = Console(file=io.StringIO())
    live = SimpleLive(console)

    asyncio.run(stream_response(llm, messages, console, live, context_window=1000))

    assert llm.received is not None
    human_count = sum(1 for m in llm.received if isinstance(m, HumanMessage))
    # first turn's Human + latest 6 turns' Human + 1 synthesized summary Human
    assert human_count == 1 + 6 + 1
    assert any("[Earlier conversation summarized" in getattr(m, "content", "")
               for m in llm.received)
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
    test_budget_below_threshold_returns_unchanged,
    test_budget_above_threshold_produces_first_plus_latest_6_plus_summary,
    test_budget_floor_case_seven_or_fewer_turns_returns_unchanged_even_over_budget,
    test_budget_summary_role_labels_and_tool_name_resolution,
    test_budget_summary_multi_tool_call_hop_renders_as_one_bullet,
    test_summary_truncate_short_text_unchanged,
    test_summary_truncate_long_text_has_head_tail_and_marker,
    test_budget_context_window_none_falls_back_to_default,
    test_budget_does_not_mutate_input_list,
    test_stream_response_applies_budget_tier_when_context_window_given,
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

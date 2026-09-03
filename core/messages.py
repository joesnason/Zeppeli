"""Shared message-content normalization — no UI deps.

Used by ui/streaming.py (Markdown rendering) and core/sessions.py (history
persistence), both of which need to flatten a LangChain message's `.content`
into plain text before doing anything else with it. Also used by
core/eventlog.py (event logging) for the same reason, plus tool_result_ok()
below for classifying a tool's ToolMessage output, truncate_tool_output()
below (used by ui/turn.py to cap a tool result before it becomes ToolMessage
content sent back to the model), compact_messages() below (used by
ui/streaming.py to build a turn-windowed view of the conversation before
every model call), and compact_messages_to_budget() below (a second,
more aggressive compaction layered on top of compact_messages(), triggered
by an estimated token-budget check rather than a fixed turn count).
"""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

_LINE_TRUNCATE_THRESHOLD = 40   # trigger: more than this many lines
_LINE_KEEP_HEAD = 20
_LINE_KEEP_TAIL = 20
_CHAR_TRUNCATE_THRESHOLD = 2400  # trigger: more than this many chars, checked after the line rule
_CHAR_KEEP_HEAD = 1200
_CHAR_KEEP_TAIL = 1200

_MAX_TURNS = 25        # compact once the turn count exceeds this
_KEEP_FIRST_TURNS = 1  # always keep the conversation's opening turn
_KEEP_LAST_TURNS = 24  # plus the most recent N turns (25 total when compacted)

_DEFAULT_CONTEXT_WINDOW = 256_000  # fallback when the real context window is unknown (k=1000, matching ui/repl.py's toolbar `tokens // 1000` convention)
_BUDGET_TRIGGER_RATIO = 0.8         # trigger once estimated tokens exceed this fraction of the context window
_CHARS_PER_TOKEN = 4                # rough "1 token ≈ 4 chars" heuristic

_BUDGET_KEEP_FIRST_TURNS = 1
_BUDGET_KEEP_LAST_TURNS = 6

_SUMMARY_KEEP_HEAD = 500
_SUMMARY_KEEP_TAIL = 150
_SUMMARY_TRUNCATE_THRESHOLD = _SUMMARY_KEEP_HEAD + _SUMMARY_KEEP_TAIL  # 650
_SUMMARY_HEADER = "[Earlier conversation summarized to fit context budget]"


def extract_text(content) -> str:
    """Normalize an AIMessage/HumanMessage `.content` into plain text.

    ChatOllama gives a plain str. Some litellm-routed cloud/self-hosted
    backends (e.g. Anthropic-style APIs) instead give a list of str/dict
    content blocks (e.g. [{"type": "text", "text": "..."}], possibly mixed
    with non-text blocks like tool_use or image_url) — those must be
    flattened to text before Markdown rendering or persistence."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def tool_result_ok(output: str) -> bool:
    """core/tools.py's @tool functions signal failure by returning an
    'Error: ...' string rather than raising; ui/turn.py's cancelled-
    permission result contains 'CANCELLED'. Anything else is success."""
    return not (output.startswith("Error:") or "CANCELLED" in output)


def truncate_tool_output(text: str) -> str:
    """Truncate a tool result before it becomes ToolMessage content sent back
    to the model. Two independent rules, both checked, both can apply (line
    rule first, then char rule on the possibly-already-line-truncated text):

    1. Line rule: if `text` has more than 40 lines, keep the first 20 and
       last 20 lines, with a "[truncated N lines]" marker between them.
    2. Char rule: if the result of step 1 still exceeds 2400 characters,
       keep the first 1200 and last 1200 characters, with a
       "[truncated N chars]" marker between them.

    Bracket-note style matches core/tools.py's rg_search max_bytes cap.
    Callers (ui/turn.py) stash the untruncated original in
    ToolMessage.additional_kwargs["full_output"] for session/event-log
    persistence — this function only affects what the model sees."""
    result = text
    lines = result.split("\n")
    if len(lines) > _LINE_TRUNCATE_THRESHOLD:
        omitted = len(lines) - _LINE_KEEP_HEAD - _LINE_KEEP_TAIL
        head = "\n".join(lines[:_LINE_KEEP_HEAD])
        tail = "\n".join(lines[-_LINE_KEEP_TAIL:])
        result = f"{head}\n[truncated {omitted} lines]\n{tail}"

    if len(result) > _CHAR_TRUNCATE_THRESHOLD:
        omitted = len(result) - _CHAR_KEEP_HEAD - _CHAR_KEEP_TAIL
        head = result[:_CHAR_KEEP_HEAD]
        tail = result[-_CHAR_KEEP_TAIL:]
        result = f"{head}\n[truncated {omitted} chars]\n{tail}"

    return result


def _split_into_turns(messages: list) -> tuple[list, list[list]]:
    """Split `messages` into (preamble, turns) — shared by compact_messages()
    and compact_messages_to_budget(). preamble is everything before the
    first HumanMessage (in practice just the always-present SystemMessage);
    each turn is one HumanMessage plus everything up to (not including) the
    next HumanMessage. Returns (list(messages), []) if there's no
    HumanMessage at all — callers decide what "no turns" means for them."""
    first_human_idx = next(
        (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), None
    )
    if first_human_idx is None:
        return list(messages), []

    preamble = messages[:first_human_idx]
    turns = []
    for m in messages[first_human_idx:]:
        if isinstance(m, HumanMessage):
            turns.append([m])
        else:
            turns[-1].append(m)
    return preamble, turns


def compact_messages(messages: list) -> list:
    """Return the view of `messages` to send to the model: the first turn +
    latest 24 turns (25 total) once the conversation exceeds 25 turns.
    Never mutates `messages` — always returns a freshly-built list.

    A "turn" is one HumanMessage plus everything that follows it
    (AIMessage/ToolMessage hops) up to but not including the next
    HumanMessage. Turns — not raw messages — are the unit of counting and
    dropping, so a hop's AIMessage(tool_calls=[...]) is never separated
    from its matching ToolMessage(s); a raw positional slice could split
    that pairing and produce a request some backends (OpenAI-compatible
    cloud APIs via litellm) reject outright.

    Any leading non-turn preamble (everything before the first
    HumanMessage — in practice just the always-present SystemMessage at
    messages[0]) is kept in full and never counted toward the 25.

    Silent: no marker/note is inserted to tell the model turns were
    dropped. This is independent of, and layered above, truncate_tool_
    output() above — that still applies inside whichever turns are kept."""
    preamble, turns = _split_into_turns(messages)
    if not turns:
        return list(messages)

    if len(turns) <= _MAX_TURNS:
        return list(messages)

    kept_turns = turns[:_KEEP_FIRST_TURNS] + turns[-_KEEP_LAST_TURNS:]
    view = list(preamble)
    for turn in kept_turns:
        view.extend(turn)
    return view


def _estimate_tokens(messages: list) -> int:
    """Rough token-count estimate for `messages`: total characters across
    every message's content (via extract_text(), so it handles both plain-
    str and list-of-blocks content) plus a JSON rendering of any AIMessage's
    tool_calls (real request payload too), divided by ~4 chars/token."""
    total_chars = 0
    for m in messages:
        total_chars += len(extract_text(m.content))
        if isinstance(m, AIMessage) and m.tool_calls:
            total_chars += len(json.dumps(m.tool_calls))
    return total_chars // _CHARS_PER_TOKEN


def _truncate_for_summary(text: str) -> str:
    """Truncate a single summarized message's displayed content to its
    first 500 + last 150 characters if it exceeds 650 — a sibling of
    truncate_tool_output()'s char rule, same bracket-note style, smaller
    keep-sizes appropriate for a compact one-line bullet."""
    if len(text) <= _SUMMARY_TRUNCATE_THRESHOLD:
        return text
    omitted = len(text) - _SUMMARY_KEEP_HEAD - _SUMMARY_KEEP_TAIL
    head = text[:_SUMMARY_KEEP_HEAD]
    tail = text[-_SUMMARY_KEEP_TAIL:]
    return f"{head}\n[truncated {omitted} chars]\n{tail}"


def compact_messages_to_budget(messages: list, context_window: int | None = None) -> list:
    """Given `messages` (already turn-compacted by compact_messages()),
    apply a second, more aggressive compaction if the estimated token count
    still exceeds 80% of the model's context window (context_window, or
    256_000 when unknown — a cloud/litellm model, or a failed local-Ollama
    lookup).

    If triggered: keeps the preamble, the first turn, and the latest 6
    turns (tier 1's 24 is too generous once the budget is this tight) —
    every message in the turns between them is flattened, in original
    order, into one numbered bullet list, one bullet per original message,
    packed into a single synthesized HumanMessage (role "user"). Each
    bullet is labeled by the original LangChain role (user/assistant/tool,
    matching docs/sessions.md's existing mapping), and truncated to its
    first 500 + last 150 characters if it exceeds 650. A ToolMessage's
    bullet resolves its tool name via a tool_call_id -> name map built
    from the AIMessages being summarized. Never mutates `messages`."""
    limit = context_window if context_window else _DEFAULT_CONTEXT_WINDOW
    budget = int(limit * _BUDGET_TRIGGER_RATIO)
    if _estimate_tokens(messages) <= budget:
        return list(messages)

    preamble, turns = _split_into_turns(messages)
    if len(turns) <= _BUDGET_KEEP_FIRST_TURNS + _BUDGET_KEEP_LAST_TURNS:
        return list(messages)  # nothing meaningful to summarize away

    kept_first = turns[:_BUDGET_KEEP_FIRST_TURNS]
    kept_last = turns[-_BUDGET_KEEP_LAST_TURNS:]
    dropped = [m for turn in turns[_BUDGET_KEEP_FIRST_TURNS:-_BUDGET_KEEP_LAST_TURNS] for m in turn]

    tool_name_by_id = {
        tc["id"]: tc["name"]
        for m in dropped if isinstance(m, AIMessage) for tc in (m.tool_calls or [])
    }

    bullets = [_SUMMARY_HEADER]
    for i, m in enumerate(dropped, start=1):
        if isinstance(m, HumanMessage):
            text = _truncate_for_summary(extract_text(m.content))
            bullets.append(f"{i}. user: {text}")
        elif isinstance(m, AIMessage) and m.tool_calls:
            calls = [{"tool": tc["name"], "args": tc["args"]} for tc in m.tool_calls]
            rendered = json.dumps(calls[0] if len(calls) == 1 else calls)
            bullets.append(f"{i}. assistant: {_truncate_for_summary(rendered)}")
        elif isinstance(m, AIMessage):
            text = _truncate_for_summary(extract_text(m.content))
            bullets.append(f"{i}. assistant: {text}")
        elif isinstance(m, ToolMessage):
            name = tool_name_by_id.get(m.tool_call_id, "unknown")
            text = _truncate_for_summary(extract_text(m.content))
            bullets.append(f"{i}. tool: Previous tool result for {name}: {text}")

    summary_message = HumanMessage(content="\n".join(bullets))

    view = list(preamble)
    for turn in kept_first:
        view.extend(turn)
    view.append(summary_message)
    for turn in kept_last:
        view.extend(turn)
    return view

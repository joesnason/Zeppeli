"""Streaming AI responses to the terminal — spinner, then live Markdown rendering."""

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown as RichMarkdown
from rich.markup import escape

from core.eventlog import log_model_activity
from core.messages import (
    extract_text as _extract_text,
    compact_messages as _compact_messages,
    compact_messages_to_budget as _compact_messages_to_budget,
)

_ctx_state = {"tokens": 0}

# Set once, the first time a reasoning=True stream call raises — remembered
# for the rest of the process so later turns skip straight to a plain call
# instead of re-attempting (and re-failing) reasoning mode every single hop.
# ui/repl.py's main() already gates reasoning=True on
# core.agent.model_supports_reasoning() (checks ollama.show()'s advertised
# capabilities up front), so this should rarely trigger in practice — it's
# a safety net for whatever that upfront check can't see (a stale/wrong
# capabilities list, an Ollama version quirk), not the primary guard.
_reasoning_unsupported = {"flag": False}


def _format_model_error(e: Exception) -> str:
    """Render an exception from the model call as a short, readable message.

    litellm/provider errors (ContextWindowExceededError, etc.) carry deeply
    nested JSON bodies that are unreadable dumped raw to the terminal, so
    this trims them and adds an actionable hint for the common case."""
    name = type(e).__name__
    msg = str(e)
    if len(msg) > 300:
        msg = msg[:300] + "…"
    if "ContextWindowExceeded" in name or "context length" in msg.lower():
        return (
            "the request exceeded the model's context window. Try a "
            "narrower rg_search pattern/glob, a smaller read_file range, or "
            "start a fresh conversation."
        )
    low = msg.lower()
    if "image" in low and any(
        k in low for k in (
            "not support", "unsupported", "invalid", "no vision",
            # vLLM's own multimodal-input validation phrases this as a limit
            # rather than an "unsupported"/"invalid" refusal, e.g. "At most 0
            # image(s) may be provided in one prompt. (parameter=image)" when
            # the served checkpoint has no image modality at all.
            "may be provided", "parameter=image",
        )
    ):
        return (
            "this model doesn't accept image input. Send the message "
            "without an image, or switch to a vision-capable model/endpoint "
            "(e.g. --base-url http://host:8000/v1 "
            "--model hosted_vllm/qwen3.6-27b-awq-int4) — if you're already "
            "pointed at what should be a vision model, double-check the "
            "checkpoint actually being served is the VL/vision variant, not "
            "a text-only one, and that the server wasn't started with an "
            "image limit of 0 (vLLM's --limit-mm-per-prompt)."
        )
    return f"{name}: {msg}"


def _consume_stream(llm_with_tools, messages, console: Console, *, reasoning: bool) -> list:
    """Runs the spinner-then-Live-Markdown loop, consuming llm_with_tools's
    stream. Returns the list of raw chunks (possibly empty if the stream
    yielded nothing). Raises whatever the model call raises — the caller
    decides how to handle it (stream_response() below)."""
    chunks = []
    accumulated = ""
    stream = llm_with_tools.stream(messages, reasoning=True) if reasoning else llm_with_tools.stream(messages)

    with console.status("[dim]Thinking...[/dim]", spinner="dots"):
        for chunk in stream:
            chunks.append(chunk)
            text = _extract_text(chunk.content)
            if text:
                accumulated = text
                break

    with Live(RichMarkdown(accumulated), console=console, refresh_per_second=15) as live:
        for chunk in stream:
            text = _extract_text(chunk.content)
            if text:
                accumulated += text
                live.update(RichMarkdown(accumulated))
            chunks.append(chunk)
    return chunks


def stream_response(llm_with_tools, messages, console: Console, *,
                     session_id: str | None = None, run_id: str | None = None,
                     turn_index: int | None = None, reasoning: bool = False,
                     context_window: int | None = None):
    """Show a 'Thinking...' spinner until the first content token arrives, then
    stream the rest of the response as live-updating Markdown. Returns the
    accumulated AIMessage (chunks merged), or None if the stream was empty or
    the model call raised (e.g. context window exceeded, network error) —
    the failure is reported to the console rather than propagating and
    crashing the process.

    reasoning=True (ui/repl.py's main() sets this for local Ollama models
    that advertise reasoning support, via ui/turn.py's run_turn() — see
    core.agent.model_supports_reasoning()) asks ChatOllama to separate the
    model's reasoning/thinking into additional_kwargs["reasoning_content"]
    instead of folding it into the visible response — see docs/logging.md.
    If the loaded model/backend doesn't actually support it despite that
    upfront check, the first attempt's exception is treated as possibly
    reasoning-related: this retries once without reasoning before giving
    up, and remembers not to try reasoning again for the rest of the
    process (_reasoning_unsupported), so later turns don't pay the same
    failed-attempt cost every hop.

    When session_id/run_id/turn_index are all given (ui/turn.py's run_turn()
    passes these through), logs one model_activity event for this hop after
    a successful merge — skipped entirely if any of the three is None, so
    other/test callers are unaffected.

    Before every call to the model, `messages` is compacted in two layers,
    neither of which mutates `messages` itself (and everything ui/turn.py
    and ui/repl.py's session/event-log persistence do with it is
    untouched):
      1. core/messages.py's compact_messages() — a turn-windowed view
         (first turn + latest 24, once the conversation exceeds 25 turns).
      2. core/messages.py's compact_messages_to_budget() — applied on top
         of (1)'s result; if the estimated token count still exceeds 80%
         of `context_window` (or a 256k default when None), compacts
         further to the first turn + latest 6, replacing everything else
         with one synthesized summary message."""
    use_reasoning = reasoning and not _reasoning_unsupported["flag"]
    view = _compact_messages(messages)
    view = _compact_messages_to_budget(view, context_window)
    try:
        chunks = _consume_stream(llm_with_tools, view, console, reasoning=use_reasoning)
    except Exception as e:
        if use_reasoning:
            _reasoning_unsupported["flag"] = True
            try:
                chunks = _consume_stream(llm_with_tools, view, console, reasoning=False)
            except Exception as e2:
                console.print(f"[red]Error: {escape(_format_model_error(e2))}[/red]")
                return None
        else:
            console.print(f"[red]Error: {escape(_format_model_error(e))}[/red]")
            return None

    if not chunks:
        return None
    response = chunks[0]
    for c in chunks[1:]:
        response = response + c

    if session_id is not None and run_id is not None and turn_index is not None:
        log_model_activity(
            session_id, run_id, index=turn_index,
            finalization=not bool(response.tool_calls),
            thinking=response.additional_kwargs.get("reasoning_content", "") or "",
            content=_extract_text(response.content),
        )
    return response


def _update_ctx(response):
    usage = getattr(response, "usage_metadata", None)
    if usage:
        _ctx_state["tokens"] = usage["input_tokens"]

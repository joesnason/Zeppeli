"""Streaming AI responses to the terminal — spinner, then live Markdown rendering."""

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown as RichMarkdown
from rich.markup import escape

_ctx_state = {"tokens": 0}


def _extract_text(content) -> str:
    """Normalize an AIMessage chunk's `.content` into plain text.

    ChatOllama chunks give a plain str. Some litellm-routed cloud/self-hosted
    backends (e.g. Anthropic-style APIs) instead give a list of str/dict
    content blocks (e.g. [{"type": "text", "text": "..."}], possibly mixed
    with non-text blocks like tool_use) — those must be flattened to text
    before Markdown rendering, or RichMarkdown(...) raises a TypeError."""
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
    return f"{name}: {msg}"


def stream_response(llm_with_tools, messages, console: Console):
    """Show a 'Thinking...' spinner until the first content token arrives, then
    stream the rest of the response as live-updating Markdown. Returns the
    accumulated AIMessage (chunks merged), or None if the stream was empty or
    the model call raised (e.g. context window exceeded, network error) —
    the failure is reported to the console rather than propagating and
    crashing the process."""
    chunks = []
    accumulated = ""
    try:
        stream = llm_with_tools.stream(messages)

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
    except Exception as e:
        console.print(f"[red]Error: {escape(_format_model_error(e))}[/red]")
        return None

    if not chunks:
        return None
    response = chunks[0]
    for c in chunks[1:]:
        response = response + c
    return response


def _update_ctx(response):
    usage = getattr(response, "usage_metadata", None)
    if usage:
        _ctx_state["tokens"] = usage["input_tokens"]

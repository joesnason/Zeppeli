"""Streaming AI responses to the terminal — spinner, then live Markdown rendering."""

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown as RichMarkdown

_ctx_state = {"tokens": 0}


def stream_response(llm_with_tools, messages, console: Console):
    """Show a 'Thinking...' spinner until the first content token arrives, then
    stream the rest of the response as live-updating Markdown. Returns the
    accumulated AIMessage (chunks merged), or None if the stream was empty."""
    chunks = []
    accumulated = ""
    stream = llm_with_tools.stream(messages)

    with console.status("[dim]Thinking...[/dim]", spinner="dots"):
        for chunk in stream:
            chunks.append(chunk)
            if chunk.content:
                accumulated = chunk.content
                break

    with Live(RichMarkdown(accumulated), console=console, refresh_per_second=15) as live:
        for chunk in stream:
            if chunk.content:
                accumulated += chunk.content
                live.update(RichMarkdown(accumulated))
            chunks.append(chunk)

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

"""One REPL turn: stream a response, then loop through any tool calls the AI makes.

Async: tool invocation (`core/tools.py`'s subprocess calls / blocking file
I/O) is offloaded to a worker thread via `asyncio.to_thread()` so it never
blocks the persistent Application's event loop (ui/repl.py) — that's what
keeps the bottom toolbar rendering during a tool call. `core/tools.py`
itself needs no changes; the offload happens purely at the call site here.
`permission_ask()` (via the pre-tool hooks) is awaited directly, never
wrapped in `asyncio.to_thread()` — it manipulates the live Application's
focus/key-bindings, which isn't thread-safe to touch off the main loop."""

import asyncio

from rich.console import Console
from rich.markup import escape
from langchain_core.messages import HumanMessage, ToolMessage

from core import TOOLS_BY_NAME, resolve_paths
from core.images import ImageError, build_message_content
from core.messages import truncate_tool_output
from .streaming import stream_response, _update_ctx
from .permissions import MODE_APPROVAL, build_pre_tool_hooks, reset_turn_approvals


async def run_turn(llm_with_tools, messages, user_input: str, console: Console, live,
                    initial_cwd: str = ".", mode: str = MODE_APPROVAL,
                    images: list[str] | None = None,
                    session_id: str | None = None, run_id: str | None = None,
                    reasoning: bool = False, context_window: int | None = None):
    reset_turn_approvals()
    hooks = build_pre_tool_hooks(mode, initial_cwd, live)
    try:
        content = build_message_content(user_input, images or [], initial_cwd)
    except ImageError as e:
        console.print(f"[red]Error: {escape(str(e))}[/red]")
        return
    messages.append(HumanMessage(content=content))
    turn_index = 0
    response = await stream_response(llm_with_tools, messages, console, live,
                                      session_id=session_id, run_id=run_id, turn_index=turn_index,
                                      reasoning=reasoning, context_window=context_window)
    if response is None:
        return
    messages.append(response)
    _update_ctx(response)
    turn_index += 1

    while response.tool_calls:
        for tc in response.tool_calls:
            info = escape(f"[tool: {tc['name']}({tc['args']})]")
            console.print(f"[dim]  {info}[/dim]")
            resolved_args = resolve_paths(tc["name"], tc["args"], initial_cwd)
            hook = hooks.get(tc["name"])
            if hook is not None and not await hook(tc["name"], resolved_args, console):
                result = (
                    f"[{tc['name']}] CANCELLED: the user denied permission. "
                    "No file was created, modified, or deleted. Tell the user "
                    "this action was cancelled — do not say it succeeded."
                )
            else:
                result = await asyncio.to_thread(TOOLS_BY_NAME[tc["name"]].invoke, resolved_args)
            full_output = str(result)
            messages.append(ToolMessage(
                content=truncate_tool_output(full_output),
                tool_call_id=tc["id"],
                additional_kwargs={"full_output": full_output},
            ))
        response = await stream_response(llm_with_tools, messages, console, live,
                                          session_id=session_id, run_id=run_id, turn_index=turn_index,
                                          reasoning=reasoning, context_window=context_window)
        if response is None:
            return
        messages.append(response)
        _update_ctx(response)
        turn_index += 1

"""One REPL turn: stream a response, then loop through any tool calls the AI makes."""

from rich.console import Console
from rich.markup import escape
from langchain_core.messages import HumanMessage, ToolMessage

from core import TOOLS_BY_NAME, resolve_paths
from .streaming import stream_response, _update_ctx
from .permissions import MODE_APPROVAL, build_pre_tool_hooks, reset_turn_approvals


def run_turn(llm_with_tools, messages, user_input: str, console: Console,
             initial_cwd: str = ".", mode: str = MODE_APPROVAL):
    reset_turn_approvals()
    hooks = build_pre_tool_hooks(mode, initial_cwd)
    messages.append(HumanMessage(content=user_input))
    response = stream_response(llm_with_tools, messages, console)
    messages.append(response)
    _update_ctx(response)

    while response.tool_calls:
        for tc in response.tool_calls:
            info = escape(f"[tool: {tc['name']}({tc['args']})]")
            console.print(f"[dim]  {info}[/dim]")
            resolved_args = resolve_paths(tc["name"], tc["args"], initial_cwd)
            hook = hooks.get(tc["name"])
            if hook is not None and not hook(tc["name"], resolved_args, console):
                result = (
                    f"[{tc['name']}] CANCELLED: the user denied permission. "
                    "No file was created, modified, or deleted. Tell the user "
                    "this action was cancelled — do not say it succeeded."
                )
            else:
                result = TOOLS_BY_NAME[tc["name"]].invoke(resolved_args)
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        response = stream_response(llm_with_tools, messages, console)
        messages.append(response)
        _update_ctx(response)

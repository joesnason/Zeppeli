"""Interactive REPL: input prompt, toolbar, and the top-level turn loop."""

import pathlib
import shutil
import sys
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markup import escape
from rich.rule import Rule
from langchain_core.messages import SystemMessage

from core import SYSTEM_PROMPT, load_llm
from .streaming import _ctx_state
from .turn import run_turn

SLASH_COMMANDS = ["/exit", "/quit"]


def _get_toolbar() -> str:
    from prompt_toolkit.application import get_app
    try:
        text = get_app().current_buffer.text
    except Exception:
        text = ""
    width = shutil.get_terminal_size().columns
    rule = "─" * width
    ctx_k = f"Ctx: {_ctx_state['tokens'] // 1000} k" if _ctx_state["tokens"] else "Ctx: 0 k"

    matches = (
        [c for c in SLASH_COMMANDS if c.startswith(text)]
        if text.startswith("/")
        else []
    )
    # Pad to fixed height so toolbar never resizes (prevents blank-line artifact)
    cmd_lines = matches + [""] * (len(SLASH_COMMANDS) - len(matches))
    return "\n".join([rule, ctx_k] + cmd_lines)


def main():
    console = Console()
    llm_with_tools = load_llm()
    initial_cwd = str(pathlib.Path.cwd())
    messages = [SystemMessage(content=SYSTEM_PROMPT + f"\n\nWorking directory: {initial_cwd}")]

    _toolbar_style = Style.from_dict({
        "bottom-toolbar": "bg:default fg:default noreverse",
    })
    session = PromptSession(bottom_toolbar=_get_toolbar, style=_toolbar_style)

    while True:
        console.print(Rule())
        try:
            user_input = session.prompt(
                FormattedText([("fg:#ff8700 bold", "> ")]),
            ).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "/exit", "/quit"):
            console.print("Bye!")
            break

        # Replace the typed line with the orange version
        sys.stdout.write("\x1b[A\x1b[2K")
        sys.stdout.flush()
        console.print(f"[bold orange1]> {escape(user_input)}[/bold orange1]")
        console.print()

        run_turn(llm_with_tools, messages, user_input, console, initial_cwd)

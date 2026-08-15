"""Interactive REPL: input prompt, toolbar, and the top-level turn loop."""

import pathlib
import shutil
import sys
import uuid
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markup import escape
from rich.rule import Rule
from langchain_core.messages import SystemMessage

from core import MODEL as DEFAULT_MODEL, SYSTEM_PROMPT, load_llm
from .permissions import MODE_APPROVAL, MODE_AUTO, MODE_YOLO, confirm_auto_mode_trust
from .streaming import _ctx_state
from .turn import run_turn

SLASH_COMMANDS = ["/exit", "/quit"]

_mode_state = {"mode": MODE_APPROVAL}
_model_state = {"name": ""}
_session_state = {"id": ""}

_MODE_COLORS = {
    MODE_YOLO: "#AB4C3F",
    MODE_AUTO: "#E1C167",
    MODE_APPROVAL: "#007C7C",
}

_MODE_LABELS = {
    MODE_APPROVAL: "Manual mode",
    MODE_AUTO: "auto mode",
    MODE_YOLO: "yolo mode",
}


def _toggle_mode(event):
    """Shift+Tab: flip live between Manual and Auto mode. No-op in yolo
    mode — yolo is launch-only (--yolo-mode) and not part of this cycle."""
    current = _mode_state["mode"]
    if current == MODE_APPROVAL:
        _mode_state["mode"] = MODE_AUTO
    elif current == MODE_AUTO:
        _mode_state["mode"] = MODE_APPROVAL
    event.app.invalidate()


def _clear_input(event):
    """Esc: clear the input line back to empty. No-op if already empty."""
    buffer = event.app.current_buffer
    if buffer.text:
        buffer.reset()


_kb = KeyBindings()
_kb.add("s-tab")(_toggle_mode)
_kb.add("escape")(_clear_input)


def _get_toolbar():
    from prompt_toolkit.application import get_app
    try:
        text = get_app().current_buffer.text
    except Exception:
        text = ""
    width = shutil.get_terminal_size().columns
    rule = "─" * width
    ctx_k = f"Ctx: {_ctx_state['tokens'] // 1000} k" if _ctx_state["tokens"] else "Ctx: 0 k"
    mode = _mode_state["mode"]
    color = _MODE_COLORS.get(mode, _MODE_COLORS[MODE_APPROVAL])
    mode_label = _MODE_LABELS.get(mode, f"{mode} mode")
    model_color = _MODE_COLORS[MODE_APPROVAL]

    matches = (
        [c for c in SLASH_COMMANDS if c.startswith(text)]
        if text.startswith("/")
        else []
    )
    # Pad to fixed height so toolbar never resizes (prevents blank-line artifact).
    # Fixed at 3 + len(SLASH_COMMANDS) lines: rule, model/mode, session/ctx, command hints.
    cmd_lines = matches + [""] * (len(SLASH_COMMANDS) - len(matches))
    return [
        ("", rule + "\n"),
        (f"fg:{model_color} bold", f"Model: {_model_state['name']}"),
        ("", "  |  "),
        (f"fg:{color} bold", mode_label),
        ("", "\n"),
        (f"fg:{model_color} bold", f"Session ID: {_session_state['id']}"),
        ("", "  |  "),
        ("", ctx_k),
        ("", "\n" + "\n".join(cmd_lines)),
    ]


def main(mode: str = MODE_APPROVAL, prompt: str | None = None,
         model: str | None = None, base_url: str | None = None, api_key: str | None = None):
    console = Console()
    _mode_state["mode"] = mode
    _model_state["name"] = model or DEFAULT_MODEL
    _session_state["id"] = str(uuid.uuid4())

    if mode == MODE_AUTO and prompt is None:
        # Interactive REPL launched with --auto-mode: gate entry on an
        # explicit one-time trust confirmation before loading the model or
        # auto-approving anything. Not shown for -p (headless/scripted) or
        # for the Shift+Tab live toggle into auto mode mid-session.
        if not confirm_auto_mode_trust(console):
            console.print("Bye!")
            return

    llm_with_tools = load_llm(model=model, base_url=base_url, api_key=api_key)
    initial_cwd = str(pathlib.Path.cwd())
    messages = [SystemMessage(content=SYSTEM_PROMPT + f"\n\nWorking directory: {initial_cwd}")]

    if prompt is not None:
        # One-shot mode: run exactly one turn and exit — no PromptSession/
        # toolbar (both assume an interactive terminal) and no REPL loop.
        run_turn(llm_with_tools, messages, prompt, console, initial_cwd, mode)
        return

    _toolbar_style = Style.from_dict({
        "bottom-toolbar": "bg:default fg:default noreverse",
    })
    session = PromptSession(bottom_toolbar=_get_toolbar, style=_toolbar_style, key_bindings=_kb)
    # prompt_toolkit's default timeoutlen (1.0s) waits after a lone Esc to
    # see if it's actually the start of an Alt-combo sequence (Alt+key is
    # sent as ESC + key on a raw terminal, and the default emacs bindings
    # include several). That wait makes _clear_input feel laggy. Shorten it
    # (mirrors Vim's ttimeoutlen fix) so Esc-to-clear reads as instant while
    # still leaving enough window for real Alt-combos to resolve correctly.
    session.app.timeoutlen = 0.1

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

        run_turn(llm_with_tools, messages, user_input, console, initial_cwd, _mode_state["mode"])

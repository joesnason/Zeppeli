"""Interactive REPL: input prompt, toolbar, and the top-level turn loop."""

import pathlib
import re
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

from core import MODEL as DEFAULT_MODEL, SYSTEM_PROMPT, load_llm, get_context_window
from core.images import MAX_SOURCE_BYTES, is_image_path, parse_image_mentions, resolve_image_path
from .completion import AtPathCompleter
from .permissions import MODE_APPROVAL, MODE_AUTO, MODE_YOLO, confirm_auto_mode_trust
from .streaming import _ctx_state
from .turn import run_turn

SLASH_COMMANDS = ["/exit", "/quit", "/image"]

_mode_state = {"mode": MODE_APPROVAL}
_model_state = {"name": ""}
_session_state = {"id": ""}
_ctx_limit_state = {"tokens": None}
_pending_images = {"paths": []}   # staged by /image, consumed by the next turn

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


def _display_path(path: str, cwd: str) -> str:
    """Shorten an absolute path to relative-to-cwd for echo/staging output
    when it's inside cwd; absolute otherwise. Showing the resolved path
    (not just the basename) is what makes a mis-resolution visible."""
    try:
        return str(pathlib.Path(path).relative_to(cwd))
    except ValueError:
        return path


def _stage_image(arg: str, console: Console, initial_cwd: str) -> None:
    """Handle `/image <path>` / `/image clear` / bare `/image`. Only does
    cheap validation (existence, extension, size) — full decode/downscale is
    deferred to send time so staging never pays for a resize you might not
    send. Extracted from the REPL loop so it's testable without a live
    PromptSession."""
    arg = arg.strip()
    if not arg:
        if _pending_images["paths"]:
            names = ", ".join(_display_path(p, initial_cwd) for p in _pending_images["paths"])
            console.print(f"[dim]Staged: {names}[/dim]")
        else:
            console.print("[dim]Usage: /image <path>  (or /image clear)[/dim]")
        return
    if arg.lower() == "clear":
        _pending_images["paths"] = []
        console.print("[dim]Cleared staged images.[/dim]")
        return

    raw = re.sub(r"\\(.)", r"\1", arg.strip("'\""))
    path = resolve_image_path(raw, initial_cwd)
    p = pathlib.Path(path)
    if not p.is_file():
        console.print(f"[red]Error: image not found: {raw}[/red]")
        return
    if not is_image_path(path):
        console.print(f"[red]Error: unsupported image type: {raw}[/red]")
        return
    if p.stat().st_size > MAX_SOURCE_BYTES:
        console.print(f"[red]Error: image too large: {raw}[/red]")
        return

    _pending_images["paths"].append(path)
    n = len(_pending_images["paths"])
    console.print(f"[dim]Staged: {_display_path(path, initial_cwd)} ({n} image{'s' if n != 1 else ''})[/dim]")


def _take_pending_images() -> list[str]:
    """Return the currently staged images and clear the staging list."""
    paths = _pending_images["paths"]
    _pending_images["paths"] = []
    return paths


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
    if _ctx_limit_state["tokens"]:
        ctx_k += f" / {_ctx_limit_state['tokens'] // 1000} k"
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
    # Height tracks len(SLASH_COMMANDS): 3 + len(SLASH_COMMANDS) lines total
    # (rule, model/mode, session/ctx, one line per command hint) — adding a
    # slash command grows the toolbar by one row, which is accepted on
    # purpose (see docs/manual-testing.md).
    cmd_lines = matches + [""] * (len(SLASH_COMMANDS) - len(matches))
    n_staged = len(_pending_images["paths"])
    staged_hint = f"  |  {n_staged} image{'s' if n_staged != 1 else ''} staged" if n_staged else ""
    return [
        ("", rule + "\n"),
        (f"fg:{model_color} bold", f"Model: {_model_state['name']}"),
        ("", "  |  "),
        (f"fg:{color} bold", mode_label),
        ("", "\n"),
        (f"fg:{model_color} bold", f"Session ID: {_session_state['id']}"),
        ("", "  |  "),
        ("", ctx_k),
        ("", staged_hint),
        ("", "\n" + "\n".join(cmd_lines)),
    ]


def main(mode: str = MODE_APPROVAL, prompt: str | None = None,
         model: str | None = None, base_url: str | None = None, api_key: str | None = None,
         images: list[str] | None = None):
    console = Console()
    _mode_state["mode"] = mode
    _model_state["name"] = model or DEFAULT_MODEL
    _session_state["id"] = str(uuid.uuid4())
    _pending_images["paths"] = list(images or [])

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
        run_turn(llm_with_tools, messages, prompt, console, initial_cwd, mode,
                 images=_take_pending_images())
        return

    if not base_url:
        # Local Ollama only — cloud/litellm models have no equivalent
        # lookup. Fetched once here (not per turn, not per toolbar
        # re-render) since it's static for the life of the process.
        _ctx_limit_state["tokens"] = get_context_window(model)

    _toolbar_style = Style.from_dict({
        "bottom-toolbar": "bg:default fg:default noreverse",
    })
    session = PromptSession(
        bottom_toolbar=_get_toolbar, style=_toolbar_style, key_bindings=_kb,
        completer=AtPathCompleter(),
        # Tab-triggered only — the default (True) pops a completion menu on
        # every keystroke after "@" and fights the bottom toolbar for rows.
        complete_while_typing=False,
        # Default is 8 reserved rows, which is exactly the blank-line
        # artifact the toolbar's fixed-height padding (above) exists to
        # prevent. Keep this small and verify manually after any change
        # here — see docs/manual-testing.md.
        reserve_space_for_menu=4,
    )
    # prompt_toolkit's default timeoutlen (1.0s) waits after a lone Esc to
    # see if it's actually the start of an Alt-combo sequence (Alt+key is
    # sent as ESC + key on a raw terminal; prompt_toolkit's built-in emacs
    # bindings register several, e.g. Alt+F/Alt+B word nav, even though this
    # app's own key_bindings never use one). We don't rely on any of those,
    # so there's nothing worth waiting for — flush immediately.
    session.app.timeoutlen = 0

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

        if user_input.lower().startswith("/image"):
            sys.stdout.write("\x1b[A\x1b[2K")
            sys.stdout.flush()
            _stage_image(user_input[len("/image"):].strip(), console, initial_cwd)
            continue

        text, mentioned = parse_image_mentions(user_input)
        images = [resolve_image_path(p, initial_cwd)
                  for p in _take_pending_images() + mentioned]

        # Replace the typed line with the orange version
        sys.stdout.write("\x1b[A\x1b[2K")
        sys.stdout.flush()
        console.print(f"[bold orange1]> {escape(user_input)}[/bold orange1]")
        for p in images:
            console.print(f"[dim]  [image: {escape(_display_path(p, initial_cwd))}][/dim]")
        console.print()

        run_turn(llm_with_tools, messages, text, console, initial_cwd,
                 _mode_state["mode"], images=images)

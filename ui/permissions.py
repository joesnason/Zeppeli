"""Interactive confirmation for destructive tool calls (pre-tool hooks).

Approvals can be remembered so repeat calls stop re-prompting, at one of two
scopes, tracked separately and in-memory only (never persisted to disk):

- "turn"    — approved for the rest of the current REPL turn only; cleared by
              reset_turn_approvals() at the start of every new run_turn() call
- "session" — approved for the rest of the running process

Records are keyed by (tool_name, path) — approving a write to one file does
not approve a write to a different file.
"""

import pathlib

from rich.console import Console

MODE_APPROVAL = "approval"
MODE_YOLO = "yolo"
MODE_AUTO = "auto"
VALID_MODES = (MODE_APPROVAL, MODE_YOLO, MODE_AUTO)

_session_approved: set[tuple[str, str]] = set()
_turn_approved: set[tuple[str, str]] = set()


def _key(tool_name: str, args: dict) -> tuple[str, str]:
    return (tool_name, args.get("path", ""))


def _approved_scope(tool_name: str, args: dict) -> str | None:
    key = _key(tool_name, args)
    if key in _session_approved:
        return "session"
    if key in _turn_approved:
        return "turn"
    return None


def _record_approval(tool_name: str, args: dict, scope: str) -> None:
    key = _key(tool_name, args)
    (_session_approved if scope == "session" else _turn_approved).add(key)


def reset_turn_approvals() -> None:
    """Clear turn-scoped approvals. Called once per new turn so they don't
    leak into the next user input."""
    _turn_approved.clear()


def permission_ask(tool_name: str, args: dict, console: Console) -> bool:
    path = args.get("path", "")

    scope = _approved_scope(tool_name, args)
    if scope is not None:
        console.print(f"[dim]  ✓ auto-approved ({scope}): {tool_name} → {path}[/dim]")
        return True

    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    action = "delete" if tool_name == "delete_file" else "write to"
    console.print(f"[yellow]  AI wants to {action}:[/yellow] [bold]{path}[/bold]")

    options = [
        ("Yes", "turn"),
        ("Always allow (this session)", "session"),
        ("No", "deny"),
    ]
    state = {"idx": 2}  # default: No

    def get_tokens():
        tokens = []
        for i, (label, _) in enumerate(options):
            if i == state["idx"]:
                tokens += [("", f" ▶  {label}"), ("", "\n")]
            else:
                tokens += [("", f"    {label}"), ("", "\n")]
        return tokens

    kb = KeyBindings()

    @kb.add("up")
    def go_up(event):
        state["idx"] = (state["idx"] - 1) % len(options)

    @kb.add("down")
    def go_down(event):
        state["idx"] = (state["idx"] + 1) % len(options)

    @kb.add("enter")
    def confirm(event):
        event.app.exit(result=options[state["idx"]][1])

    @kb.add("c-c")
    def cancel(event):
        event.app.exit(result="deny")

    layout = Layout(Window(FormattedTextControl(get_tokens, focusable=True)))
    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    choice = app.run()

    if choice == "deny":
        return False
    _record_approval(tool_name, args, choice)
    return True


PRE_TOOL_HOOKS: dict[str, callable] = {
    "write_file": permission_ask,
    "delete_file": permission_ask,
}


def _is_within_cwd(path: str, cwd: str) -> bool:
    """True if `path` resolves to somewhere inside `cwd` (symlinks and `..`
    segments included, via Path.resolve())."""
    try:
        return pathlib.Path(path).resolve().is_relative_to(pathlib.Path(cwd).resolve())
    except (OSError, ValueError):
        return False


def _make_auto_hook(initial_cwd: str) -> callable:
    """Build an auto-mode hook: auto-approve when the path is inside
    `initial_cwd`, otherwise fall back to the normal interactive prompt."""

    def _auto_hook(tool_name: str, args: dict, console: Console) -> bool:
        path = args.get("path", "")
        if _is_within_cwd(path, initial_cwd):
            console.print(f"[dim]  ✓ auto-approved (auto-mode): {tool_name} → {path}[/dim]")
            return True
        return permission_ask(tool_name, args, console)

    return _auto_hook


def build_pre_tool_hooks(mode: str, initial_cwd: str) -> dict[str, callable]:
    """Return the PRE_TOOL_HOOKS-shaped dict to use for the given mode.

    - MODE_YOLO: no hooks at all — every call runs unguarded, identical to
      having no PRE_TOOL_HOOKS entries.
    - MODE_AUTO: write_file/delete_file auto-approve inside `initial_cwd`,
      otherwise fall back to permission_ask().
    - MODE_APPROVAL (default/unrecognized): today's behavior, unchanged.
    """
    if mode == MODE_YOLO:
        return {}
    if mode == MODE_AUTO:
        hook = _make_auto_hook(initial_cwd)
        return {"write_file": hook, "delete_file": hook}
    return dict(PRE_TOOL_HOOKS)

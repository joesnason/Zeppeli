"""Interactive confirmation for destructive tool calls (pre-tool hooks)."""

from rich.console import Console


def permission_ask(tool_name: str, args: dict, console: Console) -> bool:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    path = args.get("path", "")
    action = "delete" if tool_name == "delete_file" else "write to"
    console.print(f"[yellow]  AI wants to {action}:[/yellow] [bold]{path}[/bold]")

    options = [("Yes", True), ("No", False)]
    state = {"idx": 1}  # default: No

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
        event.app.exit(result=False)

    layout = Layout(Window(FormattedTextControl(get_tokens, focusable=True)))
    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    return app.run()


PRE_TOOL_HOOKS: dict[str, callable] = {
    "write_file": permission_ask,
    "delete_file": permission_ask,
}

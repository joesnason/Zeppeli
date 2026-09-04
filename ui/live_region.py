"""The persistent Application's live-content area.

`run_turn()`'s structure guarantees exactly one of these is ever active at
a time, never overlapping: the "Thinking..." spinner, live-updating
streamed Markdown, and the in-Layout Yes/No permission menu (streaming
happens, *then* — if there were tool calls — the whole tool loop runs
before streaming resumes; the permission menu only shows up during that
tool loop). So all three safely share one Window/content-provider instead
of needing separate regions.

Bridges Rich's actual Markdown renderer into prompt_toolkit-native
formatted text via an in-memory ANSI round-trip — no real-terminal writes
involved, confirmed working end-to-end (rich 15.0.0, prompt_toolkit
3.0.53):

    Console(file=io.StringIO(), force_terminal=True) -> ANSI(...).__pt_formatted_text__()

This is what lets the live region keep Rich's actual Markdown rendering
quality while living inside the same Application/Layout as the toolbar,
instead of `rich.live.Live`/`console.status()` (which cursor-reposition-
and-overwrite their own terminal region — a second renderer the toolbar's
Application can't safely coexist with)."""

import asyncio
import io

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console as RichConsole
from rich.live import Live as RichLive
from rich.markdown import Markdown as RichMarkdown

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]  # Rich's "dots" spinner, same frames
_SPINNER_INTERVAL = 0.1  # seconds/frame — matches Rich's own "dots" cadence
_MARKDOWN_UPDATE_INTERVAL = 1 / 15  # matches today's Live(refresh_per_second=15)
_MARKDOWN_RENDER_WIDTH = 100  # ANSI render width; the Window itself wraps again to terminal width


def _render_markdown_ansi(markdown_text: str) -> str:
    """Render `markdown_text` via Rich's actual Markdown renderer to an
    ANSI-escaped string, entirely in memory (no real-terminal write)."""
    buf = io.StringIO()
    console = RichConsole(file=buf, force_terminal=True, width=_MARKDOWN_RENDER_WIDTH,
                           color_system="truecolor")
    console.print(RichMarkdown(markdown_text))
    return buf.getvalue()


def render_markdown_frame(markdown_text: str) -> list[tuple[str, str]]:
    """Render `markdown_text` into prompt_toolkit-native (style, text)
    tuples, suitable as a Window's FormattedTextControl content. Pure and
    independently testable."""
    return ANSI(_render_markdown_ansi(markdown_text)).__pt_formatted_text__()


class _MenuState:
    """Pure state machine for an arrow-key Yes/No-style menu — same
    semantics as the old ui/permissions.py's _arrow_menu(): up/down wrap,
    confirm() returns the selected index, escape() jumps to (and returns)
    the last option (by convention always the "No"/"No, exit"), matching
    ui/CLAUDE.md's documented Esc behavior exactly. No prompt_toolkit
    dependency at all — testable with plain asserts."""

    def __init__(self, options: list[str], default_idx: int = 0):
        self.options = options
        self.idx = default_idx

    def up(self) -> None:
        self.idx = (self.idx - 1) % len(self.options)

    def down(self) -> None:
        self.idx = (self.idx + 1) % len(self.options)

    def confirm(self) -> int:
        return self.idx

    def escape(self) -> int:
        self.idx = len(self.options) - 1
        return self.idx

    def render(self) -> list[tuple[str, str]]:
        tokens = []
        for i, label in enumerate(self.options):
            marker = " ▶  " if i == self.idx else "    "
            tokens += [("", f"{marker}{label}"), ("", "\n")]
        return tokens


class LiveRegion:
    """Owns the persistent Application's live-content Window. Constructed
    once in ui/repl.py before the Application exists (the `app` handle is
    attached afterward via set_app(), since Application construction needs
    this object's get_content()/menu_active first)."""

    def __init__(self):
        self.app = None  # attached post-construction via set_app()
        self.menu_active = False  # gates the ConditionalKeyBindings ui/repl.py builds around menu_key_bindings
        self._content: list[tuple[str, str]] = []
        self._spinner_task: asyncio.Task | None = None
        self.menu_key_bindings = KeyBindings()
        self._menu_future: asyncio.Future | None = None
        self._menu_state: _MenuState | None = None
        self._install_menu_bindings()

    def set_app(self, app) -> None:
        self.app = app

    def _invalidate(self) -> None:
        if self.app is not None:
            self.app.invalidate()

    def get_content(self):
        """The live Window's FormattedTextControl content-provider. Empty
        (not even a blank line) when idle, so the Window's dynamic height
        collapses to 0 and doesn't leave a stray gap between the input line
        and whatever real scrollback sits above it."""
        return self._content

    def clear(self) -> None:
        self._content = []
        self._invalidate()

    def print_line(self, formatted_text) -> None:
        """Promote text to real scrollback. Existing plain `console.print()`
        calls elsewhere don't need this at all under `patch_stdout()` — this
        exists for the input echo in ui/repl.py, which used to rely on a
        raw ANSI cursor trick that can't survive a persistent Application,
        and for finalize_markdown() below."""
        print_formatted_text(formatted_text)

    # --- spinner ---------------------------------------------------------

    async def _spin(self, label: str) -> None:
        i = 0
        while True:
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            self._content = [("fg:#888888", f"{frame} {label}")]
            self._invalidate()
            i += 1
            await asyncio.sleep(_SPINNER_INTERVAL)

    def start_spinner(self, label: str = "Thinking...") -> None:
        self.stop_spinner()
        self._spinner_task = asyncio.create_task(self._spin(label))

    def stop_spinner(self) -> None:
        if self._spinner_task is not None:
            self._spinner_task.cancel()
            self._spinner_task = None

    # --- streamed markdown -------------------------------------------------

    def update_markdown(self, markdown_text: str) -> None:
        self._content = render_markdown_frame(markdown_text)
        self._invalidate()

    def finalize_markdown(self, markdown_text: str) -> None:
        """Promote the finished response to real scrollback and clear the
        live window — the same end state today's rich.live.Live leaves
        behind on exit."""
        if markdown_text:
            self.print_line(ANSI(_render_markdown_ansi(markdown_text)))
        self.clear()

    # --- permission menu ---------------------------------------------------

    def _install_menu_bindings(self) -> None:
        kb = self.menu_key_bindings

        @kb.add("up")
        def _(event):
            self._menu_state.up()
            self._content = self._menu_state.render()
            self._invalidate()

        @kb.add("down")
        def _(event):
            self._menu_state.down()
            self._content = self._menu_state.render()
            self._invalidate()

        @kb.add("enter")
        def _(event):
            if self._menu_future is not None and not self._menu_future.done():
                self._menu_future.set_result(self._menu_state.confirm())

        @kb.add("escape")
        def _(event):
            if self._menu_future is not None and not self._menu_future.done():
                self._menu_future.set_result(self._menu_state.escape())

        # No c-c binding here on purpose: ui/repl.py registers one single,
        # always-active Ctrl+C handler that exits the whole persistent
        # Application regardless of whether a menu is showing (per the
        # confirmed "Ctrl+C always cleanly shuts down the TUI" behavior) —
        # that app.exit() cancels the turn task, which unwinds a pending
        # ask_menu() await via its own try/finally (restoring focus/
        # key-bindings) without needing a separate menu-local handler.

    async def ask_menu(self, options: list[str], default_idx: int = 0) -> int | None:
        """Show an in-Layout Yes/No-style arrow-key menu, reusing the live
        Window (safe — see module docstring). Returns the chosen index, or
        None on Ctrl+C. Must run on the main event-loop thread (never
        wrapped in asyncio.to_thread()) since it manipulates this
        Application-wide state directly.

        Restores menu_active/focus/content in a finally block so a
        cancelled caller (e.g. the surrounding turn task being torn down)
        never leaves the input line permanently unresponsive."""
        self.stop_spinner()
        loop = asyncio.get_running_loop()
        self._menu_future = loop.create_future()
        self._menu_state = _MenuState(options, default_idx)
        self.menu_active = True
        previous_focus = None
        try:
            if self.app is not None:
                previous_focus = self.app.layout.current_window
                self.app.layout.focus(self.get_content_window())
            self._content = self._menu_state.render()
            self._invalidate()
            return await self._menu_future
        finally:
            self.menu_active = False
            self._menu_future = None
            self._menu_state = None
            self.clear()
            if self.app is not None and previous_focus is not None:
                self.app.layout.focus(previous_focus)

    def get_content_window(self):
        """The live-content Window itself, set by ui/repl.py after Layout
        construction (needed so ask_menu() can move focus there — a
        FormattedTextControl needs `focusable=True` for this)."""
        return self._content_window

    def set_content_window(self, window) -> None:
        self._content_window = window


class SimpleLive:
    """One-shot `-p` mode's stand-in for LiveRegion. No persistent
    Application/toolbar ever exists on that path (see ui/repl.py's main()),
    so there's nothing for `rich.live.Live`/`console.status()` to conflict
    with — this renders exactly the way the whole codebase did before this
    rewrite, keeping `-p` mode's terminal output byte-identical. Only
    exists so ui/streaming.py's stream_response()/ui/turn.py's run_turn()
    can share one async implementation across both entry points instead of
    maintaining two copies."""

    def __init__(self, console):
        self._console = console
        self._status_cm = None
        self._live_cm = None

    def start_spinner(self, label: str = "Thinking...") -> None:
        self._status_cm = self._console.status(f"[dim]{label}[/dim]", spinner="dots")
        self._status_cm.__enter__()

    def stop_spinner(self) -> None:
        if self._status_cm is not None:
            self._status_cm.__exit__(None, None, None)
            self._status_cm = None

    def update_markdown(self, markdown_text: str) -> None:
        if self._live_cm is None:
            self._live_cm = RichLive(RichMarkdown(markdown_text), console=self._console,
                                      refresh_per_second=15)
            self._live_cm.__enter__()
        else:
            self._live_cm.update(RichMarkdown(markdown_text))

    def finalize_markdown(self, markdown_text: str) -> None:
        if self._live_cm is not None:
            self._live_cm.__exit__(None, None, None)
            self._live_cm = None

    async def ask_menu(self, options: list[str], default_idx: int = 0) -> int | None:
        """Falls back to the old throwaway-Application arrow menu (still
        used unchanged by confirm_auto_mode_trust()'s one call site too) —
        safe here since no other Application is ever running on the `-p`
        path. Run in a worker thread since Application.run() is a blocking
        call that drives its own event-loop bootstrap, which can't happen
        directly on the thread already inside asyncio.run()."""
        from .permissions import _arrow_menu
        return await asyncio.to_thread(_arrow_menu, options, default_idx)

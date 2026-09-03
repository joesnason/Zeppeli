"""Interactive REPL: input prompt, toolbar, and the top-level turn loop."""

import os
import pathlib
import platform
import re
import shutil
import sys
import time
import uuid
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markup import escape
from rich.rule import Rule
from langchain_core.messages import SystemMessage

from core import (
    MODEL as DEFAULT_MODEL,
    SYSTEM_PROMPT,
    load_llm,
    get_context_window,
    model_supports_reasoning,
)
from core.eventlog import (
    build_turns_and_outputs,
    flush_pending_events,
    log_cli_error,
    log_run_completed,
    log_run_started,
    log_session_started,
)
from core.messages import extract_text
from core.sessions import (
    append_history_from_messages,
    create_session,
    finish_run_from_messages,
    flush_pending_writes,
    save_session,
    start_run,
)
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


def _run_and_persist(llm_with_tools, messages, user_input, console, initial_cwd,
                      mode, images, session, reasoning=False, context_window=None):
    """Run one turn via run_turn(), then record it into `session` and write
    it to disk. Wraps run_turn() rather than modifying it, so ui/turn.py
    keeps no knowledge of persistence — this is the only place that needs
    both `session` and the `messages` list to diff before/after the call.

    A "running" stub is appended and saved *before* run_turn() runs, so a
    process kill mid-turn (e.g. Ctrl+C, which today propagates uncaught out
    of run_turn()) leaves an honest on-disk record — that run just stays at
    status "running" with no completedAt, rather than vanishing. Any bug in
    this bookkeeping itself is swallowed: a persistence bug must never keep
    the user's actual turn (already completed by run_turn() above) from
    landing on screen.
    """
    start_index = len(messages)
    run = start_run()
    session.runs.append(run)
    save_session(session)
    log_run_started(_session_state["id"], run.id, user_input)

    t0 = time.monotonic()
    run_turn(llm_with_tools, messages, user_input, console, initial_cwd, mode, images=images,
              session_id=_session_state["id"], run_id=run.id, reasoning=reasoning,
              context_window=context_window)
    duration_ms = int((time.monotonic() - t0) * 1000)

    try:
        new_messages = messages[start_index:]
        if not new_messages:
            # e.g. an ImageError before anything was appended (run_turn()'s
            # earliest return) — nothing happened; drop the empty stub run
            # rather than leaving a phantom entry.
            session.runs.remove(run)
        else:
            finish_run_from_messages(run, new_messages, duration_ms)
            append_history_from_messages(session, messages, start_index)
            answer = extract_text(new_messages[-1].content) if run.status == "completed" else ""
            turns, model_outputs = build_turns_and_outputs(new_messages)
            log_run_completed(_session_state["id"], run.id, status=run.status, answer=answer,
                               stats=run.stats, turns=turns, model_outputs=model_outputs,
                               error=run.error)
        save_session(session)
    except Exception:
        pass


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

    initial_cwd = str(pathlib.Path.cwd())

    # Config is known immediately (from the params, not the loaded llm
    # object), so session_started can be logged before load_llm() is even
    # attempted — that call is itself a real failure point (e.g. a bad
    # litellm config), and the try/except below (which logs any exception
    # from here on as a cli_error event before re-raising) needs to wrap it
    # too, not just what comes after a successful load.
    provider = "litellm" if base_url else "ollama"
    ollama_url = None if base_url else os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    # ChatOllama accepts a per-call reasoning=True kwarg (ui/streaming.py's
    # stream_response()) that separates the model's thinking into
    # additional_kwargs["reasoning_content"] instead of folding it into the
    # visible response — see docs/logging.md. No equivalent exists for
    # cloud/self-hosted models via litellm (--base-url). For local Ollama,
    # model_supports_reasoning() checks the model's own advertised
    # capabilities (via ollama.show()) up front, so an unsupported model is
    # never asked in the first place — ui/streaming.py's stream_response()
    # still carries a reactive fallback too, as a safety net.
    if base_url is not None:
        reasoning_enabled = False
        reasoning_mode = "unavailable"
    elif model_supports_reasoning(model):
        reasoning_enabled = True
        reasoning_mode = "enabled"
    else:
        reasoning_enabled = False
        reasoning_mode = "unsupported"
    log_session_started(_session_state["id"], cwd=initial_cwd, provider=provider,
                         model=_model_state["name"], ollama_url=ollama_url,
                         pid=os.getpid(), platform=platform.system().lower(),
                         reasoning_mode=reasoning_mode)

    try:
        llm_with_tools = load_llm(model=model, base_url=base_url, api_key=api_key)
        messages = [SystemMessage(content=SYSTEM_PROMPT + f"\n\nWorking directory: {initial_cwd}")]

        # Created once per process and written immediately (0 runs, 0
        # history) so even a session that never completes a turn still
        # leaves a file. Same code path for interactive and one-shot -p —
        # see docs/sessions.md. Named history_session (not `session`)
        # because the PromptSession object below is conventionally called
        # `session` in this REPL loop already.
        history_session = create_session(_session_state["id"], initial_cwd, _model_state["name"])
        save_session(history_session)

        # Local Ollama only — cloud/litellm models have no equivalent
        # lookup. Fetched once here (not per turn), for both the toolbar
        # display below and tier-2 compaction's token budget
        # (core/messages.py's compact_messages_to_budget()) — including in
        # one-shot -p mode, which has no toolbar but still needs a real
        # number rather than always falling back to the 256k default.
        context_window = get_context_window(model) if not base_url else None
        _ctx_limit_state["tokens"] = context_window

        if prompt is not None:
            # One-shot mode: run exactly one turn and exit — no
            # PromptSession/toolbar (both assume an interactive terminal)
            # and no REPL loop.
            _run_and_persist(llm_with_tools, messages, prompt, console, initial_cwd, mode,
                              images=_take_pending_images(), session=history_session,
                              reasoning=reasoning_enabled, context_window=context_window)
            # -p is meant to be deterministic/scriptable — a caller may read
            # the session file the instant this process exits, so flush
            # explicitly rather than relying on atexit's timing. The
            # interactive REPL loop below doesn't get an equivalent
            # per-turn flush (that would defeat the point of queuing at
            # all) — it relies on atexit when it exits.
            flush_pending_writes()
            flush_pending_events()
            return

        _toolbar_style = Style.from_dict({
            "bottom-toolbar": "bg:default fg:default noreverse",
        })
        session = PromptSession(
            bottom_toolbar=_get_toolbar, style=_toolbar_style, key_bindings=_kb,
            completer=AtPathCompleter(),
            # Tab-triggered only — the default (True) pops a completion
            # menu on every keystroke after "@" and fights the bottom
            # toolbar for rows.
            complete_while_typing=False,
            # Default is 8 reserved rows, which is exactly the blank-line
            # artifact the toolbar's fixed-height padding (above) exists to
            # prevent. Keep this small and verify manually after any change
            # here — see docs/manual-testing.md.
            reserve_space_for_menu=4,
        )
        # prompt_toolkit's default timeoutlen (1.0s) waits after a lone Esc
        # to see if it's actually the start of an Alt-combo sequence
        # (Alt+key is sent as ESC + key on a raw terminal; prompt_toolkit's
        # built-in emacs bindings register several, e.g. Alt+F/Alt+B word
        # nav, even though this app's own key_bindings never use one). We
        # don't rely on any of those, so there's nothing worth waiting for
        # — flush immediately.
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

            _run_and_persist(llm_with_tools, messages, text, console, initial_cwd,
                              _mode_state["mode"], images=images, session=history_session,
                              reasoning=reasoning_enabled, context_window=context_window)
    except Exception as e:
        # Genuine unexpected bugs only — the loop above already catches
        # KeyboardInterrupt/EOFError itself (a clean exit, not an error) and
        # never lets them escape to here. Re-raise after logging so today's
        # crash/exit-code behavior is unchanged; this only adds a record.
        log_cli_error(_session_state["id"], e)
        raise

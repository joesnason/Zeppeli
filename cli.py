import json
import os
import pathlib
import shutil
import subprocess
import sys
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown as RichMarkdown
from rich.markup import escape
from rich.rule import Rule
from langchain_ollama import ChatOllama
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

MODEL = "gemma4:26b-nvfp4"  # for Mac Mini 64G
# MODEL = "gemma4:e2b"  # for MacBook Air 8G
RG_BIN = str(pathlib.Path(__file__).parent / "bin" / "rg")

SLASH_COMMANDS = ["/exit", "/quit"]

_ctx_state = {"tokens": 0}


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

SYSTEM_PROMPT = """You are a helpful assistant with access to the following tools:

- list_files(path): List files and directories at a given path using ls -la.
- glob_files(pattern, cwd): Find files matching a glob pattern (supports ** for recursive search). Default cwd is ".".
- rg_search(pattern, path, glob): Search file contents using ripgrep (regex supported). Use glob to filter by filename (e.g. "*.py"). Default path is ".".
- read_file(path, offset, limit, max_lines, max_bytes): Read a file in chunks of up to 400 lines starting at line offset. Stops when max_lines (default 10000) or max_bytes (default 98304 = 96KB) is reached. Use offset from the returned hint to paginate through large files.
- write_file(path, content): Write content to a file, creating it if it doesn't exist or replacing all its content.

Use these tools when the user asks about files, directories, folder contents, searching within files, or writing/creating files.
For questions unrelated to the filesystem, answer directly without using any tool."""


@tool
def list_files(path: str = ".") -> str:
    """List files and directories at the given path using ls -la."""
    result = subprocess.run(["ls", "-la", path], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else f"Error: {result.stderr}"


@tool
def glob_files(pattern: str, cwd: str = ".") -> str:
    """Find files matching a glob pattern using Node.js fs.glob. Supports ** for recursive matching."""
    script = f"""
const {{ glob }} = require('node:fs/promises');
(async () => {{
  const results = [];
  for await (const f of glob({json.dumps(pattern)}, {{ cwd: {json.dumps(cwd)} }})) results.push(f);
  console.log(results.join('\\n') || '(no matches)');
}})().catch(e => {{ process.stderr.write(e.message + '\\n'); process.exit(1); }});
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        return f"Error: {result.stderr.strip()}"
    return result.stdout.strip()


@tool
def rg_search(pattern: str, path: str = ".", glob: str = "") -> str:
    """Search file contents using ripgrep. Supports regex. Use glob to filter by filename (e.g. '*.py')."""
    cmd = [RG_BIN, "--no-heading", "--color=never", pattern, path]
    if glob:
        cmd += ["--glob", glob]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 2:
        return f"Error: {result.stderr.strip()}"
    return result.stdout.strip() or "(no matches)"


@tool
def read_file(path: str, offset: int = 0, limit: int = 400,
              max_lines: int = 10000, max_bytes: int = 98304) -> str:
    """Read a file in chunks of up to 400 lines starting at line `offset` (0-indexed).
    Stops early when either max_lines lines or max_bytes bytes have been read.
    The returned footer tells you whether more content is available and the next offset to use."""
    limit = min(limit, 400)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i in range(offset):
                if not f.readline():
                    return f"[read_file] Error: offset {offset} exceeds file length ({i} lines)"

            lines: list[str] = []
            total_bytes = 0
            truncated_by: str | None = None

            for _ in range(limit):
                if len(lines) >= max_lines:
                    truncated_by = "max_lines"
                    break
                line = f.readline()
                if not line:
                    break
                line_bytes = len(line.encode())
                if total_bytes + line_bytes > max_bytes:
                    truncated_by = "max_bytes"
                    break
                lines.append(line)
                total_bytes += line_bytes

            has_more = bool(f.readline())

        end_line = offset + len(lines)
        header = f"[File: {path} | lines {offset + 1}–{end_line} | {total_bytes} bytes]"
        if truncated_by:
            footer = f"[Stopped: {truncated_by} limit reached at line {end_line}]"
        elif has_more:
            footer = f"[More available: use offset={end_line} to continue]"
        else:
            footer = "[End of file]"
        return header + "\n" + "".join(lines) + footer

    except FileNotFoundError:
        return f"[read_file] Error: file not found: {path}"
    except Exception as e:
        return f"[read_file] Error: {e}"


@tool
def write_file(path: str, content: str) -> str:
    """Write content to a file, creating it if it does not exist or replacing all existing content."""
    try:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


PATH_ARGS = {
    "list_files": ["path"],
    "glob_files": ["cwd"],
    "rg_search": ["path"],
    "read_file": ["path"],
    "write_file": ["path"],
}


def resolve_paths(tool_name: str, args: dict, cwd: str) -> dict:
    args = dict(args)
    for key in PATH_ARGS.get(tool_name, []):
        if key in args:
            p = pathlib.Path(args[key]).expanduser()
            if not p.is_absolute():
                p = pathlib.Path(cwd) / p
            args[key] = str(p)
    return args


def permission_ask(tool_name: str, args: dict, console: Console) -> bool:
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    path = args.get("path", "")
    console.print(f"[yellow]  AI wants to write to:[/yellow] [bold]{path}[/bold]")

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
}


def stream_response(llm_with_tools, messages, console):
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


def run_turn(llm_with_tools, messages, user_input, console, initial_cwd: str = "."):
    messages.append(HumanMessage(content=user_input))
    response = stream_response(llm_with_tools, messages, console)
    messages.append(response)
    _update_ctx(response)

    tools = {t.name: t for t in [list_files, glob_files, rg_search, read_file, write_file]}
    while response.tool_calls:
        for tc in response.tool_calls:
            info = escape(f"[tool: {tc['name']}({tc['args']})]")
            console.print(f"[dim]  {info}[/dim]")
            resolved_args = resolve_paths(tc["name"], tc["args"], initial_cwd)
            hook = PRE_TOOL_HOOKS.get(tc["name"])
            if hook is not None and not hook(tc["name"], resolved_args, console):
                result = f"[{tc['name']}] Cancelled by user."
            else:
                result = tools[tc["name"]].invoke(resolved_args)
            messages.append(ToolMessage(content=str(result), tool_call_id=tc["id"]))
        response = stream_response(llm_with_tools, messages, console)
        messages.append(response)
        _update_ctx(response)


def main():
    console = Console()
    llm = ChatOllama(model=MODEL)
    llm_with_tools = llm.bind_tools([list_files, glob_files, rg_search, read_file, write_file])
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


if __name__ == "__main__":
    main()

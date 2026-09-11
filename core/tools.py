"""Tool definitions and path resolution — the AI's filesystem capabilities.

All @tool functions here are pure: no console/UI dependencies. They're bound
to the model in core/agent.py and invoked by the UI layer's turn loop.
"""

import json
import pathlib
import platform
import shutil
import subprocess
from langchain_core.tools import tool

# core/tools.py lives one directory below the repo root, so climb one level
# to find bin/ regardless of the caller's cwd.
_BIN_DIR = pathlib.Path(__file__).parent.parent / "bin"

# Which bundled binary (if any) matches the current platform/architecture.
# platform.machine() spells arm64 differently across OS/Python versions
# (e.g. "arm64" on macOS, sometimes "aarch64" elsewhere), so both are
# mapped to the same darwin binary.
_BUNDLED_RG_BY_PLATFORM = {
    ("Darwin", "arm64"): "rg-darwin-arm64",
    ("Darwin", "aarch64"): "rg-darwin-arm64",
    ("Linux", "x86_64"): "rg-linux-x86_64",
}


def _find_rg_bin() -> str | None:
    """Resolve which ripgrep binary rg_search() should invoke: prefer a
    system-installed `rg` on PATH (works for any platform/architecture
    the user has it installed for, including ones not bundled here),
    falling back to a bundled binary matching the current platform/
    architecture (zero-setup on macOS arm64 and Linux x86_64 — see
    bin/). Returns None if neither exists, so rg_search() can report a
    clear, actionable error instead of crashing on an OSError from a
    wrong-platform binary (e.g. "Exec format error" from trying to run
    a macOS binary on Linux, or vice versa)."""
    system_rg = shutil.which("rg")
    if system_rg:
        return system_rg
    bundled_name = _BUNDLED_RG_BY_PLATFORM.get((platform.system(), platform.machine()))
    if bundled_name:
        bundled = _BIN_DIR / bundled_name
        if bundled.is_file():
            return str(bundled)
    return None


RG_BIN = _find_rg_bin()  # resolved once at import; may be None — see rg_search()


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
def rg_search(pattern: str, path: str = ".", glob: str = "", max_bytes: int = 50000) -> str:
    """Search file contents using ripgrep. Supports regex. Use glob to filter by filename (e.g. '*.py').
    Output is capped at max_bytes (default 50000) so a broad match against a huge file (e.g. a build
    log) can't blow out the model's context window in one call — narrow the pattern or glob if truncated."""
    if RG_BIN is None:
        return (
            "Error: ripgrep ('rg') isn't available for this platform. Install it — "
            "macOS: `brew install ripgrep`; Debian/Ubuntu: `sudo apt install ripgrep`; "
            "Fedora: `sudo dnf install ripgrep` — then restart."
        )
    cmd = [RG_BIN, "--no-heading", "--color=never", pattern, path]
    if glob:
        cmd += ["--glob", glob]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        # A launch failure (wrong-platform binary, corrupted file,
        # permissions) never even produces a returncode — subprocess.run
        # raises instead. Report it as a normal tool-error string rather
        # than letting it propagate and crash the whole process.
        return f"Error: couldn't run ripgrep at {RG_BIN}: {e}"
    if result.returncode == 2:
        return f"Error: {result.stderr.strip()}"
    output = result.stdout.strip()
    if not output:
        return "(no matches)"
    output_bytes = output.encode()
    if len(output_bytes) > max_bytes:
        output = (
            output_bytes[:max_bytes].decode(errors="ignore")
            + f"\n[Output truncated at {max_bytes} bytes — narrow the pattern or "
              "glob to see fewer, more targeted matches.]"
        )
    return output


@tool
def read_file(path: str, offset: int = 0, limit: int = 400,
              max_lines: int = 10000, max_bytes: int = 98304) -> str:
    """Read a file in chunks of up to 400 lines starting at line `offset` (0-indexed).
    Stops early when either max_lines lines or max_bytes bytes have been read.
    The returned footer tells you whether more content is available and the next offset to use.
    A single line longer than max_bytes is truncated and still counted as
    read (rather than returning nothing) so a later call always advances
    past it instead of repeating the same stop forever."""
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
                    # Include a truncated slice of this line (if any budget
                    # remains) rather than nothing, and count it as consumed
                    # — otherwise a line alone bigger than max_bytes would
                    # leave `lines` empty, `end_line` would equal `offset`
                    # unchanged, and every future call at this same offset
                    # would repeat this exact failure forever.
                    remaining = max_bytes - total_bytes
                    if remaining > 0:
                        encoded = line.encode()
                        kept = encoded[:remaining].decode(errors="replace")
                        omitted = len(encoded) - len(kept.encode())
                        lines.append(f"{kept}\n[line truncated, {omitted} more bytes]\n")
                        total_bytes += len(kept.encode())
                    truncated_by = "max_bytes"
                    break
                lines.append(line)
                total_bytes += line_bytes

            has_more = bool(f.readline())

        end_line = offset + len(lines)
        header = f"[File: {path} | lines {offset + 1}–{end_line} | {total_bytes} bytes]"
        if truncated_by:
            # As explicit as the "more available" footer below — a stop
            # due to a hard limit still has more to read (the file isn't
            # actually finished), so the next offset must be spelled out
            # just as plainly, or a model may not reliably continue.
            footer = f"[Stopped: {truncated_by} limit reached — use offset={end_line} to continue]"
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
def tail_file(path: str, lines: int = 100, max_bytes: int = 98304) -> str:
    """Read the last `lines` lines (default 100) of a file, e.g. to see the
    most recent entries in a log. Reads at most max_bytes (default 98304 =
    96KB) from the end of the file, so it stays fast and memory-bounded even
    on multi-GB files — it never loads the whole file into memory. If the
    file has more content before that max_bytes window, the footer says so
    and suggests raising max_bytes; if the window reached the true start of
    the file (or the file has fewer than `lines` lines total), the footer
    says that too."""
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return f"[tail_file] Error: file not found: {path}"
        if p.is_dir():
            return f"[tail_file] Error: {path} is a directory, not a file"

        filesize = p.stat().st_size
        seek_pos = max(0, filesize - max_bytes)
        with open(path, "rb") as f:
            f.seek(seek_pos)
            raw = f.read()  # bounded to <= max_bytes bytes

        text = raw.decode("utf-8", errors="replace")

        if seek_pos > 0:
            # Seeked into the middle of the file, so the first line of this
            # window is very likely partial (cut mid-line by the seek) —
            # drop it, the same way real `tail` discards a partial leading
            # line after a backward seek. If the window has no newline at
            # all (max_bytes smaller than the file's actual last line), the
            # whole window is that one partial line — dropping it yields
            # zero lines here rather than a corrupted fragment; the footer
            # below tells the model to raise max_bytes. Unlike read_file's
            # oversized-line handling (which keeps a truncated slice), this
            # is a deliberate all-or-nothing drop: there's no offset/
            # pagination state here that could get stuck in a loop, so the
            # fix is simply "call again with a larger max_bytes."
            nl_idx = text.find("\n")
            text = text[nl_idx + 1:] if nl_idx != -1 else ""

        # Dropping the first line whenever seek_pos > 0 is unconditional,
        # even on the rare exact-line-boundary seek — the same one-line-
        # short bias real `tail` implementations accept.
        all_lines = text.splitlines(keepends=True) if text else []
        kept = all_lines[-lines:] if lines > 0 else []
        kept_count = len(kept)
        content = "".join(kept)
        total_bytes = len(content.encode("utf-8", errors="replace"))

        header = f"[File: {path} | last {kept_count} lines | {total_bytes} bytes]"
        if kept_count >= lines:
            footer = f"[Showing last {kept_count} lines]"
        elif seek_pos == 0:
            footer = f"[Beginning of file reached — file has only {kept_count} lines]"
        else:
            footer = (
                f"[Only found {kept_count} of requested {lines} lines within "
                f"the last {max_bytes} bytes of the file — increase max_bytes "
                "to search further back]"
            )
        # header + "\n" + content + footer, no separator before the footer —
        # matches read_file's existing join style, including the same
        # no-trailing-newline glue quirk read_file already has today.
        return header + "\n" + content + footer

    except Exception as e:
        return f"[tail_file] Error: {e}"


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


@tool
def delete_file(path: str) -> str:
    """Delete a file. Refuses to delete directories."""
    try:
        p = pathlib.Path(path)
        if not p.exists():
            return f"[delete_file] Error: file not found: {path}"
        if p.is_dir():
            return f"[delete_file] Error: {path} is a directory, not a file"
        p.unlink()
        return f"Deleted {path}"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [list_files, glob_files, rg_search, read_file, tail_file, write_file, delete_file]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

PATH_ARGS = {
    "list_files": ["path"],
    "glob_files": ["cwd"],
    "rg_search": ["path"],
    "read_file": ["path"],
    "tail_file": ["path"],
    "write_file": ["path"],
    "delete_file": ["path"],
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

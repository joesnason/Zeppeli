"""Tool definitions and path resolution — the AI's filesystem capabilities.

All @tool functions here are pure: no console/UI dependencies. They're bound
to the model in core/agent.py and invoked by the UI layer's turn loop.
"""

import json
import pathlib
import subprocess
from langchain_core.tools import tool

# core/tools.py lives one directory below the repo root, so climb one level
# to find bin/rg regardless of the caller's cwd.
RG_BIN = str(pathlib.Path(__file__).parent.parent / "bin" / "rg")


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


TOOLS = [list_files, glob_files, rg_search, read_file, write_file, delete_file]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

PATH_ARGS = {
    "list_files": ["path"],
    "glob_files": ["cwd"],
    "rg_search": ["path"],
    "read_file": ["path"],
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

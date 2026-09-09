"""Slack file attachments (e.g. log files): download, save under
`allowed_dir`, and build a short preview note to fold into the turn's
text. Lives here (not `core/`) because downloading via Slack's
`url_private_download` + bot-token auth is Slack-specific plumbing — the
remote-fetch equivalent of `core/images.py`'s *local* image-path
handling, which stays UI/platform-independent.

Design: rather than inventing a new "read more of this file" tool, the
file is saved to a real path inside `allowed_dir` and the model is told
that path plus a tail preview (a log's most relevant lines are usually
its last ones) — if that's not enough, the model already has
`read_file(path, offset=...)`/`rg_search(pattern, path=...)` and can page
through the rest itself (see core/tools.py).
"""

import pathlib
import re

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB — generous for logs, caps abuse/disk growth
PREVIEW_LINES = 200                     # tail lines shown in the initial note
PREVIEW_MAX_CHARS = 8000                # bounds the preview even if lines are very long
MAX_ATTACHMENTS_PER_MESSAGE = 3

ATTACHMENTS_SUBDIR = ".slack_attachments"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class AttachmentError(Exception):
    """Any attachment failure — unsupported type, too large, download
    failure. Message is meant to be shown to the user as-is."""


# Slack's mimetype detection isn't reliable for less common text
# extensions — a plain-text .log file is commonly reported as the
# generic application/octet-stream rather than text/plain (observed in
# practice), so the mimetype check alone rejects real log files. This
# extension allowlist is the fallback for exactly that case.
_TEXT_EXTENSIONS = {
    ".log", ".txt", ".csv", ".tsv", ".json", ".md", ".markdown",
    ".yml", ".yaml", ".ini", ".conf", ".cfg", ".env", ".xml",
}


def _is_supported(file_info: dict) -> bool:
    """True if Slack reports this file as some text/* mimetype, OR its
    filename has a well-known text-file extension (see _TEXT_EXTENSIONS
    and its comment above for why the extension check is necessary, not
    just a nicety). Images/archives/other binaries are refused."""
    if (file_info.get("mimetype") or "").startswith("text/"):
        return True
    name = file_info.get("name") or ""
    return pathlib.Path(name).suffix.lower() in _TEXT_EXTENSIONS


def _sanitize_filename(name: str) -> str:
    """Strip anything that isn't alphanumeric/./_/- so a crafted Slack
    filename (path separators, "..") can't escape dest_dir."""
    base = pathlib.Path(name or "attachment").name  # drop any directory components
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", base)
    return cleaned or "attachment"


def attachments_dir(allowed_dir: str) -> pathlib.Path:
    d = pathlib.Path(allowed_dir) / ATTACHMENTS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_note(path: pathlib.Path, text: str) -> str:
    lines = text.splitlines()
    total = len(lines)
    preview_lines = lines[-PREVIEW_LINES:]
    preview = "\n".join(preview_lines)
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[-PREVIEW_MAX_CHARS:]
    start_offset = max(total - len(preview_lines), 0)
    return (
        f'📎 Saved attachment: `{path}` ({total} lines total). '
        f"Showing the last {len(preview_lines)} lines below — if you need "
        f'earlier content, call read_file(path="{path}", offset=<N>) or '
        f'rg_search(pattern=..., path="{path}") on it directly.\n\n'
        f"```\n{preview}\n```"
    )


async def process_attachment(file_info: dict, *, bot_token: str, session, dest_dir: pathlib.Path) -> str:
    """Download, validate, save, and summarize one Slack file attachment.
    Raises AttachmentError on any failure — unsupported type, too large
    (per Slack's declared size or the actual downloaded size), or a
    download/HTTP failure. Never leaves a partial file on disk on
    failure."""
    if not _is_supported(file_info):
        raise AttachmentError(
            f'unsupported file type ({file_info.get("mimetype", "unknown")}) — '
            f'only text files (logs, .txt, .csv, .json, .md, etc.) are supported'
        )

    declared_size = file_info.get("size")
    if isinstance(declared_size, int) and declared_size > MAX_DOWNLOAD_BYTES:
        raise AttachmentError(
            f"file too large: {declared_size // 1024} KB "
            f"(max {MAX_DOWNLOAD_BYTES // 1024} KB)"
        )

    url = file_info.get("url_private_download") or file_info.get("url_private")
    if not url:
        raise AttachmentError("Slack didn't provide a download URL for this file")

    try:
        async with session.get(url, headers={"Authorization": f"Bearer {bot_token}"}) as resp:
            if resp.status != 200:
                raise AttachmentError(f"download failed (HTTP {resp.status})")
            data = await resp.read()
    except AttachmentError:
        raise
    except Exception as e:
        raise AttachmentError(f"download failed: {e}") from e

    if len(data) > MAX_DOWNLOAD_BYTES:
        raise AttachmentError(
            f"file too large: {len(data) // 1024} KB "
            f"(max {MAX_DOWNLOAD_BYTES // 1024} KB)"
        )

    text = data.decode("utf-8", errors="replace")
    filename = f'{file_info.get("id", "file")}_{_sanitize_filename(file_info.get("name", "attachment"))}'
    path = dest_dir / filename
    path.write_text(text)

    return _build_note(path, text)

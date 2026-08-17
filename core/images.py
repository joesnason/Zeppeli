"""Image attachment support — path resolution, @-mention parsing, downscaling,
and LangChain content-block construction. No UI dependencies.

Design notes (see docs/models.md "Vision / image input" for the full story):
- One content-block format serves both backends: langchain_ollama strips the
  data:...;base64, prefix itself (chat_models.py's _convert_messages_to_ollama_messages),
  and ChatLiteLLM passes {"type": "image_url", "image_url": {"url": ...}} through
  unchanged for the hosted_vllm/OpenAI-compatible path (verified against the
  installed langchain_litellm package — it isn't a recognized "data content
  block" in langchain_core's newer typed system, so it falls through the
  "pass through standard text blocks or other unrecognized dict formats
  unchanged" branch of _convert_message_to_dict).
- build_message_content() returns a plain str when there are no images, never
  a single-element list — langchain_ollama's list branch prepends a newline
  to every text part it sees, so always-list would change behavior for every
  existing text-only prompt.
"""

import base64
import pathlib
import re

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

MAX_LONG_EDGE = 1568           # px, long-edge cap after downscaling
MAX_SOURCE_BYTES = 20 * 1024 * 1024   # refuse to even open anything bigger
MAX_ENCODED_BYTES = 4 * 1024 * 1024   # base64 payload cap, post-downscale
MAX_IMAGES = 4                 # per message

# Passthrough ceiling when Pillow isn't installed — small images still work.
_RAW_FALLBACK_BYTES = 2 * 1024 * 1024

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# Preceded by nothing, whitespace, "(", or "[" — excludes joesnason@gmail.com,
# foo@bar, and other non-mention @ usages. Body accepts backslash-escaped
# spaces, since a dragged-in macOS Terminal path looks like
# "/Users/x/Screen\ Shot\ 2026-08-17.png".
_MENTION_RE = re.compile(r"(?<![^\s(\[])@((?:\\.|[^\s])+)")
_TRAILING_PUNCT = ",.;:!?)]}'\"、。，"


class ImageError(Exception):
    """Any image attachment failure — path, size, format. Message is meant
    to be shown to the user as-is."""


def is_image_path(path: str) -> bool:
    """True if the path's extension is a supported image type. Does not
    touch the filesystem."""
    return pathlib.Path(path).suffix.lower() in IMAGE_EXTS


def resolve_image_path(path: str, cwd: str) -> str:
    """Resolve a possibly-relative image path against cwd. Mirrors
    core/tools.py's resolve_paths() exactly (expanduser, then join cwd if
    relative) so an @-mention and an equivalent read_file() call land on the
    same absolute path. Idempotent: resolving an already-absolute path
    returns it unchanged."""
    p = pathlib.Path(path).expanduser()
    if not p.is_absolute():
        p = pathlib.Path(cwd) / p
    return str(p)


def parse_image_mentions(text: str) -> tuple[str, list[str]]:
    """Find @path mentions of image files in text. Returns (new_text, paths)
    where new_text has the leading '@' stripped from each recognized mention
    (the path itself is kept — it's still useful context for the model, and
    stripping it entirely would make multi-image prompts like
    "compare @a.png and @b.png" ambiguous).

    A mention only "counts" — sigil stripped, path collected — if it has a
    recognized image extension. Anything else (email addresses, @tool,
    @decorator mentions, python code) is left completely untouched: this repo's
    own README documents a prompt containing "@tool" as an example, and
    warning/erroring on unrecognized @ tokens would break it.
    """
    paths: list[str] = []

    def _replace(m: re.Match) -> str:
        raw = m.group(1)
        trimmed = raw.rstrip(_TRAILING_PUNCT)
        trailing = raw[len(trimmed):]
        token = re.sub(r"\\(.)", r"\1", trimmed)
        if not is_image_path(token):
            return m.group(0)
        paths.append(token)
        return token + trailing

    new_text = _MENTION_RE.sub(_replace, text)
    return new_text, paths


def load_image_block(
    path: str,
    *,
    max_long_edge: int = MAX_LONG_EDGE,
    max_source_bytes: int = MAX_SOURCE_BYTES,
    max_encoded_bytes: int = MAX_ENCODED_BYTES,
) -> dict:
    """Read, validate, downscale, and base64-encode a single image file into
    an OpenAI-style content block: {"type": "image_url", "image_url": {"url": "data:...;base64,..."}}.
    Raises ImageError on any failure — missing file, directory, unsupported
    extension, oversize source, corrupt image data, or still-too-big after
    downscaling."""
    p = pathlib.Path(path)
    if p.is_dir():
        raise ImageError(f"{path} is a directory, not an image")
    if not p.is_file():
        raise ImageError(f"image not found: {path}")
    if not is_image_path(path):
        raise ImageError(
            f"unsupported image type: {path} "
            f"(supported: {', '.join(sorted(IMAGE_EXTS))})"
        )
    size = p.stat().st_size
    if size > max_source_bytes:
        raise ImageError(
            f"image too large: {path} is {size // 1024} KB "
            f"(max {max_source_bytes // 1024} KB)"
        )

    raw = p.read_bytes()
    try:
        data, mime = _downscale(raw, p.suffix.lower(), max_long_edge, max_encoded_bytes)
    except ImageError:
        raise
    except Exception as e:
        raise ImageError(f"couldn't read image data in {path}: {e}") from e

    b64 = base64.b64encode(data).decode()
    if len(b64) > max_encoded_bytes:
        raise ImageError(
            f"image still {len(b64) // 1024} KB after downscaling "
            f"(max {max_encoded_bytes // 1024} KB) — crop it first"
        )
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _downscale(raw: bytes, suffix: str, max_long_edge: int, max_encoded_bytes: int) -> tuple[bytes, str]:
    """Downscale image bytes to fit under max_encoded_bytes (approximately,
    accounting for base64 expansion), trying progressively smaller sizes.
    Never upscales. Falls back to raw passthrough if Pillow isn't installed
    and the source is small enough."""
    try:
        from PIL import Image, UnidentifiedImageError  # lazy: keep Pillow optional
    except ImportError:
        if len(raw) > _RAW_FALLBACK_BYTES:
            raise ImageError(
                "Pillow isn't installed, so this image can't be downscaled "
                "(pip3 install Pillow). Attach an image under "
                f"{_RAW_FALLBACK_BYTES // 1024 // 1024} MB instead, or install Pillow."
            )
        return raw, _MIME_BY_EXT.get(suffix, "image/png")

    import io

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except UnidentifiedImageError as e:
        raise ImageError(f"not a valid image file: {e}") from e

    has_alpha = im.mode in ("RGBA", "LA") or (
        im.mode == "P" and "transparency" in im.info
    )

    out = raw
    mime = _MIME_BY_EXT.get(suffix, "image/png")
    for long_edge, quality in ((max_long_edge, 85), (1024, 75), (768, 60)):
        work = im.copy()
        if max(work.size) > long_edge:  # never upscale
            work.thumbnail((long_edge, long_edge), Image.LANCZOS)

        buf = io.BytesIO()
        if has_alpha:
            work.save(buf, format="PNG", optimize=True)
            mime = "image/png"
        else:
            if work.mode != "RGB":
                work = work.convert("RGB")
            work.save(buf, format="JPEG", quality=quality, optimize=True)
            mime = "image/jpeg"
        out = buf.getvalue()

        # *4//3 approximates base64 expansion so we don't decode until we
        # have to; load_image_block() double-checks the real encoded length.
        if len(out) * 4 // 3 <= max_encoded_bytes:
            return out, mime

    return out, mime  # caller (load_image_block) raises if still too big


def build_message_content(
    text: str,
    image_paths: list[str],
    cwd: str = ".",
    *,
    max_images: int = MAX_IMAGES,
) -> str | list:
    """Build LangChain HumanMessage content from text plus a list of image
    paths (already-@-mentioned or /image-staged, resolved or not — this
    resolves them). Returns a plain str when image_paths is empty (byte-
    identical to today's behavior — see module docstring), otherwise a list
    of image blocks followed by one text block. Paths are deduped by
    resolved absolute path, preserving first-seen order, so the same image
    staged and @-mentioned is only sent once."""
    if not image_paths:
        return text

    seen: set[str] = set()
    blocks: list[dict] = []
    for raw in image_paths:
        path = resolve_image_path(raw, cwd)
        if path in seen:
            continue
        seen.add(path)
        if len(blocks) >= max_images:
            raise ImageError(f"too many images attached (max {max_images})")
        blocks.append(load_image_block(path))

    return blocks + [{"type": "text", "text": text}]

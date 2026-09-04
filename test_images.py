"""Automated tests for core/images.py: @-mention parsing, path resolution,
downscaling, and message-content construction. No network/Ollama dependency.
Style matches test_tools.py / test_streaming.py.

Regression coverage for: adding image attachment support must not change
plain-text behavior (build_message_content must return a bare str when no
images are attached — see the module docstring in core/images.py for why),
must not misfire on the @tool / @decorator / email-address text that already
appears in normal usage (README.md documents a prompt containing "@tool"),
and must fail with a readable ImageError rather than a raw traceback or a
silent no-op for missing/oversize/corrupt files.

Tests whose behavior depends on Pillow being installed are gated and report
[SKIP] rather than [FAIL] when it's absent, so this suite stays green in a
venv that only has the base requirements.
"""

import asyncio
import base64
import sys
import tempfile
from pathlib import Path

from core.images import (
    ImageError,
    IMAGE_EXTS,
    is_image_path,
    resolve_image_path,
    parse_image_mentions,
    load_image_block,
    build_message_content,
)

# A real, minimal 1x1 grayscale+alpha PNG (68 bytes) — small enough to
# exercise "a valid image file" without a Pillow dependency.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

try:
    from PIL import Image  # noqa: F401
    _HAS_PILLOW = True
except ImportError:
    _HAS_PILLOW = False


class _Skip(Exception):
    """Raised by a test to report [SKIP] instead of [PASS]/[FAIL]."""


def _require_pillow():
    if not _HAS_PILLOW:
        raise _Skip("Pillow not installed")


def _write_png(dir_path: Path, name: str = "img.png") -> str:
    p = dir_path / name
    p.write_bytes(_TINY_PNG)
    return str(p)


# --- parse_image_mentions ---------------------------------------------------

def test_parse_no_mentions_returns_text_unchanged():
    text = "what is the weather like today"
    new_text, paths = parse_image_mentions(text)
    assert new_text == text
    assert paths == []


def test_parse_single_mention_strips_sigil_keeps_path_text():
    new_text, paths = parse_image_mentions("這張圖有什麼問題 @shots/error.png")
    assert new_text == "這張圖有什麼問題 shots/error.png"
    assert paths == ["shots/error.png"]


def test_parse_mention_at_start_of_line():
    new_text, paths = parse_image_mentions("@a.png what is this")
    assert new_text == "a.png what is this"
    assert paths == ["a.png"]


def test_parse_multiple_mentions_preserve_order():
    new_text, paths = parse_image_mentions("compare @before.png and @after.png")
    assert new_text == "compare before.png and after.png"
    assert paths == ["before.png", "after.png"]


def test_parse_non_image_extension_left_untouched():
    text = "搜尋所有含有 @tool 的地方"
    new_text, paths = parse_image_mentions(text)
    assert new_text == text
    assert paths == []


def test_parse_email_address_not_treated_as_mention():
    text = "my email is joesnason@gmail.com, reach out"
    new_text, paths = parse_image_mentions(text)
    assert new_text == text
    assert paths == []


def test_parse_backslash_escaped_space_in_path():
    new_text, paths = parse_image_mentions(r"look at @Screen\ Shot\ 2026.png please")
    assert paths == ["Screen Shot 2026.png"]
    assert new_text == "look at Screen Shot 2026.png please"


def test_parse_uppercase_extension_matches():
    new_text, paths = parse_image_mentions("@Photo.PNG")
    assert paths == ["Photo.PNG"]
    assert new_text == "Photo.PNG"


def test_parse_trailing_punctuation_stripped():
    new_text, paths = parse_image_mentions("look at @shot.png, it's broken")
    assert paths == ["shot.png"]
    assert new_text == "look at shot.png, it's broken"


def test_parse_tilde_path_mention():
    new_text, paths = parse_image_mentions("@~/Desktop/shot.png")
    assert paths == ["~/Desktop/shot.png"]
    assert new_text == "~/Desktop/shot.png"


def test_parse_bare_at_sign_is_not_a_mention():
    text = "reply with @ if you agree"
    new_text, paths = parse_image_mentions(text)
    assert new_text == text
    assert paths == []


# --- resolve_image_path / is_image_path -------------------------------------

def test_resolve_image_path_relative_joins_cwd():
    resolved = resolve_image_path("shots/a.png", "/work/dir")
    assert resolved == str(Path("/work/dir/shots/a.png"))


def test_resolve_image_path_absolute_is_idempotent():
    once = resolve_image_path("/abs/a.png", "/work/dir")
    twice = resolve_image_path(once, "/work/dir")
    assert once == twice == str(Path("/abs/a.png"))


def test_resolve_image_path_expands_tilde():
    resolved = resolve_image_path("~/a.png", "/work/dir")
    assert not resolved.startswith("~")
    assert resolved.endswith("/a.png")


def test_is_image_path_extension_matrix():
    for ext in IMAGE_EXTS:
        assert is_image_path(f"file{ext}")
        assert is_image_path(f"file{ext.upper()}")
    assert not is_image_path("file.txt")
    assert not is_image_path("file.py")
    assert not is_image_path("file")


# --- load_image_block validation --------------------------------------------

def test_load_image_block_missing_file_raises_imageerror():
    try:
        load_image_block("/no/such/file.png")
        assert False, "expected ImageError"
    except ImageError as e:
        assert "not found" in str(e)


def test_load_image_block_directory_raises_imageerror():
    with tempfile.TemporaryDirectory() as d:
        try:
            load_image_block(d)
            assert False, "expected ImageError"
        except ImageError as e:
            assert "directory" in str(e)


def test_load_image_block_oversize_source_raises_imageerror():
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        try:
            load_image_block(path, max_source_bytes=10)
            assert False, "expected ImageError"
        except ImageError as e:
            assert "too large" in str(e)


def test_load_image_block_non_image_bytes_raises_imageerror():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "fake.png"
        p.write_bytes(b"not an image")
        try:
            load_image_block(str(p))
            assert False, "expected ImageError"
        except ImageError:
            pass  # either Pillow's UnidentifiedImageError path or the raw-passthrough path is fine


def test_load_image_block_unsupported_extension_raises_imageerror():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "doc.txt"
        p.write_text("hello")
        try:
            load_image_block(str(p))
            assert False, "expected ImageError"
        except ImageError as e:
            assert "unsupported image type" in str(e)


# --- build_message_content ---------------------------------------------------

def test_build_content_no_images_returns_plain_str():
    result = build_message_content("hello there", [])
    assert isinstance(result, str)
    assert result == "hello there"


def test_build_content_with_image_returns_images_first_then_text():
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        result = build_message_content("what is this", [path], d)
        assert isinstance(result, list)
        assert result[0]["type"] == "image_url"
        assert result[-1] == {"type": "text", "text": "what is this"}


def test_build_content_block_shape_matches_openai_spec():
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        result = build_message_content("x", [path], d)
        block = result[0]
        assert block["type"] == "image_url"
        assert block["image_url"]["url"].startswith("data:image/")
        assert ";base64," in block["image_url"]["url"]


def test_build_content_dedupes_same_path():
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        result = build_message_content("x", [path, path], d)
        image_blocks = [b for b in result if b.get("type") == "image_url"]
        assert len(image_blocks) == 1


def test_build_content_over_max_images_raises():
    with tempfile.TemporaryDirectory() as d:
        paths = [_write_png(Path(d), f"img{i}.png") for i in range(3)]
        try:
            build_message_content("x", paths, d, max_images=2)
            assert False, "expected ImageError"
        except ImageError as e:
            assert "too many images" in str(e)


# --- downscaling (Pillow-gated) ----------------------------------------------

def test_downscale_caps_long_edge_at_1568():
    _require_pillow()
    from PIL import Image
    import io
    im = Image.new("RGB", (3000, 2000), color=(200, 100, 50))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "big.png"
        p.write_bytes(buf.getvalue())
        block = load_image_block(str(p))
        raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
        out = Image.open(io.BytesIO(raw))
        assert max(out.size) <= 1568


def test_downscale_does_not_upscale_small_image():
    _require_pillow()
    from PIL import Image
    import io
    im = Image.new("RGB", (50, 40), color=(10, 20, 30))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "small.png"
        p.write_bytes(buf.getvalue())
        block = load_image_block(str(p))
        raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
        out = Image.open(io.BytesIO(raw))
        assert out.size == (50, 40)


def test_downscale_preserves_aspect_ratio():
    _require_pillow()
    from PIL import Image
    import io
    im = Image.new("RGB", (4000, 1000), color=(1, 2, 3))  # 4:1 aspect
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "wide.png"
        p.write_bytes(buf.getvalue())
        block = load_image_block(str(p))
        raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
        out = Image.open(io.BytesIO(raw))
        ratio = out.size[0] / out.size[1]
        assert abs(ratio - 4.0) < 0.05


def test_rgba_source_encodes_as_png_and_keeps_alpha():
    _require_pillow()
    from PIL import Image
    import io
    im = Image.new("RGBA", (20, 20), color=(255, 0, 0, 128))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "alpha.png"
        p.write_bytes(buf.getvalue())
        block = load_image_block(str(p))
        assert block["image_url"]["url"].startswith("data:image/png;base64,")
        raw = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
        out = Image.open(io.BytesIO(raw))
        assert out.mode in ("RGBA", "LA")


def test_opaque_source_encodes_as_jpeg():
    _require_pillow()
    from PIL import Image
    import io
    im = Image.new("RGB", (20, 20), color=(0, 255, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "opaque.png"
        p.write_bytes(buf.getvalue())
        block = load_image_block(str(p))
        assert block["image_url"]["url"].startswith("data:image/jpeg;base64,")


# --- cross-backend: Ollama's own converter accepts our block shape ----------

def test_ollama_converter_accepts_our_block():
    try:
        from langchain_ollama.chat_models import ChatOllama
        from langchain_core.messages import HumanMessage
    except ImportError:
        raise _Skip("langchain_ollama not installed")

    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        content = build_message_content("what is this", [path], d)

    llm = ChatOllama(model="does-not-matter")
    ollama_messages = llm._convert_messages_to_ollama_messages(
        [HumanMessage(content=content)]
    )
    msg = ollama_messages[0]
    assert msg["images"], "expected at least one image in the converted message"
    # our data:...;base64,... prefix must be stripped by Ollama's own converter
    assert not msg["images"][0].startswith("data:")


def test_litellm_message_conversion_passes_list_content():
    try:
        from langchain_litellm.chat_models.litellm import _convert_message_to_dict
        from langchain_core.messages import HumanMessage
    except ImportError:
        raise _Skip("langchain_litellm not installed")

    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        content = build_message_content("what is this", [path], d)

    result = _convert_message_to_dict(HumanMessage(content=content))
    assert isinstance(result["content"], list)
    assert result["content"][0]["type"] == "image_url"
    assert result["content"][-1] == {"type": "text", "text": "what is this"}


# --- run_turn integration -----------------------------------------------------

class _FakeConsole:
    def __init__(self):
        self.lines = []

    def print(self, *args, **kwargs):
        self.lines.append(args[0] if args else "")


class _FakeLLM:
    """Stubs .astream() to yield no chunks, so stream_response() returns
    None and run_turn() bails out right after appending the HumanMessage —
    exactly the point we want to inspect."""

    async def astream(self, messages, reasoning=None):
        return
        yield  # pragma: no cover — makes this an async generator function


class _FakeLive:
    """No-op stand-in for ui/live_region.py's LiveRegion/SimpleLive — these
    tests only inspect appended message content/printed errors, never the
    spinner/streamed-Markdown rendering."""

    def start_spinner(self, label="Thinking..."):
        pass

    def stop_spinner(self):
        pass

    def update_markdown(self, markdown_text):
        pass

    def finalize_markdown(self, markdown_text):
        pass

    async def ask_menu(self, options, default_idx=0):
        return None


def test_run_turn_without_images_appends_str_content():
    from ui.turn import run_turn
    messages = []
    asyncio.run(run_turn(_FakeLLM(), messages, "hello", _FakeConsole(), _FakeLive(), ".", "manual", images=None))
    assert len(messages) == 1
    assert messages[0].content == "hello"


def test_run_turn_with_image_appends_multipart_content():
    from ui.turn import run_turn
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        messages = []
        asyncio.run(run_turn(_FakeLLM(), messages, "what is this", _FakeConsole(), _FakeLive(), d, "manual",
                              images=[path]))
        assert len(messages) == 1
        assert isinstance(messages[0].content, list)
        assert messages[0].content[0]["type"] == "image_url"


def test_run_turn_bad_image_prints_error_and_appends_nothing():
    from ui.turn import run_turn
    messages = []
    console = _FakeConsole()
    asyncio.run(run_turn(_FakeLLM(), messages, "what is this", console, _FakeLive(), ".", "manual",
                          images=["/no/such.png"]))
    assert messages == []
    assert any("Error" in str(line) for line in console.lines)


# --- CLI --------------------------------------------------------------------

def test_cli_image_flag_repeatable_collects_all():
    import cli
    with tempfile.TemporaryDirectory() as d:
        a, b = _write_png(Path(d), "a.png"), _write_png(Path(d), "b.png")
        args = cli._parse_args(["--image", a, "--image", b])
        images = cli._resolve_images(args)
        assert images == [a, b]


def test_cli_image_flag_defaults_to_empty_list():
    import cli
    args = cli._parse_args([])
    assert cli._resolve_images(args) == []


def test_cli_image_nonexistent_path_exits_2():
    import cli
    args = cli._parse_args(["--image", "/no/such/file.png"])
    try:
        cli._resolve_images(args)
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2


# --- REPL: /image staging -----------------------------------------------------

def test_image_command_stages_path():
    import ui.repl as repl
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        repl._pending_images["paths"] = []
        repl._stage_image(path, _FakeConsole(), d)
        assert repl._pending_images["paths"] == [resolve_image_path(path, d)]
        repl._pending_images["paths"] = []


def test_image_command_bad_path_stages_nothing_and_reports():
    import ui.repl as repl
    repl._pending_images["paths"] = []
    console = _FakeConsole()
    repl._stage_image("/no/such/file.png", console, ".")
    assert repl._pending_images["paths"] == []
    assert any("Error" in str(line) for line in console.lines)


def test_image_command_clear_empties_staging():
    import ui.repl as repl
    with tempfile.TemporaryDirectory() as d:
        path = _write_png(Path(d))
        repl._pending_images["paths"] = [path]
        repl._stage_image("clear", _FakeConsole(), d)
        assert repl._pending_images["paths"] == []


def test_take_pending_images_returns_and_clears():
    import ui.repl as repl
    repl._pending_images["paths"] = ["/a.png", "/b.png"]
    taken = repl._take_pending_images()
    assert taken == ["/a.png", "/b.png"]
    assert repl._pending_images["paths"] == []


def test_slash_commands_includes_image():
    import ui.repl as repl
    assert "/image" in repl.SLASH_COMMANDS


def test_toolbar_hint_filters_to_image_on_slash_i():
    import ui.repl as repl
    matches = [c for c in repl.SLASH_COMMANDS if c.startswith("/i")]
    assert matches == ["/image"]


TESTS = [
    test_parse_no_mentions_returns_text_unchanged,
    test_parse_single_mention_strips_sigil_keeps_path_text,
    test_parse_mention_at_start_of_line,
    test_parse_multiple_mentions_preserve_order,
    test_parse_non_image_extension_left_untouched,
    test_parse_email_address_not_treated_as_mention,
    test_parse_backslash_escaped_space_in_path,
    test_parse_uppercase_extension_matches,
    test_parse_trailing_punctuation_stripped,
    test_parse_tilde_path_mention,
    test_parse_bare_at_sign_is_not_a_mention,
    test_resolve_image_path_relative_joins_cwd,
    test_resolve_image_path_absolute_is_idempotent,
    test_resolve_image_path_expands_tilde,
    test_is_image_path_extension_matrix,
    test_load_image_block_missing_file_raises_imageerror,
    test_load_image_block_directory_raises_imageerror,
    test_load_image_block_oversize_source_raises_imageerror,
    test_load_image_block_non_image_bytes_raises_imageerror,
    test_load_image_block_unsupported_extension_raises_imageerror,
    test_build_content_no_images_returns_plain_str,
    test_build_content_with_image_returns_images_first_then_text,
    test_build_content_block_shape_matches_openai_spec,
    test_build_content_dedupes_same_path,
    test_build_content_over_max_images_raises,
    test_downscale_caps_long_edge_at_1568,
    test_downscale_does_not_upscale_small_image,
    test_downscale_preserves_aspect_ratio,
    test_rgba_source_encodes_as_png_and_keeps_alpha,
    test_opaque_source_encodes_as_jpeg,
    test_ollama_converter_accepts_our_block,
    test_litellm_message_conversion_passes_list_content,
    test_run_turn_without_images_appends_str_content,
    test_run_turn_with_image_appends_multipart_content,
    test_run_turn_bad_image_prints_error_and_appends_nothing,
    test_cli_image_flag_repeatable_collects_all,
    test_cli_image_flag_defaults_to_empty_list,
    test_cli_image_nonexistent_path_exits_2,
    test_image_command_stages_path,
    test_image_command_bad_path_stages_nothing_and_reports,
    test_image_command_clear_empties_staging,
    test_take_pending_images_returns_and_clears,
    test_slash_commands_includes_image,
    test_toolbar_hint_filters_to_image_on_slash_i,
]


if __name__ == "__main__":
    failures = []
    skips = []
    for t in TESTS:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except _Skip as e:
            print(f"[SKIP] {t.__name__}: {e}")
            skips.append(t.__name__)
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failures.append(t.__name__)

    ran = len(TESTS) - len(skips)
    print(f"\n{ran - len(failures)}/{ran} passed ({len(skips)} skipped)")
    sys.exit(1 if failures else 0)

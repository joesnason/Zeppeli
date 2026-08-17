"""Automated tests for ui/streaming.py — the chunk-content normalizer and the
model-call error handling that stream_response() relies on. No Ollama/
network dependency, exits non-zero on failure. Style matches
test_permission_modes.py.

Regression coverage for two separate bugs:

1. cloud/self-hosted models routed via litellm (ChatLiteLLM, e.g.
   Anthropic-style APIs) can yield AIMessageChunk.content as a list of
   content blocks instead of a plain str like ChatOllama does. Before this
   fix, stream_response() assigned that list straight into `accumulated`
   and RichMarkdown(accumulated) raised TypeError: Input data should be a
   string, not <class 'list'>.
2. any exception raised by the model call itself (e.g. litellm's
   ContextWindowExceededError when a large tool result — an unbounded
   rg_search match, for instance — pushes the prompt over a small-context
   cloud model's limit) previously propagated all the way up through
   run_turn() and crashed the whole process with a raw traceback.
   stream_response() now catches it, prints a short message, and returns
   None instead.
"""

import io
import sys

from rich.console import Console

from ui.streaming import _extract_text, _format_model_error, stream_response


def test_extract_text_plain_str():
    assert _extract_text("hello") == "hello"


def test_extract_text_empty_str():
    assert _extract_text("") == ""


def test_extract_text_list_of_text_blocks():
    content = [{"type": "text", "text": "hello "}, {"type": "text", "text": "world"}]
    assert _extract_text(content) == "hello world"


def test_extract_text_list_mixed_with_non_text_blocks():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "tool_use", "id": "1", "name": "foo", "input": {}},
    ]
    assert _extract_text(content) == "hello"


def test_extract_text_list_of_plain_strs():
    assert _extract_text(["hello ", "world"]) == "hello world"


def test_extract_text_empty_list():
    assert _extract_text([]) == ""


def test_extract_text_none():
    assert _extract_text(None) == ""


class _RaisingLLM:
    """Stands in for llm_with_tools: .stream(messages) raises immediately,
    same as litellm surfacing a provider error before yielding any chunk."""

    def __init__(self, exc):
        self._exc = exc

    def stream(self, messages):
        raise self._exc


def test_format_model_error_context_window_exceeded_gets_friendly_hint():
    class ContextWindowExceededError(Exception):
        pass

    msg = _format_model_error(
        ContextWindowExceededError("This model's maximum context length is 26000 tokens...")
    )
    assert "context window" in msg
    assert "narrow" in msg.lower()


def test_format_model_error_generic_exception_is_short_and_labeled():
    msg = _format_model_error(ValueError("x" * 1000))
    assert msg.startswith("ValueError:")
    assert len(msg) < 350  # truncated, not the full 1000-char message


def test_format_model_error_vision_unsupported_gets_friendly_hint():
    msg = _format_model_error(
        Exception("BadRequestError: image input is not supported by this model")
    )
    assert "doesn't accept image input" in msg
    assert "vision model" in msg


def test_format_model_error_generic_image_word_alone_does_not_trigger_vision_hint():
    # "image" alone (no unsupported/invalid/etc. keyword) must not be
    # mistaken for the vision-unsupported case — e.g. a generic error that
    # happens to mention "image" in an unrelated sense.
    msg = _format_model_error(ValueError("failed to process the image upload"))
    assert "doesn't accept image input" not in msg


def test_stream_response_returns_none_on_raise_instead_of_propagating():
    console = Console(file=io.StringIO())
    llm = _RaisingLLM(RuntimeError("connection reset"))
    result = stream_response(llm, [], console)
    assert result is None
    assert "connection reset" in console.file.getvalue()


TESTS = [
    test_extract_text_plain_str,
    test_extract_text_empty_str,
    test_extract_text_list_of_text_blocks,
    test_extract_text_list_mixed_with_non_text_blocks,
    test_extract_text_list_of_plain_strs,
    test_extract_text_empty_list,
    test_extract_text_none,
    test_format_model_error_context_window_exceeded_gets_friendly_hint,
    test_format_model_error_generic_exception_is_short_and_labeled,
    test_format_model_error_vision_unsupported_gets_friendly_hint,
    test_format_model_error_generic_image_word_alone_does_not_trigger_vision_hint,
    test_stream_response_returns_none_on_raise_instead_of_propagating,
]


if __name__ == "__main__":
    failures = []
    for t in TESTS:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failures.append(t.__name__)

    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)

"""Automated tests for ui.streaming._extract_text — the chunk-content
normalizer that stream_response() relies on. No Ollama/network dependency,
exits non-zero on failure. Style matches test_permission_modes.py.

Regression coverage for: cloud/self-hosted models routed via litellm
(ChatLiteLLM, e.g. Anthropic-style APIs) can yield AIMessageChunk.content as
a list of content blocks instead of a plain str like ChatOllama does. Before
this fix, stream_response() assigned that list straight into `accumulated`
and RichMarkdown(accumulated) raised TypeError: Input data should be a
string, not <class 'list'>.
"""

import sys

from ui.streaming import _extract_text


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


TESTS = [
    test_extract_text_plain_str,
    test_extract_text_empty_str,
    test_extract_text_list_of_text_blocks,
    test_extract_text_list_mixed_with_non_text_blocks,
    test_extract_text_list_of_plain_strs,
    test_extract_text_empty_list,
    test_extract_text_none,
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

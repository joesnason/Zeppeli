"""Automated tests for core/messages.py's truncate_tool_output() — the
generic line/char cap applied to every tool result (and the CANCELLED
permission-denial message) right before it becomes ToolMessage content sent
back to the model. No Ollama/network dependency, exits non-zero on failure.
Style matches test_tools.py.

Two independent rules, both checked, both can apply:
1. Line rule: more than 40 lines -> keep first 20 + last 20, "[truncated N
   lines]" marker in between.
2. Char rule: checked against the (possibly already line-truncated) result;
   more than 2400 chars -> keep first 1200 + last 1200, "[truncated N chars]"
   marker in between.

See test_sessions.py/test_eventlog.py for the companion coverage of the
additional_kwargs["full_output"] fallback that preserves the untruncated
original for session/event-log persistence.
"""

import sys

from core.messages import truncate_tool_output, tool_result_ok


def test_short_output_unchanged():
    text = "line one\nline two\nline three"
    assert truncate_tool_output(text) == text


def test_exactly_at_boundaries_unchanged():
    lines_text = "\n".join(f"line {i}" for i in range(40))  # exactly 40 lines
    assert "truncated" not in truncate_tool_output(lines_text)
    chars_text = "x" * 2400  # exactly 2400 chars
    assert truncate_tool_output(chars_text) == chars_text


def test_empty_string_unchanged():
    assert truncate_tool_output("") == ""


def test_line_rule_only():
    lines = [f"line {i}" for i in range(60)]  # 60 short lines, well under 2400 chars
    result = truncate_tool_output("\n".join(lines))
    assert "[truncated 20 lines]" in result
    assert "chars]" not in result
    result_lines = result.split("\n")
    assert result_lines[:20] == lines[:20]
    assert result_lines[-20:] == lines[-20:]
    for i in range(20, 40):
        assert lines[i] not in result


def test_char_rule_only():
    # 10 lines of 300 chars each = 3000 chars, well under the 40-line threshold
    lines = ["x" * 300 for _ in range(10)]
    text = "\n".join(lines)
    result = truncate_tool_output(text)
    assert "lines]" not in result
    assert "chars]" in result
    assert result.startswith(text[:1200])
    assert result.endswith(text[-1200:])


def test_both_rules_triggered():
    lines = [f"line {i} " + "x" * 100 for i in range(100)]  # 100 lines x ~108 chars
    text = "\n".join(lines)
    result = truncate_tool_output(text)
    # 40 kept lines x ~108 chars is still well over 2400, so the char rule
    # also fires against the line-truncated intermediate result — the char
    # cut can land on top of (and remove) the line marker itself, so only
    # the char marker is guaranteed to survive in the final output.
    assert "chars]" in result
    assert len(result) < 2400 + 200  # bounded near the char cap + marker overhead
    assert result.startswith("line 0 ")
    assert result.endswith("x" * 100)


def test_single_very_long_line_triggers_char_rule_only():
    text = "x" * 5000  # 1 line, well over the char cap
    result = truncate_tool_output(text)
    assert "lines]" not in result
    assert "chars]" in result


def test_tool_result_ok_survives_truncation():
    text = "Error: " + "\n".join(f"detail line {i}" for i in range(100))
    result = truncate_tool_output(text)
    assert tool_result_ok(result) is False


TESTS = [
    test_short_output_unchanged,
    test_exactly_at_boundaries_unchanged,
    test_empty_string_unchanged,
    test_line_rule_only,
    test_char_rule_only,
    test_both_rules_triggered,
    test_single_very_long_line_triggers_char_rule_only,
    test_tool_result_ok_survives_truncation,
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

"""Automated tests for core/tools.py's rg_search output cap. No Ollama/
network dependency (uses the bundled bin/rg binary against a temp file),
exits non-zero on failure. Style matches test_permission_modes.py.

Regression coverage for: a broad rg_search pattern against a large file
(e.g. a build log with many FAILED/error: lines) could return unbounded
output, which — especially against a small-context cloud model — could
blow past the model's context window and crash the whole process with an
unhandled litellm ContextWindowExceededError. See test_streaming.py for the
accompanying fix in ui/streaming.py (model-call errors no longer crash the
process).
"""

import sys
import tempfile
from pathlib import Path

from core.tools import rg_search


def test_rg_search_under_cap_not_truncated():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "small.log"
        f.write_text("error: line one\nerror: line two\n")
        result = rg_search.invoke({"pattern": "error:", "path": str(f)})
        assert "line one" in result and "line two" in result
        assert "truncated" not in result


def test_rg_search_over_cap_is_truncated_with_note():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "build.log"
        f.write_text("".join(f"error: failure at step {i}\n" for i in range(5000)))
        result = rg_search.invoke({"pattern": "error:", "path": str(f), "max_bytes": 500})
        assert len(result.encode()) < 700  # capped content + note, well under the raw match size
        assert "[Output truncated at 500 bytes" in result


def test_rg_search_no_matches_unaffected_by_cap():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "clean.log"
        f.write_text("all good\n")
        result = rg_search.invoke({"pattern": "error:", "path": str(f), "max_bytes": 10})
        assert result == "(no matches)"


TESTS = [
    test_rg_search_under_cap_not_truncated,
    test_rg_search_over_cap_is_truncated_with_note,
    test_rg_search_no_matches_unaffected_by_cap,
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

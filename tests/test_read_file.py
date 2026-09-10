"""Automated tests for core/tools.py's read_file() — pagination, the
max_lines/max_bytes stop conditions, and error paths. No Ollama/network
dependency, exits non-zero on failure. Style matches test_tools.py.

Regression coverage for: a single line longer than max_bytes (e.g. one
giant line in a build log — a full classpath, a long ninja command)
used to leave `lines` empty and the reported end-of-range equal to the
call's own offset, so a follow-up call at that same offset repeated the
identical "[Stopped: max_bytes limit reached ...]" failure forever, with
no way to read past it. read_file() now includes a truncated slice of
that line and counts it as consumed, so the offset always advances.
"""

import tempfile
from pathlib import Path

from core.tools import read_file


def _header_end_line(result: str) -> int:
    """Parses "lines <start>-<end>" out of read_file()'s first line."""
    first_line = result.splitlines()[0]
    range_part = first_line.split("|")[1].strip()  # "lines <start>-<end>"
    return int(range_part.split()[1].split("–")[1])


def test_read_file_basic_pagination_more_available():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "ten_lines.txt"
        f.write_text("".join(f"line {i}\n" for i in range(10)))
        result = read_file.invoke({"path": str(f), "limit": 5})
        assert "[File:" in result
        assert "lines 1–5" in result
        assert "line 0" in result and "line 4" in result
        assert "line 5" not in result
        assert "[More available: use offset=5 to continue]" in result


def test_read_file_end_of_file():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "three_lines.txt"
        f.write_text("a\nb\nc\n")
        result = read_file.invoke({"path": str(f)})
        assert "[End of file]" in result
        assert "a\nb\nc\n" in result


def test_read_file_max_lines_truncation():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "many_lines.txt"
        f.write_text("".join(f"{i}\n" for i in range(20)))
        result = read_file.invoke({"path": str(f), "max_lines": 3, "limit": 20})
        assert "[Stopped: max_lines limit reached — use offset=3 to continue]" in result
        assert "2\n" in result  # 0,1,2 kept
        assert "3\n" not in result


def test_read_file_stopped_footer_is_as_explicit_as_more_available_footer():
    # Regression: a hard-limit stop used to omit the "use offset=N to
    # continue" hint the "more available" footer always had, making it
    # less actionable — a model asked to keep reading past a max_bytes/
    # max_lines stop has no less explicit an instruction than it would
    # after a plain "more available" stop.
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "many_lines.txt"
        f.write_text("".join(f"{i}\n" for i in range(20)))
        result = read_file.invoke({"path": str(f), "max_lines": 5, "limit": 20})
        end_line = _header_end_line(result)
        assert f"use offset={end_line} to continue" in result


def test_read_file_oversized_single_line_included_truncated_and_advances():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "one_huge_line.txt"
        f.write_text("X" * 600 + "\n" + "next line\n")
        result = read_file.invoke({"path": str(f), "max_bytes": 50})
        assert result != ""
        assert "[line truncated," in result
        assert "X" * 50 in result  # the kept slice
        # The offset must have advanced past the oversized line (not
        # stayed at 0), or a follow-up call would repeat this forever.
        end_line = _header_end_line(result)
        assert end_line == 1

        # A follow-up call at the new offset makes real progress.
        result2 = read_file.invoke({"path": str(f), "offset": end_line, "max_bytes": 50})
        assert "next line" in result2
        assert "[End of file]" in result2


def test_read_file_deferred_line_when_budget_exhausted_by_prior_lines():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "two_lines.txt"
        # line 1 is exactly 10 bytes ("AAAAAAAAA\n"); line 2 is far larger.
        f.write_text("A" * 9 + "\n" + "B" * 50 + "\n")
        result = read_file.invoke({"path": str(f), "max_bytes": 10})
        # line 1 fits exactly; line 2 has zero remaining budget in this
        # call, so it must be deferred whole (not partially/incorrectly
        # included) rather than silently dropped.
        assert "A" * 9 in result
        assert "B" not in result
        end_line = _header_end_line(result)
        assert end_line == 1
        assert "[Stopped: max_bytes limit reached — use offset=1 to continue]" in result

        # Read fresh at the new offset — line 2 is itself oversized, so
        # it's now handled via the same truncate-and-advance path.
        result2 = read_file.invoke({"path": str(f), "offset": end_line, "max_bytes": 10})
        assert "[line truncated," in result2
        assert "B" * 10 in result2


def test_read_file_not_found():
    result = read_file.invoke({"path": "/no/such/file/exists.xyz"})
    assert "[read_file] Error: file not found" in result


def test_read_file_offset_past_end_of_file():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "two_lines.txt"
        f.write_text("a\nb\n")
        result = read_file.invoke({"path": str(f), "offset": 10})
        assert "[read_file] Error: offset 10 exceeds file length" in result

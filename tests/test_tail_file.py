"""Automated tests for core/tools.py's tail_file() — the bounded
backward-read algorithm (seek to max(0, filesize - max_bytes), read
forward, drop a likely-partial leading line when the seek landed mid-file)
and its footer/error paths. No Ollama/network dependency, exits non-zero
on failure. Style matches test_read_file.py.
"""

import tempfile
from pathlib import Path

from core.tools import tail_file


def test_tail_file_normal_case():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "ten_lines.txt"
        f.write_text("".join(f"line {i}\n" for i in range(10)))
        result = tail_file.invoke({"path": str(f), "lines": 3})
        assert "[File:" in result
        assert "line 7" in result and "line 8" in result and "line 9" in result
        assert "line 6" not in result
        assert "[Showing last 3 lines]" in result


def test_tail_file_file_shorter_than_requested_lines():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "five_lines.txt"
        f.write_text("".join(f"{i}\n" for i in range(5)))
        result = tail_file.invoke({"path": str(f), "lines": 100})
        for i in range(5):
            assert f"{i}\n" in result
        assert "[Beginning of file reached — file has only 5 lines]" in result


def test_tail_file_exactly_at_max_bytes_boundary():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "boundary.txt"
        content = "".join(f"line{i:03d}\n" for i in range(100))  # 8 bytes/line
        f.write_text(content)
        filesize = f.stat().st_size
        result = tail_file.invoke({"path": str(f), "lines": 1000, "max_bytes": filesize})
        # seek_pos == max(0, filesize - filesize) == 0 — nothing dropped
        # from the front, first line present in full.
        assert "line000" in result
        assert "line099" in result
        assert "[Beginning of file reached — file has only 100 lines]" in result


def test_tail_file_larger_than_max_bytes_drops_partial_first_line():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "fixed_width.txt"
        # 50 lines of fixed width 10 bytes each ("line000\n" padded).
        lines = [f"line{i:03d}\n" for i in range(50)]  # 8 bytes each
        f.write_text("".join(lines))
        filesize = f.stat().st_size
        # Seek so the window starts 4 bytes into a line (mid-line).
        max_bytes = filesize - (8 * 10) - 4
        result = tail_file.invoke({"path": str(f), "lines": 1000, "max_bytes": max_bytes})
        # The partial fragment from the split line must not appear, and
        # every kept line must be a complete, correctly-ordered "lineNNN".
        for line in result.splitlines():
            if line.startswith("line"):
                assert len(line) == 7 and line[4:].isdigit()
        assert "line049" in result  # last line still present
        assert "e010" not in result  # a fragment of a split line, not a real line


def test_tail_file_window_has_fewer_lines_than_requested():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "many_lines.txt"
        f.write_text("".join(f"{i:04d}\n" for i in range(1000)))  # 5 bytes/line
        filesize = f.stat().st_size
        # Small window: room for a handful of complete lines after the
        # partial-first-line drop, well short of the 500 lines requested.
        max_bytes = 47  # ~9 lines worth, minus the dropped partial one
        result = tail_file.invoke({"path": str(f), "lines": 500, "max_bytes": max_bytes})
        assert "0999" in result  # last line always present
        assert f"within the last {max_bytes} bytes of the file" in result
        assert "increase max_bytes to search further back" in result
        # Sanity: kept_count is genuinely less than requested.
        header = result.splitlines()[0]
        kept_count = int(header.split("last ")[1].split(" lines")[0])
        assert kept_count < 500


def test_tail_file_not_found():
    result = tail_file.invoke({"path": "/no/such/file/exists.xyz"})
    assert "[tail_file] Error: file not found" in result


def test_tail_file_path_is_a_directory():
    with tempfile.TemporaryDirectory() as d:
        result = tail_file.invoke({"path": d})
        assert "[tail_file] Error:" in result
        assert "is a directory, not a file" in result


def test_tail_file_empty_file():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "empty.txt"
        f.write_text("")
        result = tail_file.invoke({"path": str(f)})
        assert "[File:" in result
        assert "last 0 lines | 0 bytes" in result
        assert "[Beginning of file reached — file has only 0 lines]" in result


def test_tail_file_single_line_larger_than_max_bytes_dropped_entirely():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "one_huge_line.txt"
        f.write_text("X" * 500)  # no trailing newline anywhere in the file
        result = tail_file.invoke({"path": str(f), "lines": 10, "max_bytes": 50})
        assert "last 0 lines | 0 bytes" in result
        assert "Only found 0 of requested 10 lines" in result
        assert "increase max_bytes to search further back" in result
        assert "X" not in result.split("]", 1)[1].split("[", 1)[0]  # no garbage fragment in content


def test_tail_file_default_lines_and_max_bytes():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "small.txt"
        f.write_text("".join(f"{i}\n" for i in range(5)))
        result = tail_file.invoke({"path": str(f)})  # lines/max_bytes omitted
        # Defaults (lines=100, max_bytes=98304) are both far larger than
        # this file, so every line is returned and the "beginning of file"
        # footer fires — confirms the signature defaults are actually used.
        for i in range(5):
            assert f"{i}\n" in result
        assert "[Beginning of file reached — file has only 5 lines]" in result

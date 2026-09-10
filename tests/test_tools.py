"""Automated tests for core/tools.py's rg_search output cap and its
ripgrep-binary resolution/error-handling. No Ollama/network dependency
(uses the bundled bin/rg-* binary against a temp file), exits non-zero
on failure. Style matches test_permission_modes.py.

Regression coverage for: a broad rg_search pattern against a large file
(e.g. a build log with many FAILED/error: lines) could return unbounded
output, which — especially against a small-context cloud model — could
blow past the model's context window and crash the whole process with an
unhandled litellm ContextWindowExceededError. See test_streaming.py for the
accompanying fix in ui/streaming.py (model-call errors no longer crash the
process).

Also covers: rg_search() used to crash the whole process with an
uncaught OSError ("[Errno 8] Exec format error") when the bundled
binary didn't match the running platform (found for real on a Linux
machine, where bin/rg was a macOS-only binary). _find_rg_bin() now
resolves a platform-matching binary (or a system-installed `rg` on
PATH, preferred either way), and rg_search() degrades to a friendly
error string instead of crashing if no binary is usable or launching it
fails outright.
"""

import tempfile
from pathlib import Path

import core.tools as tools_module
from core.tools import rg_search, _find_rg_bin


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


# --- core/tools.py: _find_rg_bin() resolution -------------------------------

def test_find_rg_bin_prefers_system_rg_when_present(monkeypatch):
    monkeypatch.setattr(tools_module.shutil, "which", lambda name: "/usr/local/bin/rg")
    assert _find_rg_bin() == "/usr/local/bin/rg"


def test_find_rg_bin_falls_back_to_bundled_darwin_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tools_module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(tools_module, "_BIN_DIR", tmp_path)
    (tmp_path / "rg-darwin-arm64").write_text("fake binary")
    assert _find_rg_bin() == str(tmp_path / "rg-darwin-arm64")


def test_find_rg_bin_falls_back_to_bundled_linux_binary(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tools_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(tools_module, "_BIN_DIR", tmp_path)
    (tmp_path / "rg-linux-x86_64").write_text("fake binary")
    assert _find_rg_bin() == str(tmp_path / "rg-linux-x86_64")


def test_find_rg_bin_returns_none_for_unrecognized_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(tools_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tools_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(tools_module, "_BIN_DIR", tmp_path)
    assert _find_rg_bin() is None


def test_find_rg_bin_returns_none_when_bundled_file_missing(monkeypatch, tmp_path):
    # Platform matches a known entry, but no file actually exists at
    # _BIN_DIR (e.g. a partial/broken checkout) — must not return a
    # bogus path.
    monkeypatch.setattr(tools_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(tools_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tools_module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(tools_module, "_BIN_DIR", tmp_path)  # empty dir
    assert _find_rg_bin() is None


# --- core/tools.py: rg_search() graceful degradation ------------------------

def test_rg_search_returns_friendly_error_when_rg_bin_is_none(monkeypatch):
    monkeypatch.setattr(tools_module, "RG_BIN", None)
    calls = []
    monkeypatch.setattr(tools_module.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    result = rg_search.invoke({"pattern": "x", "path": "."})
    assert "Error:" in result
    assert "ripgrep" in result.lower()
    assert calls == []  # never even attempted to launch anything


def test_rg_search_returns_friendly_error_on_launch_failure_instead_of_crashing(monkeypatch):
    # Regression: a wrong-platform binary made subprocess.run() raise an
    # uncaught OSError ("[Errno 8] Exec format error"), crashing the
    # whole process instead of returning a tool-error string.
    monkeypatch.setattr(tools_module, "RG_BIN", "/some/wrong-platform/rg")

    def _raise(*args, **kwargs):
        raise OSError(8, "Exec format error")

    monkeypatch.setattr(tools_module.subprocess, "run", _raise)
    result = rg_search.invoke({"pattern": "x", "path": "."})
    assert "Error:" in result
    assert "Exec format error" in result

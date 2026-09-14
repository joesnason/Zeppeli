"""Automated tests for core/tools.py's run_bash() — the shell-execution
tool. No Ollama/network dependency, exits non-zero on failure. Style
matches test_tail_file.py. Permission-gating behavior (the workspace/sudo
approval checks) is covered separately in test_permission_modes.py — this
file only covers the tool's own execution/output/error handling, including
its conditional direnv/.envrc wrapping.
"""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

import core.tools as tools
from core.tools import run_bash


def test_run_bash_basic_command_returns_stdout():
    result = run_bash.invoke({"command": "echo hello"})
    assert "hello" in result


def test_run_bash_uses_given_cwd(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    result = run_bash.invoke({"command": "ls", "cwd": str(tmp_path)})
    assert "marker.txt" in result


def test_run_bash_nonzero_exit_code_prefixed():
    result = run_bash.invoke({"command": "exit 3"})
    assert result.startswith("(exit code 3)")


def test_run_bash_zero_exit_code_not_prefixed():
    result = run_bash.invoke({"command": "echo hi"})
    assert not result.startswith("(exit code")


def test_run_bash_captures_stderr():
    result = run_bash.invoke({"command": "echo oops 1>&2"})
    assert "oops" in result


def test_run_bash_no_output_case(tmp_path):
    # cwd is a fresh tmp_path (no applicable .envrc) so this doesn't
    # accidentally pick up direnv's own "loading ..." stderr note from
    # some ambient .envrc (e.g. this repo's own) when direnv is installed.
    result = run_bash.invoke({"command": "true", "cwd": str(tmp_path)})
    assert result == "(no output)"


def test_run_bash_output_truncated_at_max_bytes():
    result = run_bash.invoke({"command": "yes x | head -c 60000"})
    assert "[Output truncated at 50000 bytes]" in result
    assert len(result.encode()) < 60000


def test_run_bash_timeout_expired(monkeypatch):
    def _raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="sleep 999", timeout=1)

    monkeypatch.setattr(tools.subprocess, "run", _raise_timeout)
    result = run_bash.invoke({"command": "sleep 999", "timeout": 1})
    assert result == "Error: command timed out after 1s"


def test_run_bash_oserror_reported_as_error_string(monkeypatch):
    def _raise_oserror(*a, **k):
        raise OSError("bash not found")

    monkeypatch.setattr(tools.subprocess, "run", _raise_oserror)
    result = run_bash.invoke({"command": "echo hi"})
    assert result.startswith("Error: couldn't run bash:")
    assert "bash not found" in result


# --- direnv / .envrc wrapping -----------------------------------------------

def test_run_bash_wraps_with_direnv_exec_when_direnv_available(monkeypatch, tmp_path):
    seen = {}

    def _spy(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tools, "DIRENV_BIN", "/fake/bin/direnv")
    monkeypatch.setattr(tools.subprocess, "run", _spy)
    run_bash.invoke({"command": "echo hi", "cwd": str(tmp_path)})
    assert seen["cmd"] == ["/fake/bin/direnv", "exec", str(tmp_path), "bash", "-c", "echo hi"]


def test_run_bash_plain_bash_when_direnv_not_installed(monkeypatch, tmp_path):
    seen = {}

    def _spy(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tools, "DIRENV_BIN", None)
    monkeypatch.setattr(tools.subprocess, "run", _spy)
    run_bash.invoke({"command": "echo hi", "cwd": str(tmp_path)})
    assert seen["cmd"] == ["bash", "-c", "echo hi"]


@pytest.mark.skipif(shutil.which("direnv") is None, reason="direnv not installed on this machine")
def test_run_bash_loads_allowed_envrc_via_real_direnv(tmp_path):
    (tmp_path / ".envrc").write_text("export ZEPPELI_TEST_VAR=hello\n")
    subprocess.run(["direnv", "allow", str(tmp_path)], check=True, capture_output=True)
    result = run_bash.invoke({"command": "echo [$ZEPPELI_TEST_VAR]", "cwd": str(tmp_path)})
    if "is blocked" in result:
        # `direnv allow` didn't actually persist in this environment (e.g. a
        # sandboxed test runner without write access to its real allow-file
        # store) — not a code bug, just an environment that can't exercise
        # this path; the blocked-fallback path below covers the code either
        # way.
        pytest.skip("`direnv allow` did not take effect in this environment")
    assert "[hello]" in result


@pytest.mark.skipif(shutil.which("direnv") is None, reason="direnv not installed on this machine")
def test_run_bash_does_not_load_unallowed_envrc(tmp_path):
    (tmp_path / ".envrc").write_text("export ZEPPELI_TEST_VAR2=hello\n")
    # Deliberately never runs `direnv allow` — the env must NOT be loaded,
    # but the command must still run (no error, no hang).
    result = run_bash.invoke({"command": "echo [$ZEPPELI_TEST_VAR2]", "cwd": str(tmp_path)})
    assert "[]" in result

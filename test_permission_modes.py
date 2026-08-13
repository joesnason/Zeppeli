"""Automated tests for permission-mode logic — no Ollama/LLM dependency, no
real TTY interaction. Safe to run anytime (e.g. with Ollama stopped) and
exits non-zero on failure, unlike test_tool_call.py (which needs a live
model and is inspected by eye).

Covers:
- build_pre_tool_hooks() dispatch for all three modes
- _is_within_cwd() path-containment edge cases
- the auto-mode hook's inside/outside-cwd branching
- cli.py's argparse: --yolo-mode/--auto-mode mutual exclusivity, -p/--prompt
"""

import sys
import tempfile
from pathlib import Path

from rich.console import Console

import cli
import ui.permissions as permissions
from ui.permissions import (
    MODE_APPROVAL,
    MODE_AUTO,
    MODE_YOLO,
    _is_within_cwd,
    _make_auto_hook,
    build_pre_tool_hooks,
)

# Captured before any test can monkeypatch permissions.permission_ask.
_ORIGINAL_PERMISSION_ASK = permissions.permission_ask

_console = Console()


def _reset_approval_state():
    permissions._turn_approved.clear()
    permissions._session_approved.clear()


def test_build_pre_tool_hooks_yolo():
    assert build_pre_tool_hooks(MODE_YOLO, "/any/cwd") == {}


def test_build_pre_tool_hooks_approval_shape():
    hooks = build_pre_tool_hooks(MODE_APPROVAL, "/any/cwd")
    assert hooks["write_file"] is _ORIGINAL_PERMISSION_ASK
    assert hooks["delete_file"] is _ORIGINAL_PERMISSION_ASK
    assert hooks is not permissions.PRE_TOOL_HOOKS


def test_build_pre_tool_hooks_auto_shape():
    hooks = build_pre_tool_hooks(MODE_AUTO, "/any/cwd")
    assert set(hooks.keys()) == {"write_file", "delete_file"}
    assert callable(hooks["write_file"]) and callable(hooks["delete_file"])
    assert hooks["write_file"] is hooks["delete_file"]


def test_is_within_cwd_relative_inside():
    with tempfile.TemporaryDirectory() as tmp:
        assert _is_within_cwd(str(Path(tmp) / "child" / "file.txt"), tmp) is True


def test_is_within_cwd_absolute_inside():
    with tempfile.TemporaryDirectory() as tmp:
        assert _is_within_cwd(str(Path(tmp) / "file.txt"), tmp) is True


def test_is_within_cwd_absolute_outside():
    with tempfile.TemporaryDirectory() as tmp:
        assert _is_within_cwd("/etc/passwd", tmp) is False


def test_is_within_cwd_dotdot_escape_outside():
    with tempfile.TemporaryDirectory() as tmp:
        escaped = str(Path(tmp) / ".." / "escape.txt")
        assert _is_within_cwd(escaped, tmp) is False


def test_auto_hook_inside_cwd_no_prompt():
    with tempfile.TemporaryDirectory() as tmp:
        def _spy(*a, **k):
            raise AssertionError("permission_ask should not be called inside cwd")

        permissions.permission_ask = _spy
        try:
            hook = _make_auto_hook(tmp)
            path = str(Path(tmp) / "f.txt")
            assert hook("write_file", {"path": path}, _console) is True
        finally:
            permissions.permission_ask = _ORIGINAL_PERMISSION_ASK


def test_auto_hook_outside_cwd_delegates_true():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        permissions.permission_ask = lambda *a, **k: (calls.append(a), True)[1]
        try:
            hook = _make_auto_hook(tmp)
            assert hook("write_file", {"path": "/etc/passwd"}, _console) is True
            assert len(calls) == 1
        finally:
            permissions.permission_ask = _ORIGINAL_PERMISSION_ASK


def test_auto_hook_outside_cwd_delegates_false():
    with tempfile.TemporaryDirectory() as tmp:
        permissions.permission_ask = lambda *a, **k: False
        try:
            hook = _make_auto_hook(tmp)
            assert hook("delete_file", {"path": "/etc/passwd"}, _console) is False
        finally:
            permissions.permission_ask = _ORIGINAL_PERMISSION_ASK


def test_cli_mutex_yolo_and_auto_raises_systemexit_2():
    try:
        cli._parse_args(["--yolo-mode", "--auto-mode"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert e.code == 2


def test_cli_prompt_flag_short_and_long():
    assert cli._parse_args(["-p", "hi"]).prompt == "hi"
    assert cli._parse_args(["--prompt", "hi"]).prompt == "hi"


def test_cli_prompt_default_none():
    assert cli._parse_args([]).prompt is None


def test_cli_prompt_combines_with_mode_flags():
    ns = cli._parse_args(["--yolo-mode", "-p", "hi"])
    assert ns.yolo_mode is True
    assert ns.prompt == "hi"


TESTS = [
    test_build_pre_tool_hooks_yolo,
    test_build_pre_tool_hooks_approval_shape,
    test_build_pre_tool_hooks_auto_shape,
    test_is_within_cwd_relative_inside,
    test_is_within_cwd_absolute_inside,
    test_is_within_cwd_absolute_outside,
    test_is_within_cwd_dotdot_escape_outside,
    test_auto_hook_inside_cwd_no_prompt,
    test_auto_hook_outside_cwd_delegates_true,
    test_auto_hook_outside_cwd_delegates_false,
    test_cli_mutex_yolo_and_auto_raises_systemexit_2,
    test_cli_prompt_flag_short_and_long,
    test_cli_prompt_default_none,
    test_cli_prompt_combines_with_mode_flags,
]


if __name__ == "__main__":
    failures = []
    for t in TESTS:
        _reset_approval_state()
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failures.append(t.__name__)
        finally:
            permissions.permission_ask = _ORIGINAL_PERMISSION_ASK
            _reset_approval_state()

    print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} passed")
    sys.exit(1 if failures else 0)

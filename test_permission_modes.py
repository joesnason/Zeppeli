"""Automated tests for permission-mode logic — no Ollama/LLM dependency, no
real TTY interaction. Safe to run anytime (e.g. with Ollama stopped) and
exits non-zero on failure, unlike test_tool_call.py (which needs a live
model and is inspected by eye).

Covers:
- build_pre_tool_hooks() dispatch for all three modes
- _is_within_cwd() path-containment edge cases
- the auto-mode hook's inside/outside-cwd branching
- cli.py's argparse: --yolo-mode/--auto-mode mutual exclusivity, -p/--prompt
- confirm_auto_mode_trust()'s _arrow_menu-index -> bool mapping
- ui.repl.main()'s auto-mode trust gate: declining skips load_llm(), and
  one-shot -p mode never triggers the trust check at all
- ui.repl._clear_input: Esc clears the live input buffer, no-ops if empty
- ui.repl main()'s per-process session ID: uniqueness, and its rendering in
  _get_toolbar()

Tests that call ui.repl.main() for real also redirect
core.sessions.SESSIONS_DIR and core.eventlog.LOGS_DIR to a temp dir
(main() now unconditionally writes a session-history file and an
event-log file — see test_sessions.py / test_eventlog.py for those
features' own coverage) so this suite never touches the developer's real
~/.zeppeli/sessions/ or ~/.zeppeli/logs/.
"""

import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.buffer import Buffer
from rich.console import Console

import cli
import core.eventlog as eventlog
import core.sessions as sessions
import ui.permissions as permissions
import ui.repl as repl
from ui.permissions import (
    MODE_APPROVAL,
    MODE_AUTO,
    MODE_YOLO,
    _is_within_cwd,
    _make_auto_hook,
    build_pre_tool_hooks,
    confirm_auto_mode_trust,
)

# Captured before any test can monkeypatch permissions.permission_ask /
# _arrow_menu, or ui.repl's confirm_auto_mode_trust / load_llm / run_turn.
_ORIGINAL_PERMISSION_ASK = permissions.permission_ask
_ORIGINAL_ARROW_MENU = permissions._arrow_menu
_ORIGINAL_CONFIRM_AUTO_MODE_TRUST = repl.confirm_auto_mode_trust
_ORIGINAL_REPL_LOAD_LLM = repl.load_llm
_ORIGINAL_REPL_RUN_TURN = repl.run_turn
_ORIGINAL_SESSIONS_DIR = sessions.SESSIONS_DIR
_ORIGINAL_LOGS_DIR = eventlog.LOGS_DIR

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


def test_confirm_auto_mode_trust_yes():
    permissions._arrow_menu = lambda *a, **k: 0  # "Yes, I trust this folder"
    try:
        assert confirm_auto_mode_trust(_console) is True
    finally:
        permissions._arrow_menu = _ORIGINAL_ARROW_MENU


def test_confirm_auto_mode_trust_no():
    permissions._arrow_menu = lambda *a, **k: 1  # "No, exit"
    try:
        assert confirm_auto_mode_trust(_console) is False
    finally:
        permissions._arrow_menu = _ORIGINAL_ARROW_MENU


def test_confirm_auto_mode_trust_cancelled():
    permissions._arrow_menu = lambda *a, **k: None  # Ctrl+C
    try:
        assert confirm_auto_mode_trust(_console) is False
    finally:
        permissions._arrow_menu = _ORIGINAL_ARROW_MENU


def test_main_auto_mode_declined_skips_llm_load():
    def _spy(*a, **k):
        raise AssertionError("load_llm should not be called when trust is declined")

    repl.confirm_auto_mode_trust = lambda console: False
    repl.load_llm = _spy
    try:
        repl.main(mode=MODE_AUTO, prompt=None)  # should return early, no exception
    finally:
        repl.confirm_auto_mode_trust = _ORIGINAL_CONFIRM_AUTO_MODE_TRUST
        repl.load_llm = _ORIGINAL_REPL_LOAD_LLM


def test_main_prompt_mode_skips_trust_check():
    def _spy(console):
        raise AssertionError("confirm_auto_mode_trust should not be called in -p mode")

    repl.confirm_auto_mode_trust = _spy
    repl.load_llm = lambda **k: object()
    repl.run_turn = lambda *a, **k: None
    with tempfile.TemporaryDirectory() as tmp:
        sessions.SESSIONS_DIR = Path(tmp) / "sessions"
        eventlog.LOGS_DIR = Path(tmp) / "logs"
        try:
            repl.main(mode=MODE_AUTO, prompt="hi")
        finally:
            repl.confirm_auto_mode_trust = _ORIGINAL_CONFIRM_AUTO_MODE_TRUST
            repl.load_llm = _ORIGINAL_REPL_LOAD_LLM
            repl.run_turn = _ORIGINAL_REPL_RUN_TURN
            sessions.SESSIONS_DIR = _ORIGINAL_SESSIONS_DIR
            eventlog.LOGS_DIR = _ORIGINAL_LOGS_DIR


def test_clear_input_esc_empties_nonempty_buffer():
    buffer = Buffer()
    buffer.text = "hello world"
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))
    repl._clear_input(event)
    assert buffer.text == ""


def test_clear_input_esc_noop_on_empty_buffer():
    buffer = Buffer()
    event = SimpleNamespace(app=SimpleNamespace(current_buffer=buffer))
    repl._clear_input(event)  # should not raise
    assert buffer.text == ""


def test_main_seeds_unique_session_id():
    repl.load_llm = lambda **k: object()
    repl.run_turn = lambda *a, **k: None
    with tempfile.TemporaryDirectory() as tmp:
        sessions.SESSIONS_DIR = Path(tmp) / "sessions"
        eventlog.LOGS_DIR = Path(tmp) / "logs"
        try:
            repl.main(mode=MODE_APPROVAL, prompt="hi")
            first_id = repl._session_state["id"]
            repl.main(mode=MODE_APPROVAL, prompt="hi")
            second_id = repl._session_state["id"]
        finally:
            repl.load_llm = _ORIGINAL_REPL_LOAD_LLM
            repl.run_turn = _ORIGINAL_REPL_RUN_TURN
            sessions.SESSIONS_DIR = _ORIGINAL_SESSIONS_DIR
            eventlog.LOGS_DIR = _ORIGINAL_LOGS_DIR

    uuid.UUID(first_id)  # raises ValueError if not a valid UUID string
    uuid.UUID(second_id)
    assert first_id != second_id


def test_get_toolbar_includes_session_id_line():
    fixed_id = str(uuid.uuid4())
    repl._session_state["id"] = fixed_id
    tokens = repl._get_toolbar()
    text = "".join(t for _, t in tokens)

    assert f"Session ID: {fixed_id}" in text
    # 3 + len(SLASH_COMMANDS) *lines* means 2 + len(SLASH_COMMANDS) newline
    # characters joining them (N lines need N-1 separators).
    assert text.count("\n") == 2 + len(repl.SLASH_COMMANDS)


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
    test_confirm_auto_mode_trust_yes,
    test_confirm_auto_mode_trust_no,
    test_confirm_auto_mode_trust_cancelled,
    test_main_auto_mode_declined_skips_llm_load,
    test_main_prompt_mode_skips_trust_check,
    test_clear_input_esc_empties_nonempty_buffer,
    test_clear_input_esc_noop_on_empty_buffer,
    test_main_seeds_unique_session_id,
    test_get_toolbar_includes_session_id_line,
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

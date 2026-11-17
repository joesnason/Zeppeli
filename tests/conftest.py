import pytest

import core.eventlog as eventlog
import core.sessions as sessions
import core.skills as skills


@pytest.fixture
def tmp_zeppeli_dirs(tmp_path, monkeypatch):
    """Redirects core.sessions.SESSIONS_DIR and core.eventlog.LOGS_DIR into
    a pytest-managed temp directory for the duration of one test, restored
    automatically by `monkeypatch` afterward. Always redirects both dirs —
    a test that only cares about one just ignores the other — since every
    real call site that needs either eventually needs both (ui.repl.main()
    always touches session history AND the event log together). Also
    points core.skills.USER_SKILLS_DIR at an (empty) temp dir, so the
    user's real ~/.zeppeli/skills never leaks into a test's system prompt."""
    sessions_dir = tmp_path / "sessions"
    logs_dir = tmp_path / "logs"
    monkeypatch.setattr(sessions, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(eventlog, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path / "user_skills")
    monkeypatch.setattr(skills, "_active_skills", {})
    return sessions_dir, logs_dir

"""Tests for core/skills.py (skill discovery, frontmatter, skill:// resolution)
and its wiring into read_file()/resolve_paths()/ui.repl.main(). No Ollama or
network dependency."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import core.skills as skills
from core.tools import read_file, resolve_paths
from ui import repl

EXAMPLE_SKILL_MD = """---
name: mail-request-checker
description: "郵件欄位驗證工作流。當使用者要求檢查 mail 資料夾裡的郵件是否包含必要欄位時使用此 skill。"
argument-hint: "mail 資料夾路徑（預設為當前目錄下的 mail/）"
---

# 郵件必填欄位驗證

執行驗證腳本：`python3 ./scripts/validate_mail.py <mail資料夾路徑>`
"""


def _make_skill(root, name, text):
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    return folder


@pytest.fixture
def skill_dirs(tmp_path, monkeypatch):
    user_dir = tmp_path / "home_skills"
    project_cwd = tmp_path / "project"
    user_dir.mkdir()
    project_cwd.mkdir()
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", user_dir)
    monkeypatch.setattr(skills, "_active_skills", {})
    return user_dir, project_cwd, skills.project_skills_dir(str(project_cwd))


# --- parse_frontmatter -------------------------------------------------------

def test_parse_frontmatter_quoted_chinese_description():
    meta = skills.parse_frontmatter(EXAMPLE_SKILL_MD)
    assert meta["name"] == "mail-request-checker"
    assert meta["description"].startswith("郵件欄位驗證工作流")


def test_parse_frontmatter_missing_returns_empty():
    assert skills.parse_frontmatter("# just a heading\n") == {}
    assert skills.parse_frontmatter("") == {}


def test_parse_frontmatter_unterminated_or_invalid_returns_empty():
    assert skills.parse_frontmatter("---\nname: x\n") == {}
    assert skills.parse_frontmatter("---\nname: [unclosed\n---\n") == {}
    assert skills.parse_frontmatter("---\n- a list\n---\n") == {}


# --- discover_skills ---------------------------------------------------------

def test_discover_example_skill_loaded_by_default(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "mail-request-checker", EXAMPLE_SKILL_MD)
    found = skills.discover_skills(str(cwd))
    assert list(found) == ["mail-request-checker"]
    assert found["mail-request-checker"].description.startswith("郵件欄位驗證工作流")


def test_discover_project_overrides_user_and_merges(skill_dirs):
    user_dir, cwd, project_dir = skill_dirs
    _make_skill(user_dir, "shared", "---\ndescription: from user\n---\n")
    _make_skill(user_dir, "user-only", "---\ndescription: u\n---\n")
    _make_skill(project_dir, "shared", "---\ndescription: from project\n---\n")
    found = skills.discover_skills(str(cwd))
    assert list(found) == ["shared", "user-only"]
    assert found["shared"].description == "from project"
    assert found["shared"].directory == (project_dir / "shared").resolve()


def test_discover_folder_name_is_skill_name(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "folder-name", "---\nname: other-name\ndescription: d\n---\n")
    assert list(skills.discover_skills(str(cwd))) == ["folder-name"]


def test_discover_skips_folder_without_skill_md(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    (user_dir / "empty").mkdir()
    assert skills.discover_skills(str(cwd)) == {}


def test_discover_excludes_disable_model_invocation_true(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "hidden", "---\ndescription: d\ndisable-model-invocation: true\n---\n")
    _make_skill(user_dir, "shown", "---\ndescription: d\ndisable-model-invocation: false\n---\n")
    assert list(skills.discover_skills(str(cwd))) == ["shown"]


def test_discover_missing_dirs_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path / "nope")
    assert skills.discover_skills(str(tmp_path / "also-nope")) == {}


# --- build_skills_prompt -----------------------------------------------------

def test_build_skills_prompt_empty_is_empty_string():
    assert skills.build_skills_prompt({}) == ""


def test_build_skills_prompt_lists_skills_and_uri_syntax(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "mail-request-checker", EXAMPLE_SKILL_MD)
    _make_skill(user_dir, "no-desc", "no frontmatter at all\n")
    prompt = skills.build_skills_prompt(skills.discover_skills(str(cwd)))
    assert "- mail-request-checker: 郵件欄位驗證工作流" in prompt
    assert "- no-desc\n" in prompt
    assert 'read_file(path="skill://<name>")' in prompt
    assert "skill://<name>/<relative path>" in prompt


# --- resolve_skill_uri -------------------------------------------------------

def test_resolve_skill_uri_variants(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    folder = _make_skill(user_dir, "s", "---\ndescription: d\n---\n").resolve()
    skills.load_skills(str(cwd))
    assert skills.resolve_skill_uri("skill://s")[0] == folder / "SKILL.md"
    assert skills.resolve_skill_uri("skill://s/")[0] == folder / "SKILL.md"
    assert skills.resolve_skill_uri("skill://s/scripts/a.py")[0] == folder / "scripts" / "a.py"


def test_resolve_skill_uri_errors(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "s", "---\ndescription: d\n---\n")
    _make_skill(user_dir, "t", "---\ndescription: d\n---\n")
    skills.load_skills(str(cwd))
    with pytest.raises(skills.SkillError, match="unknown skill"):
        skills.resolve_skill_uri("skill://missing")
    with pytest.raises(skills.SkillError, match="outside"):
        skills.resolve_skill_uri("skill://s/../t/SKILL.md")
    with pytest.raises(skills.SkillError, match="missing skill name"):
        skills.resolve_skill_uri("skill://")


# --- read_file / resolve_paths integration -----------------------------------

def test_resolve_paths_leaves_skill_uri_untouched(tmp_path):
    args = resolve_paths("read_file", {"path": "skill://s/x.md"}, str(tmp_path))
    assert args["path"] == "skill://s/x.md"


def test_read_file_skill_uri_reads_skill_md_and_subpath(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    folder = _make_skill(user_dir, "mail-request-checker", EXAMPLE_SKILL_MD)
    (folder / "scripts").mkdir()
    (folder / "scripts" / "validate_mail.py").write_text("print('ok')\n", encoding="utf-8")
    skills.load_skills(str(cwd))

    out = read_file.invoke({"path": "skill://mail-request-checker"})
    assert out.startswith("[File: skill://mail-request-checker | skill dir: ")
    assert str(folder.resolve()) in out.splitlines()[0]
    assert "# 郵件必填欄位驗證" in out
    assert out.endswith("[End of file]")

    out = read_file.invoke({"path": "skill://mail-request-checker/scripts/validate_mail.py"})
    assert "print('ok')" in out


def test_read_file_skill_uri_errors_are_strings(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "s", "---\ndescription: d\n---\n")
    skills.load_skills(str(cwd))
    assert read_file.invoke({"path": "skill://nope"}).startswith("[read_file] Error: unknown skill")
    assert read_file.invoke({"path": "skill://s/missing.txt"}) == \
        "[read_file] Error: file not found: skill://s/missing.txt"


def test_disabled_skill_not_readable(skill_dirs):
    user_dir, cwd, _ = skill_dirs
    _make_skill(user_dir, "hidden", "---\ndisable-model-invocation: true\n---\n")
    skills.load_skills(str(cwd))
    assert "unknown skill" in read_file.invoke({"path": "skill://hidden"})


# --- ui.repl.main() integration ----------------------------------------------

def test_main_system_prompt_lists_skills(tmp_zeppeli_dirs, tmp_path, monkeypatch):
    user_dir = tmp_path / "user_skills"
    _make_skill(user_dir, "mail-request-checker", EXAMPLE_SKILL_MD)
    monkeypatch.setattr(repl, "load_llm", lambda **k: object())
    monkeypatch.setattr(repl, "model_supports_reasoning", lambda *a, **k: False)
    monkeypatch.setattr(repl, "get_context_window", lambda *a, **k: None)
    captured = {}

    async def _fake_run_turn(llm_with_tools, messages, user_input, console, live, initial_cwd,
                              mode, images=None, session_id=None, run_id=None, reasoning=False,
                              context_window=None):
        captured["system"] = messages[0].content
        messages.append(HumanMessage(content=user_input))
        messages.append(AIMessage(content="ok"))

    monkeypatch.setattr(repl, "run_turn", _fake_run_turn)
    repl.main(prompt="hi", mode="approval")

    assert "Available skills" in captured["system"]
    assert "- mail-request-checker: 郵件欄位驗證工作流" in captured["system"]
    assert captured["system"].rstrip().splitlines()[-1].startswith("Working directory:")


def test_main_system_prompt_unchanged_without_skills(tmp_zeppeli_dirs, monkeypatch):
    monkeypatch.setattr(repl, "load_llm", lambda **k: object())
    monkeypatch.setattr(repl, "model_supports_reasoning", lambda *a, **k: False)
    monkeypatch.setattr(repl, "get_context_window", lambda *a, **k: None)
    captured = {}

    async def _fake_run_turn(llm_with_tools, messages, user_input, console, live, initial_cwd,
                              mode, images=None, session_id=None, run_id=None, reasoning=False,
                              context_window=None):
        captured["system"] = messages[0].content

    monkeypatch.setattr(repl, "run_turn", _fake_run_turn)
    repl.main(prompt="hi", mode="approval")
    assert "Available skills (specialized" not in captured["system"]

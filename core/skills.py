"""Skill discovery and `skill://` resolution — no UI dependencies.

A skill is a folder containing a SKILL.md, found under either the user-level
`~/.zeppeli/skills/` or the project-level `<launch cwd>/.zeppeli/skills/`
(the project level wins on a name clash). The folder name is the skill
name. SKILL.md starts with YAML frontmatter:

    ---
    name: mail-request-checker
    description: "What the skill does and when to use it."
    disable-model-invocation: false   # optional, default false
    ---

`load_skills()` is called once at session start (ui/repl.py's main(),
slack_bot.py's startup); `build_skills_prompt()` turns the result into a
system-prompt section; core/tools.py's read_file() resolves
`skill://<name>[/<path>]` through `resolve_skill_uri()`. See docs/skills.md.
"""

import pathlib
from dataclasses import dataclass

import yaml

SKILL_URI_PREFIX = "skill://"
SKILL_FILENAME = "SKILL.md"

USER_SKILLS_DIR = pathlib.Path.home() / ".zeppeli" / "skills"


def project_skills_dir(cwd: str) -> pathlib.Path:
    return pathlib.Path(cwd) / ".zeppeli" / "skills"


class SkillError(Exception):
    """A `skill://` URI that can't be resolved — unknown skill, path escaping
    the skill's folder, or a malformed URI. read_file() turns this into a
    plain `[read_file] Error: ...` string."""


@dataclass
class Skill:
    name: str
    description: str
    directory: pathlib.Path
    disable_model_invocation: bool = False


# The skills discovered by the most recent load_skills() call. Module-level
# because read_file() is a plain @tool with no session context to thread
# a registry through; one process = one launch directory, so one registry.
_active_skills: dict[str, Skill] = {}


def parse_frontmatter(text: str) -> dict:
    """Return the YAML frontmatter at the top of `text` as a dict, or {} if
    there is none, it's invalid YAML, or it isn't a mapping. Never raises."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:i]))
            except yaml.YAMLError:
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "on")
    return bool(value)


def _scan_dir(root: pathlib.Path) -> dict[str, Skill]:
    found: dict[str, Skill] = {}
    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for folder in entries:
        skill_md = folder / SKILL_FILENAME
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta = parse_frontmatter(text)
        description = meta.get("description")
        found[folder.name] = Skill(
            name=folder.name,
            description=" ".join(str(description).split()) if description is not None else "",
            directory=folder.resolve(),
            disable_model_invocation=_as_bool(meta.get("disable-model-invocation", False)),
        )
    return found


def discover_skills(cwd: str) -> dict[str, Skill]:
    """Scan the user-level then project-level skills directories (project
    overrides user on a name clash), dropping skills that set
    `disable-model-invocation: true`. Sorted by name. Never raises."""
    merged = _scan_dir(USER_SKILLS_DIR)
    merged.update(_scan_dir(project_skills_dir(cwd)))
    return {name: merged[name] for name in sorted(merged)
            if not merged[name].disable_model_invocation}


def load_skills(cwd: str) -> dict[str, Skill]:
    """Discover skills for `cwd` and make them the active registry that
    `skill://` URIs resolve against."""
    global _active_skills
    _active_skills = discover_skills(cwd)
    return dict(_active_skills)


def get_active_skills() -> dict[str, Skill]:
    return dict(_active_skills)


def build_skills_prompt(skills: dict[str, Skill]) -> str:
    """System-prompt section listing `skills`, or "" when there are none (so
    the prompt is byte-identical to a skill-less launch)."""
    if not skills:
        return ""
    listing = "\n".join(
        f"- {s.name}: {s.description}" if s.description else f"- {s.name}"
        for s in skills.values()
    )
    return (
        "\n\nAvailable skills (specialized instructions for specific tasks):\n"
        f"{listing}\n\n"
        "When the user's request matches a skill's description, first read its full "
        'instructions with read_file(path="skill://<name>") before doing the task, then '
        "follow them. Files the skill references by relative path (e.g. ./scripts/foo.py) "
        'are read with read_file(path="skill://<name>/<relative path>"). The read_file '
        "header shows the skill's real directory — use that absolute path when the skill "
        "tells you to run one of its scripts with run_bash. Only read_file understands "
        "skill:// paths."
    )


def is_skill_uri(path) -> bool:
    return isinstance(path, str) and path.startswith(SKILL_URI_PREFIX)


def resolve_skill_uri(uri: str) -> tuple[pathlib.Path, Skill]:
    """Map `skill://<name>[/<path>]` to a real file path inside that skill's
    folder (`SKILL.md` when no path is given). Raises SkillError for an
    unknown skill or a path that escapes the skill's folder."""
    if not is_skill_uri(uri):
        raise SkillError(f"not a skill:// URI: {uri}")
    rest = uri[len(SKILL_URI_PREFIX):]
    name, _, sub = rest.partition("/")
    if not name:
        raise SkillError(f"missing skill name in {uri!r} — use skill://<name>")
    skill = _active_skills.get(name)
    if skill is None:
        available = ", ".join(_active_skills) or "(none)"
        raise SkillError(f"unknown skill {name!r}. Available skills: {available}")
    sub = sub.strip("/")
    base = skill.directory.resolve()
    target = (base / sub).resolve() if sub else base / SKILL_FILENAME
    if not target.is_relative_to(base):
        raise SkillError(f"{uri!r} points outside the skill's folder")
    return target, skill

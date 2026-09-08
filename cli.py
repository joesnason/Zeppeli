"""Entry point: `python3 cli.py` starts the interactive REPL (see ui/repl.py).

Permission mode is chosen once at launch via CLI flag (default: approval):
  --yolo-mode   EXTREMELY DANGEROUS — skip all permission prompts entirely;
                the AI can write/delete any file it can reach, unconfirmed
  --auto-mode   auto-approve writes/deletes inside the launch directory;
                prompt for anything outside it

-p/--prompt PROMPT runs one turn non-interactively and exits (no REPL);
combine it with either mode flag as needed.

--base-url/--model/--api-key (each also settable via LITELLM_BASE_URL/
LITELLM_MODEL/LITELLM_API_KEY, or via config.json — flag > env var >
config.json) switch to a cloud/self-hosted model via litellm instead of
local Ollama — see docs/models.md.

--image PATH (repeatable) attaches a local image file — see the "Vision /
image input" section of docs/models.md.
"""

import argparse
import json
import os
import pathlib
import sys

from core.images import is_image_path
from ui import MODE_APPROVAL, MODE_AUTO, MODE_YOLO, main


def _build_parser():
    parser = argparse.ArgumentParser(prog="cli.py", description="Ollama CLI chat")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--yolo-mode", action="store_true",
        help="EXTREMELY DANGEROUS: skip all permission prompts for "
             "write_file/delete_file calls. The AI can overwrite or delete "
             "any file it can reach, with no confirmation and no way to "
             "stop it beforehand. Use only if you fully trust the prompts "
             "you give it.",
    )
    group.add_argument(
        "--auto-mode", action="store_true",
        help="Auto-approve write_file/delete_file calls inside the launch "
             "directory; prompt for calls outside it.",
    )
    parser.add_argument(
        "-p", "--prompt", type=str, default=None, metavar="PROMPT",
        help="Run one turn non-interactively with PROMPT as input, print "
             "the response, and exit (skips the REPL). Combine with "
             "--yolo-mode or --auto-mode as needed.",
    )
    parser.add_argument(
        "--base-url", type=str, default=None, metavar="URL",
        help="Use a cloud/self-hosted model via litellm (OpenAI-compatible "
             "endpoint) instead of local Ollama. Requires --model. Can "
             "also be set via the LITELLM_BASE_URL env var.",
    )
    parser.add_argument(
        "--model", type=str, default=None, metavar="NAME",
        help="Model name/tag. Without --base-url, overrides the local "
             "Ollama model tag (default set in core/agent.py). With "
             "--base-url, this is required and must follow litellm's "
             "provider-prefix convention, e.g. openai/gpt-4o-mini. Can "
             "also be set via the LITELLM_MODEL env var.",
    )
    parser.add_argument(
        "--api-key", type=str, default=None, metavar="KEY",
        help="API key for the cloud endpoint (--base-url). Falls back to "
             "the LITELLM_API_KEY env var, then to litellm's own "
             "provider-specific env vars (e.g. OPENAI_API_KEY) if neither "
             "is set.",
    )
    parser.add_argument(
        "--image", type=str, action="append", default=None, metavar="PATH",
        help="Attach a local image file to the turn (repeatable, max 4). "
             "Local paths only — no URLs. Downscaled to 1568px on the long "
             "edge before sending. Requires a vision-capable model. With "
             "-p/--prompt, attaches to that one turn; without it, attaches "
             "to your first message in the REPL.",
    )
    return parser


def _parse_args(argv: list[str] | None = None):
    return _build_parser().parse_args(argv)


# Optional git-ignored config file for model/base_url/api_key defaults —
# see config.json.example. Anchored to this file's own directory (not
# cwd) so it works regardless of launch directory. Exposed as a module
# attribute (rather than inlined in _load_config_file) specifically so
# tests can monkeypatch it to a tmp_path location instead of ever
# touching a real repo-root file.
_CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"
_CONFIG_KEYS = ("model", "base_url", "api_key", "slack")
_SLACK_CONFIG_KEYS = ("bot_token", "app_token", "allowed_dir", "allowed_users")


def _read_json_config() -> dict | None:
    """Read + parse _CONFIG_PATH as a JSON object. Never raises:
      - missing file -> None silently (the common case; most launches
        won't have one).
      - unreadable / invalid JSON / not a JSON object -> None plus a
        one-line stderr warning — this file is a best-effort convenience
        layer, not something that should block every future invocation
        until a stale/corrupt file is noticed and fixed.
    Shared by _load_config_file() (flat model/base_url/api_key keys) and
    _load_slack_config() (nested "slack" key) so this read/parse/warn
    logic isn't duplicated between them. Not cached: reads a tiny local
    file a handful of times per process startup, deliberately not
    memoized so tests can monkeypatch _CONFIG_PATH per-test without
    stale results.
    """
    try:
        text = _CONFIG_PATH.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"warning: could not read {_CONFIG_PATH}: {e}", file=sys.stderr)
        return None

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(
            f"warning: {_CONFIG_PATH} is not valid JSON ({e}); ignoring it",
            file=sys.stderr,
        )
        return None

    if not isinstance(data, dict):
        print(
            f"warning: {_CONFIG_PATH} must contain a JSON object; ignoring it",
            file=sys.stderr,
        )
        return None

    return data


def _load_config_file() -> dict:
    """Load the optional config.json (repo root, next to cli.py) as a
    dict of model/base_url/api_key defaults — the lowest-precedence tier,
    below flag and env var. Never raises:
      - missing/unreadable/malformed file -> {} (see _read_json_config()).
      - unrecognized top-level keys -> ignored, plus a one-line stderr
        warning naming them (catches typos like "modle" without blocking
        startup). "slack" is a recognized key here too (see
        _load_slack_config()) so it never triggers this warning, but its
        value is a dict, not a string, so it's excluded below regardless.
      - a recognized key whose value isn't a string -> treated as absent
        for that key, silently.
    """
    data = _read_json_config()
    if data is None:
        return {}

    unknown = sorted(set(data) - set(_CONFIG_KEYS))
    if unknown:
        print(
            f"warning: {_CONFIG_PATH} has unrecognized key(s) {unknown}; "
            f"ignoring them (valid keys: {list(_CONFIG_KEYS)})",
            file=sys.stderr,
        )

    return {k: v for k, v in data.items() if k in _CONFIG_KEYS and isinstance(v, str)}


def _load_slack_config() -> dict:
    """Load the optional "slack" object from config.json (bot_token,
    app_token, allowed_dir, allowed_users) — see config.json.example and
    docs/slack.md. Never raises; stays as best-effort as _load_config_file():
      - missing "slack" key, or the whole file missing/malformed -> {}.
      - "slack" present but not a JSON object -> {} plus a warning.
      - unrecognized nested keys -> ignored, plus a warning naming them.
      - bot_token/app_token/allowed_dir must be non-empty strings, or
        they're treated as absent; allowed_users must be a list of
        strings, or it's dropped (an empty/omitted allowed_users means
        "anyone is allowed" — see slack_bot/access.py).
    Unlike _load_config_file(), a missing/invalid result here isn't a
    silently-tolerable fallback tier — slack_bot.py has no flag/env-var
    escape hatch for these values, so it's the caller's job to treat a
    missing bot_token/app_token/allowed_dir as fatal, not this function's.
    """
    data = _read_json_config()
    if data is None:
        return {}

    slack = data.get("slack")
    if slack is None:
        return {}
    if not isinstance(slack, dict):
        print(
            f'warning: {_CONFIG_PATH}\'s "slack" key must be a JSON object; ignoring it',
            file=sys.stderr,
        )
        return {}

    unknown = sorted(set(slack) - set(_SLACK_CONFIG_KEYS))
    if unknown:
        print(
            f'warning: {_CONFIG_PATH}\'s "slack" object has unrecognized key(s) '
            f'{unknown}; ignoring them (valid keys: {list(_SLACK_CONFIG_KEYS)})',
            file=sys.stderr,
        )

    result = {}
    for key in ("bot_token", "app_token", "allowed_dir"):
        value = slack.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    users = slack.get("allowed_users")
    if isinstance(users, list) and all(isinstance(u, str) for u in users):
        result["allowed_users"] = users
    return result


def _resolve_base_url(args) -> str | None:
    return args.base_url or os.environ.get("LITELLM_BASE_URL") or _load_config_file().get("base_url")


def _resolve_model(args) -> str | None:
    return args.model or os.environ.get("LITELLM_MODEL") or _load_config_file().get("model")


def _resolve_api_key(args) -> str | None:
    return args.api_key or os.environ.get("LITELLM_API_KEY") or _load_config_file().get("api_key")


def _resolve_config(args):
    """Resolve model/base_url/api_key as flag > env var > config.json >
    None, and validate that a resolved base_url always comes with a
    resolved model."""
    base_url = _resolve_base_url(args)
    model = _resolve_model(args)
    api_key = _resolve_api_key(args)
    if base_url and not model:
        _build_parser().error(
            "--model (or LITELLM_MODEL) is required when --base-url "
            "(or LITELLM_BASE_URL) is set"
        )
    return model, base_url, api_key


def _resolve_images(args) -> list[str]:
    """Validate --image paths eagerly (existence + extension) so a typo
    fails fast with SystemExit(2), same as _resolve_config's validation,
    instead of surfacing later as an opaque ImageError mid-turn. Kept
    separate from _resolve_config on purpose — folding it in would change
    that function's return arity and break its existing tests."""
    paths = []
    for raw in (args.image or []):
        p = pathlib.Path(raw).expanduser()
        if not p.is_file():
            _build_parser().error(f"--image: file not found: {raw}")
        if not is_image_path(str(p)):
            _build_parser().error(f"--image: unsupported image type: {raw}")
        paths.append(str(p))
    return paths


if __name__ == "__main__":
    args = _parse_args()
    if args.yolo_mode:
        mode = MODE_YOLO
    elif args.auto_mode:
        mode = MODE_AUTO
    else:
        mode = MODE_APPROVAL
    model, base_url, api_key = _resolve_config(args)
    images = _resolve_images(args)
    main(mode=mode, prompt=args.prompt, model=model, base_url=base_url,
         api_key=api_key, images=images)

"""Entry point: `python3 cli.py` starts the interactive REPL (see ui/repl.py).

Permission mode is chosen once at launch via CLI flag (default: approval):
  --yolo-mode   EXTREMELY DANGEROUS — skip all permission prompts entirely;
                the AI can write/delete any file it can reach, unconfirmed
  --auto-mode   auto-approve writes/deletes inside the launch directory;
                prompt for anything outside it

-p/--prompt PROMPT runs one turn non-interactively and exits (no REPL);
combine it with either mode flag as needed.

--base-url/--model/--api-key (each also settable via LITELLM_BASE_URL/
LITELLM_MODEL/LITELLM_API_KEY) switch to a cloud/self-hosted model via
litellm instead of local Ollama — see docs/models.md.
"""

import argparse
import os

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
    return parser


def _parse_args(argv: list[str] | None = None):
    return _build_parser().parse_args(argv)


def _resolve_base_url(args) -> str | None:
    return args.base_url or os.environ.get("LITELLM_BASE_URL")


def _resolve_model(args) -> str | None:
    return args.model or os.environ.get("LITELLM_MODEL")


def _resolve_api_key(args) -> str | None:
    return args.api_key or os.environ.get("LITELLM_API_KEY")


def _resolve_config(args):
    """Resolve model/base_url/api_key as flag > env var > None, and
    validate that a resolved base_url always comes with a resolved model."""
    base_url = _resolve_base_url(args)
    model = _resolve_model(args)
    api_key = _resolve_api_key(args)
    if base_url and not model:
        _build_parser().error(
            "--model (or LITELLM_MODEL) is required when --base-url "
            "(or LITELLM_BASE_URL) is set"
        )
    return model, base_url, api_key


if __name__ == "__main__":
    args = _parse_args()
    if args.yolo_mode:
        mode = MODE_YOLO
    elif args.auto_mode:
        mode = MODE_AUTO
    else:
        mode = MODE_APPROVAL
    model, base_url, api_key = _resolve_config(args)
    main(mode=mode, prompt=args.prompt, model=model, base_url=base_url, api_key=api_key)

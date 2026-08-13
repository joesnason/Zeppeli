"""Entry point: `python3 cli.py` starts the interactive REPL (see ui/repl.py).

Permission mode is chosen once at launch via CLI flag (default: approval):
  --yolo-mode   skip all permission prompts entirely
  --auto-mode   auto-approve writes/deletes inside the launch directory;
                prompt for anything outside it

-p/--prompt PROMPT runs one turn non-interactively and exits (no REPL);
combine it with either mode flag as needed.
"""

import argparse

from ui import MODE_APPROVAL, MODE_AUTO, MODE_YOLO, main


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(prog="cli.py", description="Ollama CLI chat")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--yolo-mode", action="store_true",
        help="Skip all permission prompts for write_file/delete_file calls.",
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
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    if args.yolo_mode:
        mode = MODE_YOLO
    elif args.auto_mode:
        mode = MODE_AUTO
    else:
        mode = MODE_APPROVAL
    main(mode=mode, prompt=args.prompt)

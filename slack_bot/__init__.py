"""Slack integration layer — a third, independent surface alongside `ui/`'s
terminal surface, reusing `core/`'s UI-independent agent logic plus
`ui/turn.py`'s `run_turn()` / `ui/streaming.py`'s `stream_response()` /
`ui/permissions.py`'s `build_pre_tool_hooks()` (all already written
against an abstract `console`/`live` duck-typed interface, not against
the terminal directly — no changes needed to any of those three modules).

Deliberately does NOT import `ui.repl`/`ui.live_region`/`ui.completion`
(terminal-Application-specific) — only `ui.turn`/`ui.streaming`/
`ui.permissions`, none of which depend on prompt_toolkit's `Application`.
Not imported by `core/` or `ui/` (one-way, mirroring the core<-ui rule).

See docs/slack.md for setup and the config.json "slack" schema.
"""

"""Access control: who may trigger the bot.

Deliberately trivial and dependency-free — see docs/slack.md's Access
Control section for the intended workflow: start with a short
`allowed_users` list (just your own Slack user id) for testing, then
widen access later by emptying the list out in config.json.
"""


def is_allowed(user_id: str, allowed_users: list[str] | None) -> bool:
    """True if `user_id` may trigger the bot. An empty/omitted
    `allowed_users` means "anyone in the channel" (open by default);
    a non-empty list restricts to exactly those Slack user ids."""
    return not allowed_users or user_id in allowed_users

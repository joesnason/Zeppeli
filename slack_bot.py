"""Entry point: `python3 slack_bot.py` starts the Slack bot (Socket Mode).

Purely config.json-driven — no CLI flags, no env vars. See
config.json.example's "slack" block and docs/slack.md for setup
(creating the Slack app, enabling Socket Mode, required OAuth scopes).

One model (loaded once via core.agent.load_llm()) serves every Slack
thread; each thread is its own independent conversation (see
slack_bot/sessions.py). Tool calls run in MODE_AUTO, scoped to the
config's "allowed_dir" — see slack_bot/live.py's ask_menu() and
docs/slack.md's security-consideration section for what that does and
doesn't protect against.
"""

import asyncio
import sys

import aiohttp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

import cli
from core.agent import get_context_window, load_llm
from ui.permissions import MODE_AUTO
from slack_bot.handlers import register_handlers
from slack_bot.sessions import ThreadRegistry


def _load_and_validate_slack_config() -> dict:
    slack_cfg = cli._load_slack_config()
    missing = [k for k in ("bot_token", "app_token", "allowed_dir") if not slack_cfg.get(k)]
    if missing:
        print(
            f"error: config.json's \"slack\" object is missing required key(s): {missing}\n"
            f"Copy config.json.example to config.json and fill in the \"slack\" block "
            f"(bot_token, app_token, allowed_dir) — see docs/slack.md.",
            file=sys.stderr,
        )
        sys.exit(1)
    return slack_cfg


async def _run() -> None:
    slack_cfg = _load_and_validate_slack_config()
    model_cfg = cli._load_config_file()
    model = model_cfg.get("model")
    base_url = model_cfg.get("base_url")
    api_key = model_cfg.get("api_key")

    llm_with_tools = load_llm(model=model, base_url=base_url, api_key=api_key)
    context_window = get_context_window(model) if not base_url else None

    app = AsyncApp(token=slack_cfg["bot_token"])
    auth = await app.client.auth_test()
    bot_user_id = auth["user_id"]

    registry = ThreadRegistry()
    async with aiohttp.ClientSession() as http_session:
        register_handlers(
            app,
            llm_with_tools=llm_with_tools,
            initial_cwd=slack_cfg["allowed_dir"],
            allowed_users=slack_cfg.get("allowed_users"),
            registry=registry,
            bot_user_id=bot_user_id,
            model_name=model,
            mode=MODE_AUTO,
            bot_token=slack_cfg["bot_token"],
            http_session=http_session,
            context_window=context_window,
        )

        print(f"Zeppeli Slack bot connected (bot user: {bot_user_id}, allowed_dir: {slack_cfg['allowed_dir']})")
        handler = AsyncSocketModeHandler(app, slack_cfg["app_token"])
        await handler.start_async()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\nBye!")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

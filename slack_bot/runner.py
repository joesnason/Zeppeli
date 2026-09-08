"""Runs one turn for a Slack thread and persists it — mirrors
ui/repl.py's `_run_and_persist()` almost line for line (same
core/sessions.py + core/eventlog.py calls, same "save a 'running' stub
before the turn so a crash leaves an honest on-disk record, swallow any
persistence-layer bug so it never breaks the user-visible reply" shape).
The one addition beyond that mirror: since there's no terminal to fall
back on, a failed run posts one explicit error line to the Slack thread
directly.
"""

import time

from core.eventlog import build_turns_and_outputs, log_run_completed, log_run_started
from core.messages import extract_text
from core.sessions import append_history_from_messages, finish_run_from_messages, save_session, start_run
from ui.turn import run_turn


async def run_and_persist(llm_with_tools, thread_session, user_input: str, console, live,
                           initial_cwd: str, mode: str, context_window: int | None) -> None:
    session = thread_session.history_session
    start_index = len(thread_session.messages)
    run = start_run()
    session.runs.append(run)
    save_session(session)
    log_run_started(thread_session.session_id, run.id, user_input)

    t0 = time.monotonic()
    await run_turn(llm_with_tools, thread_session.messages, user_input, console, live,
                    initial_cwd, mode, session_id=thread_session.session_id, run_id=run.id,
                    context_window=context_window)
    # Make sure the last streamed/finalized Slack message has actually
    # landed before persisting/logging the completed run — SlackLive's
    # update sends are fire-and-forget tasks internally (see
    # slack_bot/live.py), flush() is the one place that awaits them.
    await live.flush()
    duration_ms = int((time.monotonic() - t0) * 1000)

    try:
        new_messages = thread_session.messages[start_index:]
        if not new_messages:
            # e.g. an ImageError before anything was appended (run_turn()'s
            # earliest return) — nothing happened; drop the empty stub run.
            session.runs.remove(run)
        else:
            finish_run_from_messages(run, new_messages, duration_ms)
            append_history_from_messages(session, thread_session.messages, start_index)
            answer = extract_text(new_messages[-1].content) if run.status == "completed" else ""
            turns, model_outputs = build_turns_and_outputs(new_messages)
            log_run_completed(thread_session.session_id, run.id, status=run.status, answer=answer,
                               stats=run.stats, turns=turns, model_outputs=model_outputs,
                               error=run.error)
            if run.status == "failed":
                # No terminal to surface a model-call failure in — post
                # one explicit line so it isn't silently invisible.
                await live.post_error(run.error or "the run failed with no error message.")
        save_session(session)
    except Exception:
        pass

# Known Issues

## Reasoning model "goes silent" mid-task — decides next step in `thinking`, never emits it as a tool call or answer

**Status:** open, not yet root-caused as a code bug — appears to be model behavior, not a Zeppeli bug.

**Observed:** 2026-09-04, session `04012db5-05a2-4e53-ac72-d0167af84246`
(`~/.zeppeli/logs/log-04012db5-05a2-4e53-ac72-d0167af84246.jsonl`), model
`gemma4:26b-nvfp4`, reasoning mode enabled (local Ollama).

**Symptom:** the user asked Zeppeli to look at `logs/build.log` and find why
a build failed. It ran 7 tool calls (`list_files` → `rg_search` → `read_file`
→ narrowing `rg_search` calls) and correctly narrowed the failure down to:

```
FAILED: out/target/product/qssi/obj/APPS/VrsLauncher_intermediates/package.apk
<stdin>:297:248: error: \x used with no following hex digits
```

On the 8th (final) hop, the model's `reasoning_content` shows it decided on
a next step —

> "...I'll search for the lines *before* that `FAILED` line to see what
> failed. I'll use `rg` to find the line number of that specific string."

— but never issued the corresponding `rg_search` tool call, and the visible
`content` for that hop was also empty (`""`). Since there were no
`tool_calls`, `run_turn()`'s loop ended normally: the run's
`completionStatus` is `"completed"`, `error: None`, no `cli_error` — nothing
crashed. From the user's side, Zeppeli just stopped producing output
mid-task with no explanation, as if it had frozen or given up.

**Confirmed NOT the cause:**
- Not tier-1/tier-2 context compaction (`core/messages.py`) — only 8 hops,
  6,806 input / 241 output tokens, far under both the 25-turn and
  80%-of-context-window triggers.
- Not `truncate_tool_output()` cutting off something the model needed — the
  narrowing `rg_search` calls did get capped at 50,000 bytes on the two
  broad early searches, but the final, narrowly-scoped calls (418 and 2,704
  bytes) were well under the cap and returned intact.
- No exception anywhere in the log (`cli_error` never fired).

**Working theory:** with `reasoning=True` (`ui/streaming.py`'s
`stream_response()`, `core/agent.py`'s `model_supports_reasoning()`), the
model can apparently spend an entire hop's token budget deciding on an
action inside `additional_kwargs["reasoning_content"]` and then fail to
actually emit that action (or any answer) in the visible response —
starving both `response.tool_calls` and `response.content` in the same hop.
Possibly correlates with longer reasoning chains (this hop's `thinking` was
673 chars, the longest of the run) or with local/quantized models under
reasoning mode specifically; unconfirmed with a single occurrence.

**Workaround:** re-prompt (e.g. "continue — you said you'd search for the
line number") — the model typically picks the thread back up.

**Possible future mitigations (not implemented):**
- Detect a hop where `tool_calls` is empty AND `content` is empty (a
  "silent" hop) and surface it distinctly in the UI/toolbar instead of
  silently ending the turn, so the user isn't left wondering whether it
  crashed.
- Consider whether `_reasoning_unsupported`-style tracking should also
  cover "reasoning produced no usable output" as a distinct signal, separate
  from the existing raised-exception fallback path.
- Needs more occurrences (ideally across different prompts/models) before
  concluding this is systemic rather than a one-off.

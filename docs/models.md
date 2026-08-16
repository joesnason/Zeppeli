# Models: local Ollama vs. cloud via litellm

`core/agent.py`'s `load_llm(model, base_url, api_key)` is the single place
that decides which backend to build:

```python
def load_llm(model: str | None = None, base_url: str | None = None, api_key: str | None = None):
    if base_url:
        from langchain_litellm import ChatLiteLLM  # lazy: keep litellm optional

        kwargs = {"model": model, "api_base": base_url}
        if api_key:
            kwargs["api_key"] = api_key
        return ChatLiteLLM(**kwargs).bind_tools(TOOLS)
    return ChatOllama(model=model or MODEL).bind_tools(TOOLS)
```

- `base_url` truthy → cloud/self-hosted model via
  [`langchain-litellm`](https://github.com/langchain-ai/langchain-litellm)'s
  `ChatLiteLLM`. `langchain_litellm` is imported *inside* this branch, not
  at module scope — so `langchain-litellm`/`litellm` stay effectively
  optional: nothing that only ever uses local Ollama needs them installed
  or working.
- Otherwise → local `ChatOllama`, `model` falling back to the `MODEL`
  constant — today's behavior, unchanged.
- `api_key` is only added to the kwargs when truthy; it's never passed as
  `api_key=None`.
- `model` is passed through as-is — **no automatic `openai/` prefixing**
  (see below).

Both branches call `.bind_tools(TOOLS)` the same way, so everything
downstream (`ui/turn.py`'s tool-call loop, `ui/streaming.py`'s
token-by-token rendering) works against the same LangChain chat-model
interface once bound. One shape difference does leak through, though: chunk
`.content` itself. `ChatOllama` chunks give a plain `str`; some
litellm-routed backends give a list of content blocks instead — see
"`AIMessageChunk.content` as a list of blocks" below. `ui/streaming.py`'s
`_extract_text()` normalizes both shapes before Markdown rendering, so
callers of `stream_response()` don't need to care which backend loaded.

## CLI flags and env-var fallbacks

Each of the three cloud-related settings can come from a flag or an env
var, flag taking precedence (`cli.py`'s `_resolve_base_url()` /
`_resolve_model()` / `_resolve_api_key()`):

| Setting | Flag | Env var |
|---|---|---|
| Base URL | `--base-url URL` | `LITELLM_BASE_URL` |
| Model | `--model NAME` | `LITELLM_MODEL` |
| API key | `--api-key KEY` | `LITELLM_API_KEY` |

`cli.py`'s `_resolve_config(args)` resolves all three (flag > env var >
`None`) and validates that a resolved `base_url` never shows up without a
resolved `model` — there's no sensible default model string for a cloud
endpoint. If that happens, it errors via the same mechanism as the
`--yolo-mode`/`--auto-mode` mutex group: `parser.error(...)`, `SystemExit(2)`,
usage printed to stderr.

If `api_key` resolves to `None` (no flag, no `LITELLM_API_KEY`), nothing is
passed to `ChatLiteLLM` at all — litellm then falls back to its own
provider-specific env vars internally (e.g. `OPENAI_API_KEY` for an
`openai/`-prefixed model). A key is never required.

`--model` alone (no `--base-url`/`LITELLM_BASE_URL`) overrides the local
Ollama model tag — this is the CLI-level override that was previously only
possible by editing the `MODEL` constant in `core/agent.py` directly.

## The `openai/` prefix convention for custom endpoints

litellm identifies which provider adapter to use from the `model` string's
prefix, not from `base_url` alone. For a custom/self-hosted OpenAI-compatible
endpoint, the documented convention (docs.litellm.ai) is:

```bash
python3 cli.py --base-url https://your-endpoint.example.com --model openai/gpt-4o-mini --api-key sk-...
```

- `--model` needs the `openai/` prefix — this project does **not** add it
  for you.
- `--base-url` should **not** have a trailing `/v1`.
- Env-var equivalent: `LITELLM_BASE_URL=https://your-endpoint.example.com LITELLM_MODEL=openai/gpt-4o-mini LITELLM_API_KEY=sk-... python3 cli.py`.

## Known upstream caveat — test against your real endpoint first

`langchain-litellm`'s `ChatLiteLLM` has open upstream issues specifically in
the `bind_tools()` + streaming + `usage_metadata` combination this project
relies on:

- [langchain-litellm#51](https://github.com/langchain-ai/langchain-litellm/issues/51) — `bind_tools().astream()` mis-routes to OpenAI's `vector_stores` API for Bedrock-backed models.
- [langchain-litellm#52](https://github.com/langchain-ai/langchain-litellm/issues/52) — Anthropic prompt-cache token fields get stripped from `usage_metadata` during streaming (present on non-streaming calls).

Both are concentrated on Bedrock routing and Anthropic prompt-caching
fields — a plain OpenAI-compatible custom endpoint is likely unaffected,
but this needs verifying against whatever real endpoint you point it at.
This is deliberately **not** coded around here; treat it as a "test it
yourself once" item, not a known bug in this project's code.

### `AIMessageChunk.content` as a list of blocks

This one *is* coded around, in `ui/streaming.py`. `ChatOllama` chunks always
give `.content` as a plain `str`. Some litellm-routed backends (observed
with an Anthropic-style API behind `ChatLiteLLM`) instead give `.content` as
a list of content blocks, e.g. `[{"type": "text", "text": "..."}]`,
sometimes mixed with non-text blocks (`tool_use`, etc.). Before this was
handled, `stream_response()` assigned that list straight into its
`accumulated` string and `RichMarkdown(accumulated)` raised
`TypeError: Input data should be a string, not <class 'list'>` on the very
first chunk.

`_extract_text(content)` flattens either shape (`str`, or `list` of
`str`/`{"type": "text", "text": ...}` dicts, ignoring other block types)
into plain text; `stream_response()` calls it on every chunk instead of
using `chunk.content` directly. Covered by `test_streaming.py`, no live
endpoint required.

## `usage_metadata` and the toolbar's `Ctx: xx k` counter

`ui/streaming.py`'s `_update_ctx()` already tolerates a response with no
`usage_metadata`:

```python
def _update_ctx(response):
    usage = getattr(response, "usage_metadata", None)
    if usage:
        _ctx_state["tokens"] = usage["input_tokens"]
```

If a cloud backend doesn't populate `usage_metadata` on streamed chunks
(see the caveat above), the toolbar's `Ctx: xx k` counter simply stays at
its last known value instead of updating — no crash, no code change needed.

## Context window lookup and the toolbar's `/ yy k` suffix

`core/agent.py`'s `get_context_window(model)` appends a `/ yy k` suffix to
the toolbar's `Ctx: xx k` line, showing the loaded model's context window
(max tokens) alongside current usage. It uses the official `ollama` PyPI
package (`ollama.show(model)` → Ollama's `/api/show`), **not**
`langchain_ollama`/`ChatOllama` — `ChatOllama` doesn't expose this
metadata through its own interface; only the raw `/api/show` response
carries it, via a `modelinfo` dict (aliased from the JSON `model_info`
field). Both target the same Ollama host (`OLLAMA_HOST` env var or
localhost default) that `ChatOllama` implicitly uses, but they're separate
packages and separate calls.

`modelinfo` has no fixed field name for the context window — Ollama emits a
family-prefixed key instead (`"llama.context_length"`,
`"gemma3.context_length"`, etc., depending on the model's architecture), so
`get_context_window()` scans for the first key ending in `.context_length`
rather than indexing a known key:

```python
modelinfo = info.modelinfo or {}
for key, value in modelinfo.items():
    if key.endswith(".context_length"):
        return int(value)
```

This is **local-Ollama-only** — cloud/self-hosted models loaded via
`--base-url` have no equivalent (`load_llm()`'s litellm branch isn't an
Ollama server), so `ui/repl.py`'s `main()` only calls
`get_context_window()` when `base_url` is falsy. It's fetched exactly
**once** per process, right before the interactive `PromptSession` is
created — not per turn, not per toolbar re-render (the toolbar callback
only reads the cached result) — and not at all in one-shot `-p` mode, since
no toolbar exists there to show it. Any failure (Ollama unreachable,
unknown model tag, unexpected response shape) makes the function return
`None` rather than raise; the toolbar then just omits the suffix.

Covered by `test_model_config.py` with a faked `ollama` module (no live
Ollama dependency) for the key-matching and error-handling logic. **Not**
covered automatically: whether Ollama's real `/api/show` response for a
model you actually run still follows the `<family>.context_length` key
convention assumed here — verify manually against a live Ollama instance
(`ollama show <model>` or `python3 -c "import ollama;
print(ollama.show('<model>').modelinfo)"`) before relying on the toolbar
number, similar in spirit to the litellm caveat above.

## Testing

`test_model_config.py` covers all of the flag/env-var resolution logic and
`load_llm()`'s branching with no Ollama or network dependency — see
[`docs/manual-testing.md`](manual-testing.md).

An actual end-to-end round trip against a real cloud endpoint
(`--base-url`/`--model`/`--api-key`, confirming both config resolution and
streaming work against a real provider) has been manually verified
end-to-end (2026-08-13) — this still requires credentials only you have, so
it isn't something this repo's automated tests can cover on their own:

```bash
python3 cli.py --base-url <real-url> --model openai/<real-model> --api-key <key> -p "What is 2 + 2?"
```

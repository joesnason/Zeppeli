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

## Vision / image input

`core/images.py` builds `HumanMessage` content from an image list — see
[`docs/tools.md`](tools.md#images-are-not-a-tool) for how it's invoked
(`@path` mention, `/image` command, `--image` flag). The interesting part
for this doc is that **one content-block format works against both
backends unchanged**:

```python
{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
```

- `ChatOllama` (`langchain_ollama/chat_models.py`'s
  `_convert_messages_to_ollama_messages()`) recognizes this exact shape,
  strips the `data:...;base64,` prefix itself, and puts the raw base64 into
  Ollama's own `images` field.
- `ChatLiteLLM` (`langchain_litellm`'s `_convert_message_to_dict()`) doesn't
  recognize `"image_url"` as one of `langchain_core`'s newer typed data
  content blocks, so it falls through the "pass through standard text
  blocks or other unrecognized dict formats unchanged" branch — the block
  reaches litellm, and therefore the `hosted_vllm/` endpoint, byte-for-byte
  as constructed. Verified directly against the installed package (not just
  read from source) — see `test_images.py`'s
  `test_litellm_message_conversion_passes_list_content` — and end-to-end
  against a mock OpenAI-compatible server capturing the actual HTTP request
  body.

So attaching an image to a `hosted_vllm/qwen3.6-27b-awq-int4` run needs
**zero** extra config beyond what "The `openai/` prefix convention" above
already covers — `model` is passed through as-is, so
`--model hosted_vllm/qwen3.6-27b-awq-int4 --base-url http://host:8000/v1`
already routes correctly; the only new thing is attaching the image.

`build_message_content()` returns a plain `str` when no images are
attached — never a single-element list. This matters because
`ChatOllama`'s list-content branch prepends `\n` to every text part it
sees; always returning a list would silently add a leading newline to
every existing text-only prompt.

### Downscaling and size caps

Before encoding, `core/images._downscale()` (lazy `PIL` import) resizes to
a `1568px` long edge (never upscales) and re-encodes as PNG (if the source
has alpha) or JPEG, trying progressively lower quality/size until the
base64 payload fits under `MAX_ENCODED_BYTES` (4 MB). `MAX_SOURCE_BYTES`
(20 MB) rejects an oversize original before it's even opened.
`MAX_IMAGES` (4 per message) × `MAX_ENCODED_BYTES` means a single request
can carry up to ~16 MB of image data — fine for vLLM itself, but if there's
a reverse proxy (e.g. nginx) in front of the endpoint, check its
`client_max_body_size` — the common `1m` default will reject that request
with an opaque error before it reaches vLLM at all.

If Pillow isn't installed, `_downscale()` falls back to sending the raw
file unmodified as long as it's under 2 MB, and raises a clear
`ImageError` (asking for `pip3 install Pillow`) for anything bigger — it
doesn't hard-fail the whole feature.

### What happens with a text-only model

If the loaded model doesn't understand `image_url` content (a non-vision
OpenAI-compatible endpoint, or a text-only Ollama tag), the provider
returns an error, which surfaces through the same path as every other
model-call failure — `ui/streaming.py`'s `stream_response()` catches it
and calls `_format_model_error()`. A branch there matches image-rejection
wording (`"image"` plus `"not support"`/`"unsupported"`/`"invalid"`/
`"no vision"`) and returns a one-line hint suggesting a vision model
instead of dumping the raw provider error.

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

## Turn-level context compaction (25-turn sliding window)

The in-memory `messages` list (`ui/repl.py`'s `main()`) grows for the whole
life of the process — every turn appends onto it, nothing ever trims it.
Left unchecked, a long-running conversation eventually risks exceeding the
model's context window. `core/messages.py`'s `compact_messages()` addresses
this by building a **turn-windowed view** of the conversation for the model,
without ever touching the canonical list itself.

A **turn** is one `HumanMessage` plus everything that follows it
(`AIMessage`/`ToolMessage` hops from `ui/turn.py`'s `run_turn()`) up to but
not including the next `HumanMessage`. Once the conversation exceeds
`_MAX_TURNS` (25) turns, `compact_messages()` returns only:

- the first turn (`_KEEP_FIRST_TURNS = 1`) — the user's original intent for
  the conversation, and
- the latest 24 turns (`_KEEP_LAST_TURNS = 24`)

— 25 turns' worth of messages total. Any leading preamble (everything before
the first `HumanMessage` — in practice just the always-present
`SystemMessage`) is kept in full and doesn't count toward the 25. At or
below the threshold, the conversation is returned unchanged.

**Why the unit is a whole turn, not a raw message count**: a hop's
`AIMessage(tool_calls=[...])` is always immediately followed by one or more
matching `ToolMessage`s (paired by `tool_call_id`). A naive "keep the last N
raw messages" slice could land in the middle of that pairing — dropping the
`AIMessage` while keeping an orphaned `ToolMessage`, or vice versa.
LangChain's Ollama/litellm message converters don't validate this
client-side, but OpenAI-compatible cloud APIs (reached via litellm) reject a
malformed sequence server-side. Because a turn is always a complete,
self-contained unit, grouping by turn makes a split pairing structurally
impossible — a turn is kept or dropped as a whole.

Compaction is **silent**: no marker or note is inserted telling the model
turns were omitted (unlike `truncate_tool_output()`'s `[truncated N
lines/chars]` notes — see above). It's also independent of and layered
*above* that per-tool-output cap: `truncate_tool_output()` still runs inside
whichever turns are kept, at the single-tool-result level; `compact_
messages()` operates one layer up, at the whole-message-list level.

Wired in at the single choke point every model call goes through:
`ui/streaming.py`'s `stream_response()` calls `compact_messages(messages)`
once per hop, immediately before both of its `.stream()` call sites (the
main attempt and the reasoning-fallback retry), and passes the result —
never `messages` itself — to the model. See
[`docs/sessions.md`](sessions.md#message--history-conversion) for why this
is safe with respect to session/event-log persistence, which reads the
canonical, uncompacted `messages` list exclusively.

Covered by `test_compaction.py`: turn-counting/grouping correctness at and
above the threshold, preamble handling, a multi-hop tool-call turn
surviving intact when kept vs. dropped as a whole unit when not, non-
mutation of the input list, and an integration test on `stream_response()`
confirming the model receives the compacted view while the caller's own
`messages` list is untouched.

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

The vision path additionally needs a round trip that actually sends image
bytes — the mock-server check above (`test_images.py`) proves the request
is well-formed, but not that a real vLLM/qwen backend accepts and reads
it:

```bash
python3 cli.py --base-url http://<vllm-host>:8000/v1 \
  --model hosted_vllm/qwen3.6-27b-awq-int4 \
  -p "這張圖裡有什麼？" --image ./shot.png
```

"""Model configuration and loading — the AI agent's brain, no UI dependencies."""

from langchain_ollama import ChatOllama
from .tools import TOOLS

MODEL = "gemma4:26b-nvfp4"  # for Mac Mini 64G
# MODEL = "gemma4:e2b"  # for MacBook Air 8G

SYSTEM_PROMPT = """You are a helpful assistant with access to the following tools:

- list_files(path): List files and directories at a given path using ls -la.
- glob_files(pattern, cwd): Find files matching a glob pattern (supports ** for recursive search). Default cwd is ".".
- rg_search(pattern, path, glob): Search file contents using ripgrep (regex supported). Use glob to filter by filename (e.g. "*.py"). Default path is ".".
- read_file(path, offset, limit, max_lines, max_bytes): Read a file in chunks of up to 400 lines starting at line offset. Stops when max_lines (default 10000) or max_bytes (default 98304 = 96KB) is reached. Use offset from the returned hint to paginate through large files.
- write_file(path, content): Write content to a file, creating it if it doesn't exist or replacing all its content.
- delete_file(path): Delete a file. Does not delete directories.

Use these tools when the user asks about files, directories, folder contents, searching within files, or writing/creating/deleting files.
For questions unrelated to the filesystem, answer directly without using any tool.

Always base your response strictly on the actual tool result. If a tool result says the action was cancelled, denied, or failed (e.g. contains "CANCELLED" or "Error"), you must tell the user it did NOT happen — never claim a file was created, modified, or deleted unless the tool result confirms it succeeded."""


def load_llm(model: str | None = None, base_url: str | None = None, api_key: str | None = None):
    """Load a chat model and bind it to the full tool set.

    Local Ollama is the default. If base_url is given, routes to a cloud/
    self-hosted model via litellm instead — model is required in that case
    (validated upstream in cli.py, not here). Flag/env-var resolution for
    all three params also happens upstream in cli.py; this stays a pure
    function of its three params.
    """
    if base_url:
        from langchain_litellm import ChatLiteLLM  # lazy: keep litellm optional

        kwargs = {"model": model, "api_base": base_url}
        if api_key:
            kwargs["api_key"] = api_key
        return ChatLiteLLM(**kwargs).bind_tools(TOOLS)
    return ChatOllama(model=model or MODEL).bind_tools(TOOLS)


def get_context_window(model: str | None = None) -> int | None:
    """Look up a local-Ollama model's context window (max tokens) via
    ollama.show(). Returns None on any failure (Ollama unreachable, unknown
    model, unexpected response shape) — never raises. Not meaningful for
    cloud/litellm models; callers should only invoke this when base_url is
    not set.
    """
    import ollama  # lazy: mirrors load_llm()'s own litellm lazy-import

    try:
        info = ollama.show(model or MODEL)
    except Exception:
        return None

    modelinfo = info.modelinfo or {}
    for key, value in modelinfo.items():
        if key.endswith(".context_length"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None

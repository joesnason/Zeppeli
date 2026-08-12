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
For questions unrelated to the filesystem, answer directly without using any tool."""


def load_llm(model: str = MODEL):
    """Load the Ollama chat model and bind it to the full tool set."""
    return ChatOllama(model=model).bind_tools(TOOLS)

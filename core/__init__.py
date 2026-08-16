"""Core layer: AI agent/model logic — tool definitions, path resolution, Ollama loading.

No UI dependencies (no Rich, no prompt_toolkit). See ui/ for the interactive layer.
"""

from .agent import MODEL, SYSTEM_PROMPT, load_llm, get_context_window
from .tools import (
    TOOLS,
    TOOLS_BY_NAME,
    PATH_ARGS,
    RG_BIN,
    resolve_paths,
    list_files,
    glob_files,
    rg_search,
    read_file,
    write_file,
    delete_file,
)

__all__ = [
    "MODEL",
    "SYSTEM_PROMPT",
    "load_llm",
    "get_context_window",
    "TOOLS",
    "TOOLS_BY_NAME",
    "PATH_ARGS",
    "RG_BIN",
    "resolve_paths",
    "list_files",
    "glob_files",
    "rg_search",
    "read_file",
    "write_file",
    "delete_file",
]

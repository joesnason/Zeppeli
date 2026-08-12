"""UI layer: everything that talks to the user — REPL, Rich rendering, prompt_toolkit input,
permission prompts. Imports from core/ for AI agent/model functionality; core/ never imports
back from here.
"""

from .repl import main

__all__ = ["main"]

"""UI layer: everything that talks to the user — REPL, Rich rendering, prompt_toolkit input,
permission prompts. Imports from core/ for AI agent/model functionality; core/ never imports
back from here.
"""

from .repl import main
from .permissions import MODE_APPROVAL, MODE_AUTO, MODE_YOLO

__all__ = ["main", "MODE_APPROVAL", "MODE_YOLO", "MODE_AUTO"]

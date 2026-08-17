"""Tab-completion for `@path` mentions in the REPL input line."""

import os
import re

from prompt_toolkit.completion import Completer, PathCompleter
from prompt_toolkit.document import Document

from core.images import is_image_path

# Matches an in-progress @-mention ending at the cursor, so Tab can offer
# completions for it. Mirrors core.images._MENTION_RE's "must be preceded by
# nothing/whitespace/(/[" rule so it doesn't fire mid-email-address, but
# doesn't require a trailing word char since it's matched at the cursor.
_MENTION_AT_CURSOR_RE = re.compile(r"(?<![^\s(\[])@((?:\\.|[^\s])*)$")


class AtPathCompleter(Completer):
    """Complete a local path after an `@` token. Offers directories (so you
    can descend into them) and image files only — anything else would just
    be noise for this feature."""

    def __init__(self):
        self._paths = PathCompleter(
            expanduser=True,
            file_filter=lambda p: os.path.isdir(p) or is_image_path(p),
        )

    def get_completions(self, document: Document, complete_event):
        m = _MENTION_AT_CURSOR_RE.search(document.text_before_cursor)
        if not m:
            return
        word = m.group(1)
        # PathCompleter's start_position is relative to its own Document's
        # cursor, so completing against a sub-Document containing just the
        # path fragment composes correctly without offset arithmetic.
        sub_document = Document(text=word, cursor_position=len(word))
        yield from self._paths.get_completions(sub_document, complete_event)

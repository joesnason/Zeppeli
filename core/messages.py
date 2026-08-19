"""Shared message-content normalization — no UI deps.

Used by ui/streaming.py (Markdown rendering) and core/sessions.py (history
persistence), both of which need to flatten a LangChain message's `.content`
into plain text before doing anything else with it.
"""


def extract_text(content) -> str:
    """Normalize an AIMessage/HumanMessage `.content` into plain text.

    ChatOllama gives a plain str. Some litellm-routed cloud/self-hosted
    backends (e.g. Anthropic-style APIs) instead give a list of str/dict
    content blocks (e.g. [{"type": "text", "text": "..."}], possibly mixed
    with non-text blocks like tool_use or image_url) — those must be
    flattened to text before Markdown rendering or persistence."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""

"""Loads this project's LLM prompts from the ``.md`` files in this folder.

Every prompt this project sends to an LLM lives here, in plain Markdown, kept
separate from the Python code that composes and sends it — so a prompt can be
read, reviewed, or changed without reading code, and every call site pulls
from exactly one place.

Each file holds exactly one prompt: an ``# H1`` title (documentation only,
never sent to the LLM) followed by a blank line, then the prompt text
verbatim. A file composed from more than one piece (e.g. a chat system
prompt built from an intro plus shared scope rules) stays composed in
Python — this loader only returns one named block at a time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Return the prompt text in ``<name>.md`` (no ``.md`` in ``name``).

    Strips the leading ``# Title`` line and the blank line after it; every
    line after that is returned exactly as written, including its own blank
    lines, with only a single trailing newline removed.
    """
    path = _DIR / f"{name}.md"
    lines = path.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
        if lines and not lines[0].strip():
            lines = lines[1:]
    return "\n".join(lines).rstrip("\n")

"""Console output safety — keep memo's output printable wherever it lands.

Why this exists: memo's primary *consumer* is an agentic IDE that captures our
stdout through a pipe, and Windows is the primary platform. When stdout is a pipe
rather than a real console, CPython picks the locale encoding (cp1252 on a default
Windows install) instead of the console's UTF-8. The glyphs memo prints then blow
up the whole command:

    $ python -m memo.cli show | cat
    UnicodeEncodeError: 'charmap' codec can't encode character '\\u2713'

That is a hard failure of the exact path the tool is built for, and it was
previously worked around by asking every caller to set PYTHONIOENCODING=utf-8.
`init_streams()` fixes it in-process instead, once, for every command.

Two layers of defence:
  1. Re-encode stdout/stderr as UTF-8 with errors="replace" (Python 3.7+).
  2. If a stream can't be reconfigured, downgrade SYM to ASCII equivalents so the
     text still reads correctly instead of raising.

Only *ephemeral* CLI chrome uses SYM. Persisted content (the summary text built in
summarizer.py, the GUIDE constant, generated rule files) keeps its real Unicode —
those are written to disk as explicit UTF-8 and must not vary by terminal.
"""

from __future__ import annotations

import sys

# The glyphs memo prints. Every one of these is absent from cp1252, which is why
# they need a fallback (the em dash, by contrast, exists at cp1252 0x97 and is safe).
GLYPH: dict[str, str] = {
    "ok": "✓",      # ✓ cached / healthy
    "warn": "⚠",    # ⚠ ambiguous match, risk flag
    "fail": "!",         # already ASCII; kept here so callers have one lookup
    "arrow": "→",   # → token deltas, output paths
}

ASCII: dict[str, str] = {
    "ok": "+",
    "warn": "!",
    "fail": "!",
    "arrow": "->",
}

# Mutated in place by init_streams() when the stream can't represent GLYPH, so
# callers can `from .console import SYM` at import time and still get the right set.
SYM: dict[str, str] = dict(GLYPH)


def _force_utf8(stream) -> None:
    """Best-effort: re-encode `stream` as UTF-8, replacing anything unmappable.

    errors="replace" matters as much as the encoding — it guarantees no output path
    can raise, even for text we didn't anticipate (a non-ASCII identifier in someone's
    source file, say, echoed back in a digest).
    """
    enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    if enc in ("utf8", "utf8sig"):
        return  # already fine; don't disturb a working stream
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass  # not a reconfigurable TextIOWrapper (redirected in-process, etc.)


def _can_encode(stream) -> bool:
    """True if `stream` can represent every glyph in GLYPH."""
    enc = getattr(stream, "encoding", None)
    if not enc:
        return True  # unknown encoding: assume OK, errors="replace" is the net
    try:
        "".join(GLYPH.values()).encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def init_streams() -> None:
    """Make stdout/stderr safe for memo's output. Call once, early, from main()."""
    for stream in (sys.stdout, sys.stderr):
        _force_utf8(stream)
    # Both streams must cope, since diagnostics go to stderr and data to stdout.
    if not (_can_encode(sys.stdout) and _can_encode(sys.stderr)):
        SYM.update(ASCII)

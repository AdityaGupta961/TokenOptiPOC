"""Token counting via tiktoken (cl100k_base), with a graceful fallback.

tiktoken has native (Rust) wheels that may not exist for every Python version yet.
If tiktoken can't be imported/loaded, we fall back to a rough char-based estimate so
`memo show` still works — the estimate is clearly labeled as approximate.
"""

from __future__ import annotations

from functools import lru_cache

_ENCODING_NAME = "cl100k_base"


@lru_cache(maxsize=1)
def _encoder():
    """Return a tiktoken encoder, or None if tiktoken is unavailable."""
    try:
        import tiktoken

        return tiktoken.get_encoding(_ENCODING_NAME)
    except Exception:
        return None


def count_tokens(text: str) -> tuple[int, bool]:
    """Return (token_count, exact).

    `exact` is True when tiktoken produced the count, False when we fell back to
    the ~chars/4 heuristic.
    """
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=())), True
    # Fallback heuristic: ~4 characters per token is a reasonable rough proxy.
    return max(1, round(len(text) / 4)), False


def encoding_available() -> bool:
    return _encoder() is not None

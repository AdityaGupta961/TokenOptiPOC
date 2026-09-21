"""memo — a local, offline file-summary cache for AI coding assistants.

memo generates compact *structural* summaries of source files using local static
analysis (Python AST + language-aware heuristic parsers) and caches them keyed by
a SHA-256 of the file contents. Paste the summary into an AI coding assistant
instead of the full file to save tokens during discovery/planning conversations.

No LLM API is used or required.
"""

# Bump this whenever an analyzer's OUTPUT changes, not just when the CLI does. Cache
# entries record the version that produced them and are reused when it matches, so an
# analyzer improvement without a bump is silently never applied to already-indexed files.
__version__ = "0.4.0"

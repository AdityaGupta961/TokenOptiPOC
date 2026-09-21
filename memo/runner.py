"""`memo run` — execute a real command, compress its stdout for an agent, keep the
original recoverable via `memo recall`.

This is Layer 1: unlike the rest of memo (which indexes source files), this module
never touches anything memo already knows about — it wraps whatever command an agent
(or its PATH-shimmed alias, see `shim.py`) invokes and only ever compresses *fresh*
output as it's produced. It never rewrites history, so there's nothing here that can
invalidate an upstream prompt cache the way retroactively editing a transcript would.

Three invariants, matched to what independent tools in this space converged on
separately (see `.memo/adrs.json` if `memo adr` has an entry for this):

1. A human at a real terminal (`sys.stdout.isatty()`) always gets the tool's own
   output, byte-for-byte, streamed live — compression only applies when stdout is
   being piped, i.e. an agent's tool-call harness is the consumer.
2. never_worse: compressed output only ships if it's actually smaller once the
   recall trailer itself is counted. Otherwise the original ships unchanged.
3. Nothing is ever silently discarded. Whenever compression does ship, the original
   is stored, content-addressed, under `.memo/recall/<sha256>` and named in a trailer
   line so it's one `memo recall <hash>` away.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cache import sha256_of_bytes
from .filters import compress, normalize_tool_name
from .tokens import count_tokens

TRAILER = "\n[full output: memo recall {hash}]"
_PLACEHOLDER_HASH = "0" * 64  # sha256 hexdigest length, for pre-store token counting
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

RECALL_SUBDIR = "recall"
LEDGER_FILENAME = "ledger.jsonl"


def _decode_bytes(data: bytes) -> str:
    """Best-effort decode of a subprocess's raw output, tolerating odd encodings —
    mirrors cli.py's `_read_text` fallback chain for files."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _resolve_executable(cmd: str) -> str:
    """Resolve `cmd` to a real executable path, refusing to resolve back into memo's
    own console script — a shim pointing `memo run` at itself would recurse forever."""
    resolved = shutil.which(cmd)
    if resolved is None:
        raise FileNotFoundError(f"'{cmd}' not found on PATH")
    memo_self = shutil.which("memo")
    if memo_self is not None:
        try:
            if Path(resolved).resolve() == Path(memo_self).resolve():
                raise ValueError(
                    f"refusing to run '{cmd}': it resolves to memo's own executable — "
                    "a shim pointing memo run back at itself would recurse forever."
                )
        except OSError:
            pass  # can't stat one of them; don't block on a best-effort safety check
    return resolved


def _store_recall(cache_root: Path, data: bytes) -> str:
    recall_dir = cache_root / RECALL_SUBDIR
    recall_dir.mkdir(parents=True, exist_ok=True)
    h = sha256_of_bytes(data)
    blob = recall_dir / h
    if not blob.exists():
        blob.write_bytes(data)
    return h


def recall(cache_root: Path, hash_: str) -> str:
    """Return the stored original behind a compression trailer's hash.

    Raises KeyError if the hash is malformed or nothing is stored for it (never
    stored, or the `.memo/recall/` directory was cleared).
    """
    if not _HASH_RE.match(hash_):
        raise KeyError(hash_)
    blob = cache_root / RECALL_SUBDIR / hash_
    if not blob.is_file():
        raise KeyError(hash_)
    return _decode_bytes(blob.read_bytes())


def _append_ledger(cache_root: Path, record: dict) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    ledger_path = cache_root / LEDGER_FILENAME
    with ledger_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def run_command(argv: list[str], cache_root: Path) -> int:
    """Run `argv` as a real command, returning its exit code unchanged.

    When stdout isn't a terminal (the agent-harness case), the tool's stdout is run
    through a per-tool compressor (`filters.compress`), guarded by never_worse, with
    the original recoverable via `memo recall` and a record appended to
    `.memo/ledger.jsonl`. stderr is always passed through unmodified — only stdout is
    a compression target in v1, since that's where verbose tool noise (test output,
    diffs, lint results) lives.
    """
    if not argv:
        raise ValueError("no command given")

    tool = normalize_tool_name(argv)
    exe = _resolve_executable(argv[0])
    full_argv = [exe, *argv[1:]]

    if sys.stdout.isatty():
        # A human is watching: don't intercept anything, preserve real-time
        # streaming and coloring exactly as the tool would produce on its own.
        proc = subprocess.run(full_argv)
        return proc.returncode

    proc = subprocess.run(full_argv, capture_output=True)
    stdout_text = _decode_bytes(proc.stdout)
    stderr_text = _decode_bytes(proc.stderr)

    tokens_before, before_exact = count_tokens(stdout_text)
    compressed = compress(tool, stdout_text, cache_root)

    final_text = stdout_text
    final_hash: str | None = None
    if compressed != stdout_text:
        # Compare against a placeholder trailer first (fixed-length hash, so the
        # token count doesn't depend on the real hash) — only pay for the recall
        # store's disk write if compression is actually going to ship.
        candidate_tokens, _ = count_tokens(compressed + TRAILER.format(hash=_PLACEHOLDER_HASH))
        if candidate_tokens < tokens_before:
            final_hash = _store_recall(cache_root, proc.stdout)
            final_text = compressed + TRAILER.format(hash=final_hash)
        # else: never_worse — final_text stays the untouched original.

    sys.stdout.write(final_text)
    sys.stderr.write(stderr_text)

    final_tokens, final_exact = count_tokens(final_text)
    _append_ledger(cache_root, {
        "cmd": " ".join(argv),
        "ts": datetime.now(timezone.utc).isoformat(),
        "tokens_before": tokens_before,
        "tokens_after": final_tokens,
        "exact": bool(before_exact and final_exact),
        "hash": final_hash,
        "exit_code": proc.returncode,
    })

    return proc.returncode

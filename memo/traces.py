"""Runtime trace ingestion — `codebase-memory-mcp`'s `ingest_traces`, in memo's
offline, flat-JSON form.

memo's call graph is 100% static (README: "Honest limits") and is blind to reflection,
dependency injection and dynamic dispatch by construction — a name-based scan of source
text cannot see what only happens at runtime. Feeding it real (caller, callee) pairs
observed while the program actually ran closes exactly that gap, without a model and
without a live agent watching: `memo ingest-traces` merges them into `.memo/runtime_edges.json`,
and `graphindex.merge_runtime_edges` folds them into every graph load as TIER_VERIFIED
edges — outranking even a declared-type match, because they aren't inferred.

Input format is a tool-agnostic JSONL stream, one observed call per line:

    {"caller": "OrderService.Process", "callee": "PaymentGateway.Charge"}
    {"caller": "handlers.on_message", "callee": "queue.publish"}

`caller`/`callee` may be bare names or `Container.method` — matched the same way
`memo callers`/`memo calls` already match names, so nothing new to learn. Produce this
from application logs, an APM export, or `record()` below for Python.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .graphindex import RUNTIME_FILE


def parse_jsonl(text: str) -> list[dict]:
    """Parse the ingest format, skipping blank lines, comments and malformed rows.

    Tolerant on purpose: a trace file is produced by pasting together log lines or an
    APM export, not hand-written, so one bad line must not sink the whole ingest.
    """
    out: list[dict] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("caller") and rec.get("callee"):
            row = {"caller": str(rec["caller"]), "callee": str(rec["callee"])}
            if isinstance(rec.get("count"), int) and rec["count"] > 1:
                row["count"] = rec["count"]
            out.append(row)
    return out


def ingest(cache_root: Path, records: list[dict]) -> dict:
    """Merge `records` into the persisted runtime-edge store (append + count, not
    replace) so repeated ingests from different runs accumulate evidence instead of
    each other overwriting the last."""
    f = cache_root / RUNTIME_FILE
    existing: dict[tuple, dict] = {}
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for e in data.get("edges", []):
                existing[(e["caller"], e["callee"])] = e
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    added = updated = 0
    for r in records:
        key = (r["caller"], r["callee"])
        n = r.get("count", 1)
        if key in existing:
            existing[key]["count"] = existing[key].get("count", 1) + n
            updated += 1
        else:
            existing[key] = {"caller": r["caller"], "callee": r["callee"], "count": n}
            added += 1

    cache_root.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps({"edges": list(existing.values())}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return {"added": added, "updated": updated, "total": len(existing), "file": str(f)}


def clear(cache_root: Path) -> bool:
    f = cache_root / RUNTIME_FILE
    if f.is_file():
        f.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# Optional Python recorder: produces the same JSONL format with no extra dependency,
# for the common case of "run the test suite / a repro script, then ingest what
# actually fired." Not required — any producer of the JSONL format works.
# --------------------------------------------------------------------------- #

class record:
    """Context manager: capture real Python call pairs during `with record(path):`.

    Uses `sys.setprofile` rather than `sys.settrace` — profile hooks fire once per
    call/return, not once per line, so the overhead is proportional to call count, not
    to lines executed. Frames from stdlib/site-packages are skipped: recording *into*
    a framework is rarely the missing edge (that's the DI/reflection call the
    framework makes back into *your* code, which the return path here still catches
    once your code is on the stack) and would otherwise dominate the trace.

    Writes one JSON line per (caller, callee) pair the first time it's seen; a repeat
    call increments `count` in memory and is written once at exit, so a hot loop
    doesn't produce a million duplicate lines.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._counts: dict[tuple[str, str], int] = {}
        self._old_profile = None

    @staticmethod
    def _skip(frame) -> bool:
        fn = frame.f_code.co_filename
        return ("site-packages" in fn or f"{__package__}" in fn.replace("\\", "/")
                or fn.startswith("<"))

    def _on_call(self, frame, event, arg):
        if event != "call":
            return
        callee_frame = frame
        caller_frame = frame.f_back
        if caller_frame is None or self._skip(callee_frame) or self._skip(caller_frame):
            return
        caller = self._qualname(caller_frame)
        callee = self._qualname(callee_frame)
        if caller and callee:
            key = (caller, callee)
            self._counts[key] = self._counts.get(key, 0) + 1

    @staticmethod
    def _qualname(frame) -> str:
        code = frame.f_code
        self_obj = frame.f_locals.get("self") or frame.f_locals.get("cls")
        cls_name = type(self_obj).__name__ if "self" in frame.f_locals else (
            self_obj.__name__ if "cls" in frame.f_locals and isinstance(self_obj, type) else "")
        return f"{cls_name}.{code.co_name}" if cls_name else code.co_name

    def __enter__(self) -> "record":
        self._old_profile = sys.getprofile()
        sys.setprofile(self._on_call)
        return self

    def __exit__(self, *exc) -> None:
        sys.setprofile(self._old_profile)
        lines = [json.dumps({"caller": c, "callee": e, "count": n})
                 for (c, e), n in sorted(self._counts.items())]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

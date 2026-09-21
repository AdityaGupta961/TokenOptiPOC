"""The cache store: content hashing + flat-JSON persistence.

Layout (under the project's `.memo/` home):

    .memo/
        index.json                 # { "<abs path>": {"hash": ..., "language": ...} }
        cache/<sha256>.json        # one file per unique content hash

Keying by content hash means identical files share an entry and re-caching an
unchanged file is a no-op. The index lets `memo get <path>` resolve quickly.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import CACHE_SUBDIR, INDEX_FILENAME, find_cache_root


@dataclass
class CacheEntry:
    path: str  # absolute, normalized
    content_hash: str
    language: str
    summary_text: str
    structured: dict
    raw_tokens: int
    summary_tokens: int
    tokens_exact: bool
    created_at: str = ""
    # Which memo produced this entry. Content hashing catches *file* changes, but not
    # changes to the analyzers themselves: after an analyzer fix, an untouched file's
    # cached symbols (and their line numbers) can be wrong while still looking fresh.
    # `memo status` compares this against the running version so that is visible
    # instead of silently believed. Defaults empty so pre-0.2.0 caches still load.
    memo_version: str = ""

    def to_json(self) -> dict:
        return asdict(self)


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_path(path: Path) -> str:
    """Canonical absolute key for a file path."""
    return str(path.expanduser().resolve())


MANIFEST_FILENAME = "manifest.json"


class Cache:
    def __init__(self, root: Path | None = None):
        self.root = root or find_cache_root()
        self.cache_dir = self.root / CACHE_SUBDIR
        self.index_path = self.root / INDEX_FILENAME
        self.manifest_path = self.root / MANIFEST_FILENAME
        # When non-None, store() buffers index updates here instead of rewriting
        # index.json per file (see `batch`).
        self._buffer: dict | None = None

    # --- setup -----------------------------------------------------------
    def ensure(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # --- batching --------------------------------------------------------
    @contextmanager
    def batch(self):
        """Buffer index writes for the duration, flushing once on exit.

        Outside a batch, `store()` reads *and* rewrites the whole index.json for every
        single file — O(N) work per file, so O(N^2) across a full index run. Wrapping a
        batch collapses that to one read and one write.
        """
        # Any batch may rewrite entry *contents* while leaving the index identical
        # (re-analyzing unchanged files after an analyzer change does exactly that), so
        # the existing manifest cannot be assumed to describe the new entries.
        self._drop_manifest()
        self._buffer = self._load_index()
        try:
            yield self
        finally:
            buffered, self._buffer = self._buffer, None
            if buffered is not None:
                self._save_index(buffered)

    # --- index -----------------------------------------------------------
    def _load_index(self) -> dict:
        if self.index_path.is_file():
            try:
                return json.loads(self.index_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_index(self, index: dict) -> None:
        # sort_keys is load-bearing, not cosmetic. list_entries() iterates this dict in
        # insertion order, and every consumer then re-sorts it by relevance with keys
        # that tie constantly (is_test, hit count, confidence tier). Python's sort is
        # stable, so whatever order arrives decides how those ties come out. Sorted keys
        # here are what make identical repos answer a query in an identical row order —
        # which is what lets a prompt cache serve a repeated answer, and what keeps
        # golden-file cost benchmarks free of phantom diffs.
        #
        # Without it, `cache <file>` appending one path would reshuffle ties across the
        # whole result set. Locked by tests/test_determinism.py.
        self.ensure()
        self.index_path.write_text(
            json.dumps(index, indent=2, sort_keys=True), encoding="utf-8"
        )

    # --- entries ---------------------------------------------------------
    def _entry_file(self, content_hash: str) -> Path:
        return self.cache_dir / f"{content_hash}.json"

    def get_by_path(self, path: Path) -> CacheEntry | None:
        """Return the cached entry for a path if present (regardless of freshness)."""
        key = normalize_path(path)
        index = self._load_index()
        rec = index.get(key)
        if not rec:
            return None
        return self._read_entry(rec["hash"])

    def _read_entry(self, content_hash: str) -> CacheEntry | None:
        f = self._entry_file(content_hash)
        if not f.is_file():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return CacheEntry(**data)
        except (json.JSONDecodeError, OSError, TypeError):
            return None

    def gc(self) -> int:
        """Delete cache entry files no longer referenced by the index.

        Entries are stored per content hash, so dropping index paths — or re-indexing
        after files changed — leaves the old per-hash files on disk. They are invisible
        to every command yet occupy the bulk of `.memo/`: on one repo, 9,665 orphans
        totalling 78 MB against 541 live entries. Run after the index is committed.
        """
        if not self.cache_dir.is_dir():
            return 0
        referenced = {rec["hash"] for rec in self._load_index().values()}
        removed = 0
        for f in self.cache_dir.glob("*.json"):
            if f.stem not in referenced:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass  # a locked file only costs space, never correctness
        return removed

    def prune(self, under: str, keep: set[str]) -> list[str]:
        """Drop buffered index entries under `under` that aren't in `keep`.

        Without this, tightening a `.memoignore` had no effect: a batch seeds its buffer
        from the existing index and only ever adds, so files that are no longer walked
        stayed indexed forever. They then can't be spotted — they still exist on disk, so
        they are neither "stale" nor "deleted", just permanent bloat. Scoped to `under`
        so indexing one subdirectory never evicts the rest of the repo.
        """
        if self._buffer is None:
            return []
        prefix = os.path.normcase(under.rstrip("\\/")) + os.sep
        dropped = [k for k in self._buffer
                   if os.path.normcase(k).startswith(prefix) and k not in keep]
        for k in dropped:
            del self._buffer[k]
        return dropped

    def keep(self, path_key: str, rec: dict) -> None:
        """Re-assert an existing index record during a batch (file unchanged).

        Needed because a batch buffers the index and writes it wholesale: a file that
        was skipped as unchanged still has to appear in the buffer, or it would be
        dropped from the index entirely.
        """
        if self._buffer is not None:
            self._buffer[path_key] = rec

    def index_snapshot(self) -> dict:
        """The raw `path -> {hash, language}` index, read once.

        `is_fresh()` re-reads the index per call, which is fine for a single file but
        O(N) reads when auditing a whole repo (as `memo status` does). Callers that
        need every path should take one snapshot and compare hashes themselves.
        """
        return self._load_index()

    def is_fresh(self, path: Path) -> bool:
        """True if the file's current content hash matches the indexed hash."""
        key = normalize_path(path)
        rec = self._load_index().get(key)
        if not rec:
            return False
        if not path.is_file():
            return False
        return rec["hash"] == sha256_of_file(path)

    def store(self, entry: CacheEntry) -> None:
        entry.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        entry.memo_version = __version__
        self.ensure()
        self._entry_file(entry.content_hash).write_text(
            json.dumps(entry.to_json(), indent=2), encoding="utf-8"
        )
        rec = {"hash": entry.content_hash, "language": entry.language}
        if self._buffer is not None:
            self._buffer[entry.path] = rec
            return  # index.json (and the manifest) are written once at batch exit
        index = self._load_index()
        index[entry.path] = rec
        self._save_index(index)
        # The manifest no longer matches the index; drop it rather than let a query
        # read stale symbols. list_entries() rebuilds it on the next call.
        self._drop_manifest()

    # --- manifest: one read instead of one-per-file ----------------------
    # Every query command calls list_entries(), which otherwise opens one JSON per
    # indexed file (measured: 213 ms / 540 files, and it scales linearly). The whole
    # cache is small, so consolidating it into a single file makes that a single read.
    # The manifest embeds the index it was built from, so staleness is exact rather
    # than heuristic: if the index has changed at all, the manifest is not used.

    def _drop_manifest(self) -> None:
        try:
            self.manifest_path.unlink()
        except OSError:
            pass

    def write_manifest(self, entries: list[CacheEntry] | None = None) -> None:
        """Consolidate all entries into one file keyed to the current index."""
        index = self._load_index()
        if entries is None:
            entries = self._read_all_entries(index)
        self.ensure()
        payload = {
            "version": 1,
            # The index alone is not enough to validate this file: an analyzer change
            # alters cached symbols while paths and content hashes stay identical.
            "memo_version": __version__,
            "index": index,
            "entries": [e.to_json() for e in entries],
        }
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.manifest_path)  # atomic: never leave a half-written manifest

    def _read_manifest(self, index: dict) -> list[CacheEntry] | None:
        if not self.manifest_path.is_file():
            return None
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if (data.get("version") != 1
                or data.get("memo_version") != __version__
                or data.get("index") != index):
            return None  # index or analyzer moved on; fall back to per-file entries
        out: list[CacheEntry] = []
        for d in data.get("entries", []):
            try:
                out.append(CacheEntry(**d))
            except TypeError:
                return None  # schema drift; rebuild
        return out

    def _read_all_entries(self, index: dict) -> list[CacheEntry]:
        """One entry per *indexed path*.

        Entries are stored per content hash, so two paths holding identical bytes share
        one cache file — and that file records only one of the paths. Returning it
        verbatim silently dropped the other path from every consumer: the call graph
        omitted it entirely, and the incremental indexer re-analyzed it every run
        because it never appeared in the "already cached" set. Each path therefore gets
        its own entry, with `path` set to the key it was indexed under.
        """
        out: list[CacheEntry] = []
        by_hash: dict[str, CacheEntry | None] = {}
        for path_key, rec in index.items():
            h = rec["hash"]
            if h not in by_hash:
                by_hash[h] = self._read_entry(h)
            e = by_hash[h]
            if e is None:
                continue
            out.append(e if e.path == path_key else replace(e, path=path_key))
        return out

    def list_entries(self) -> list[CacheEntry]:
        index = self._load_index()
        cached = self._read_manifest(index)
        if cached is not None:
            return cached
        entries = self._read_all_entries(index)
        if entries:
            # Self-heal so only the first call after a change pays the slow path.
            try:
                self.write_manifest(entries)
            except OSError:
                pass
        return entries

    # --- clearing --------------------------------------------------------
    def clear_path(self, path: Path) -> bool:
        """Remove the cache entry for one path. Returns True if something was removed."""
        key = normalize_path(path)
        index = self._load_index()
        rec = index.pop(key, None)
        if rec is None:
            return False
        # Only delete the content file if no other path still references that hash.
        still_used = any(r["hash"] == rec["hash"] for r in index.values())
        if not still_used:
            f = self._entry_file(rec["hash"])
            if f.is_file():
                f.unlink()
        self._save_index(index)
        self._drop_manifest()
        return True

    def clear_all(self) -> int:
        """Remove all cached entries. Returns the number of content files removed."""
        count = 0
        if self.cache_dir.is_dir():
            for f in self.cache_dir.glob("*.json"):
                f.unlink()
                count += 1
        if self.index_path.is_file():
            self.index_path.unlink()
        # Every derived artifact must go too. Leaving them behind meant `memo status`
        # reported thousands of graph symbols against a zero-file index, and
        # list_entries() would keep serving the cleared entries from the manifest.
        self._drop_manifest()
        for name in ("graph.json", "shards.json"):
            try:
                (self.root / name).unlink()
            except OSError:
                pass
        return count

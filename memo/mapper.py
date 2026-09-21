"""`memo map <query>` — a queryable def/use routing digest built from the cache.

This is the agent-facing complement to prose summaries. Instead of asking an
assistant to read files, it answers routing questions cheaply:

    - Where is <query> *defined*?  (from the structured symbols already cached)
    - Which files *reference* <query>, and how heavily?  (on-disk token scan)
    - What are the likely *entry points* (files that both define and are
      heavily referenced)?

The tool does the scanning and returns only a compact digest, so the caller's
context sees a few hundred tokens instead of the raw files or a wall of grep
lines. Definitions are free (no file IO); references re-read the cached files
from disk so counts reflect current content.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .cache import CacheEntry


def _short_name(name: str) -> str:
    """Bare symbol name: strip `Class.` / `Foo#` qualifiers used by analyzers."""
    return re.split(r"[.#]", name)[-1]


def common_root(paths: list[str]) -> str:
    """Longest shared *directory* across `paths` (for terse relativization).

    Strips each path to its directory before comparing, rather than comparing the raw
    paths and hoping the mismatch happens before the filename. That distinction is not
    academic: when every symbol comes from the same one file (a single-file repo, or
    simply the first file cached), every raw path is identical, so the old zip-based
    prefix walk matched every segment *including the filename* and returned it as the
    "root" — a file, not a directory. `arch` then printed a file as the repo root, and
    `changed` shelled out to `git -C <that file>`, which fails outright ("Not a
    directory"). Comparing directories throughout means a single file degenerates to
    exactly that file's own directory, with no special case needed.
    """
    dirs = [p.replace("\\", "/").rstrip("/").rsplit("/", 1)[0] for p in paths if p]
    if not dirs:
        return ""
    common: list[str] = []
    for parts in zip(*[d.split("/") for d in dirs]):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    return "/".join(common)


def rel(path: str, root: str) -> str:
    """Path relative to `root` (falls back to the full path)."""
    p = path.replace("\\", "/")
    if root and p.startswith(root):
        return p[len(root):].lstrip("/") or p.rsplit("/", 1)[-1]
    return p


def disambiguate(paths: list[str]) -> dict[str, str]:
    """Shortest unambiguous label per path: basename unless it collides, then add
    just enough parent segments. Minimizes tokens in terse output."""
    from collections import Counter

    norm = {p: p.replace("\\", "/") for p in set(paths) if p}
    base_counts = Counter(np.rsplit("/", 1)[-1] for np in norm.values())
    out: dict[str, str] = {}
    for p, np in norm.items():
        segs = np.split("/")
        base = segs[-1]
        if base_counts[base] == 1:
            out[p] = base
        else:
            out[p] = "/".join(segs[-2:]) if len(segs) >= 2 else base
    return out


# Split an identifier into lowercase word tokens, camelCase/PascalCase/acronym aware:
#   UctConfig -> [uct, config]   UCTId -> [uct, id]   MapToUctSiteRequest -> [map, to, uct, site, request]
# This is what makes matching identifier-aware instead of naive substring, so a
# query like "uct" does NOT match "Constructor" / "Product" / "Instruction".
_TOK_SPLIT = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _id_tokens(name: str) -> list[str]:
    return [t.lower() for t in _TOK_SPLIT.findall(name)]


def _collapse(s: str) -> str:
    """Lowercase, boundary-stripped form: ErssMapper -> erssmapper, UCTId -> uctid."""
    return "".join(_id_tokens(s))


def _matches(name: str, qc: str) -> bool:
    """True if collapsed query `qc` is a prefix of the identifier's collapsed
    token-suffix at some word boundary.

    Identifier-token aware: "uct" matches UctConfig / MapToUctSiteRequest / UCTId
    (each at a hump boundary) but NOT Constructor / Product / Instruction; and a
    compound query like "erssmapper" (from `ErssMapper`) matches `ErssMapper`.
    """
    if not qc:
        return False
    toks = _id_tokens(name)
    for i in range(len(toks)):
        if "".join(toks[i:]).startswith(qc):
            return True
    return False


def _is_test(path: str) -> bool:
    """Heuristic: is this a test file? Catches Test/Tests namespace dirs (e.g.
    ERSS.CaseSave.Test/) and filenames whose tokens include test/tests/spec
    (e.g. FooTests.cs, CaseComparisonServiceTestsV2.cs)."""
    segs = path.replace("\\", "/").split("/")
    name = segs[-1]
    for seg in segs[:-1]:
        sl = seg.lower()
        if sl in ("test", "tests") or sl.endswith((".test", ".tests")):
            return True
    stem = name.split(".")[0]
    if {"test", "tests", "spec"} & set(_id_tokens(stem)):
        return True
    return name.lower().endswith((".test.ts", ".spec.ts", ".test.js", ".spec.js"))


def _count_refs(text: str, qc: str, cache: dict | None = None) -> int:
    """Count identifiers in `text` that match the collapsed query at a token boundary.

    Counts *distinct* identifiers and multiplies by their frequency, rather than
    testing every occurrence. Source code is enormously repetitive — a 541-file repo
    has 844,719 identifier occurrences drawn from only 37,657 distinct names — so the
    naive form called `_matches` 22x more often than it needed to, and that dominated
    `memo map` (1,763 ms of a 2,300 ms command; the file reads were only 149 ms).

    Pass a `cache` dict to memoize `_matches` across files within one command. It is
    keyed by (identifier, query) — NOT identifier alone, which would silently return
    the first term's verdict for every other term in a multi-term query.
    """
    if cache is None:
        cache = {}
    count = 0
    for ident, n in Counter(_IDENT.findall(text)).items():
        key = (ident, qc)
        hit = cache.get(key)
        if hit is None:
            hit = cache[key] = _matches(ident, qc)
        if hit:
            count += n
    return count


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def build_map(entries: list[CacheEntry], query: str, scan_refs: bool = True) -> dict:
    """Return a routing digest for `query` across the cached `entries`.

    Single-term: {query, definitions, references, entry_points, scanned, missing}.
    Multi-term (whitespace-separated): AND co-occurrence ranking — see _build_multimap.
    All plain data so it can be rendered as markdown or emitted as JSON.
    """
    terms = query.split()
    if len(terms) > 1:
        return _build_multimap(entries, terms, scan_refs)

    q = query.lower()
    qc = _collapse(query)  # boundary-stripped form used for token matching

    # --- definitions: from cached structured symbols (no file IO) -----------
    definitions: list[dict] = []
    for e in entries:
        matched = []
        for s in e.structured.get("symbols", []):
            if _matches(_short_name(s.get("name", "")), qc):
                matched.append({"name": s["name"], "kind": s.get("kind", ""), "line": s.get("line", 0)})
        purpose = e.structured.get("purpose", "")
        # Concept queries: also surface files whose purpose mentions the query
        # as a whole word (so "uct" doesn't match "reconstructing").
        purpose_hit = re.search(r"\b" + re.escape(q), purpose.lower()) is not None
        if matched or purpose_hit:
            definitions.append(
                {
                    "path": e.path,
                    "name": Path(e.path).name,
                    "language": e.language,
                    "purpose": purpose,
                    "symbols": matched,
                    "purpose_only": not matched and purpose_hit,
                    "is_test": _is_test(e.path),
                }
            )

    # --- references: on-disk case-insensitive substring count ---------------
    references: list[dict] = []
    scanned = 0
    missing = 0
    if scan_refs:
        # One cache for the whole scan: identifiers repeat ~22x across a repo, so most
        # files re-ask questions earlier files already answered.
        match_cache: dict = {}
        for e in entries:
            p = Path(e.path)
            if not p.is_file():
                missing += 1
                continue
            scanned += 1
            count = _count_refs(_read_text(p), qc, match_cache)
            if count:
                references.append({"path": e.path, "name": p.name, "count": count})
        references.sort(key=lambda r: r["count"], reverse=True)

    # --- entry points: PRODUCTION files that define a match AND are referenced -
    ref_by_path = {r["path"]: r["count"] for r in references}
    def_paths = {d["path"] for d in definitions if d["symbols"] and not d["is_test"]}
    entry_points = sorted(
        (
            {
                "name": Path(p).name,
                "path": p,
                "defs": sum(len(d["symbols"]) for d in definitions if d["path"] == p),
                "refs": ref_by_path.get(p, 0),
            }
            for p in def_paths
        ),
        key=lambda x: (x["refs"], x["defs"]),
        reverse=True,
    )

    return {
        "query": query,
        "definitions": definitions,
        "references": references,
        "entry_points": entry_points,
        "scanned": scanned,
        "missing": missing,
    }


def _build_multimap(entries: list[CacheEntry], terms: list[str], scan_refs: bool) -> dict:
    """Rank files by how many of `terms` they contain (defs or refs) — AND scoring.

    A file matching *all* terms ranks above one matching a subset. Fixes the
    single-term failure where "OON" alone crowned a file that never mentions the
    other concept (e.g. Premera).
    """
    qcs = [(t, _collapse(t)) for t in terms]
    files: list[dict] = []
    scanned = missing = 0
    # Shared across files AND terms (keyed by identifier+query), which matters more
    # here: a multi-term query re-scans every file once per term.
    match_cache: dict = {}
    for e in entries:
        matched_syms: dict[str, list[dict]] = {}
        for s in e.structured.get("symbols", []):
            sn = _short_name(s.get("name", ""))
            for t, qc in qcs:
                if _matches(sn, qc):
                    matched_syms.setdefault(t, []).append(
                        {"name": s["name"], "kind": s.get("kind", ""), "line": s.get("line", 0)}
                    )
        ref_counts: dict[str, int] = {}
        if scan_refs:
            p = Path(e.path)
            if p.is_file():
                scanned += 1
                text = _read_text(p)
                for t, qc in qcs:
                    c = _count_refs(text, qc, match_cache)
                    if c:
                        ref_counts[t] = c
            else:
                missing += 1
        matched_terms = sorted(set(matched_syms) | set(ref_counts), key=terms.index)
        if matched_terms:
            files.append({
                "path": e.path, "name": Path(e.path).name, "language": e.language,
                "is_test": _is_test(e.path), "purpose": e.structured.get("purpose", ""),
                "matched_terms": matched_terms, "score": len(matched_terms),
                "symbols": matched_syms, "ref_counts": ref_counts,
            })
    files.sort(key=lambda f: (-f["score"], f["is_test"], -sum(f["ref_counts"].values())))
    return {"multi": True, "query": " ".join(terms), "terms": terms,
            "files": files, "scanned": scanned, "missing": missing}


def _render_multimap(result: dict, limit: int = 25) -> str:
    terms = result["terms"]
    files = result["files"]
    lines = [f'# Map: "{result["query"]}"  ({len(terms)} terms, AND-ranked)', ""]
    lines.append(f"Scanned {result['scanned']} file(s). "
                 f"Ranked by co-occurrence of: {', '.join(terms)}.")
    lines.append("")
    by_score: dict[int, list[dict]] = {}
    for f in files:
        by_score.setdefault(f["score"], []).append(f)
    for score in sorted(by_score, reverse=True):
        bucket = [f for f in by_score[score] if not f["is_test"]]
        if not bucket:
            continue
        header = "all terms" if score == len(terms) else f"{score}/{len(terms)} terms"
        lines.append(f"## Matches {header}")
        for f in bucket[:limit]:
            lines.append(f"- **{f['name']}** ({f['language']}) — matched: {', '.join(f['matched_terms'])}")
            lines.append(f"  `{f['path']}`")
            for t in f["matched_terms"]:
                syms = f["symbols"].get(t, [])
                if syms:
                    shown = ", ".join(
                        f"{s['name']} `{s['kind']}`" + (f":{s['line']}" if s.get("line") else "")
                        for s in syms[:6]
                    )
                    lines.append(f"  - {t} defs: {shown}")
                elif t in f["ref_counts"]:
                    lines.append(f"  - {t} refs: {f['ref_counts'][t]}")
        lines.append("")
    if not files:
        lines.append(f'_No cached file matches any of: {", ".join(terms)}._')
    return "\n".join(lines).rstrip() + "\n"


def _terse_multimap(result: dict, limit: int = 25) -> str:
    terms = result["terms"]
    files = [f for f in result["files"] if not f["is_test"]]
    lbl = disambiguate([f["path"] for f in result["files"]])
    lines = [f'map "{result["query"]}" (terse, AND / {len(terms)} terms)']
    for f in files[:limit]:
        parts = []
        for t in f["matched_terms"]:
            syms = f["symbols"].get(t, [])
            if syms:
                locs = ",".join(str(s["line"]) for s in syms[:6] if s.get("line"))
                parts.append(f"{t}@{locs}")
            elif t in f["ref_counts"]:
                parts.append(f"{t}(ref{f['ref_counts'][t]})")
        lines.append(f"[{f['score']}/{len(terms)}] {lbl[f['path']]} | {' '.join(parts)}")
    if not files:
        lines.append("(no production matches)")
    return "\n".join(lines) + "\n"


def _terse_map(result: dict, limit: int = 25) -> str:
    defs = [d for d in result["definitions"] if d["symbols"]]
    refs = result["references"]
    lbl = disambiguate([d["path"] for d in defs] + [r["path"] for r in refs])
    lines = [f'map "{result["query"]}" (terse)']
    for d in [d for d in defs if not d["is_test"]][:limit]:
        syms = ", ".join(
            f"{s['name']}:{s['line']}" if s.get("line") else s["name"]
            for s in d["symbols"][:8]
        )
        lines.append(f"{lbl[d['path']]} | {syms}")
    tests = [d for d in defs if d["is_test"]]
    if tests:
        lines.append("tests: " + ", ".join(lbl[d["path"]] for d in tests[:10]))
    if refs:
        lines.append("refs: " + " ".join(f"{lbl[r['path']]}({r['count']})" for r in refs[:12]))
    if not defs and not refs:
        lines.append(f'(nothing matches "{result["query"]}")')
    return "\n".join(lines) + "\n"


def render_map(result: dict, limit: int = 25, terse: bool = False) -> str:
    if result.get("multi"):
        return _terse_multimap(result, limit) if terse else _render_multimap(result, limit)
    if terse:
        return _terse_map(result, limit)
    q = result["query"]
    defs = result["definitions"]
    refs = result["references"]
    eps = result["entry_points"]

    lines: list[str] = [f'# Map: "{q}"', ""]
    total_defs = sum(len(d["symbols"]) for d in defs)
    lines.append(
        f"Scanned {result['scanned']} cached file(s); "
        f"{total_defs} matching definition(s), {len(refs)} file(s) referencing."
    )
    lines.append("")

    if eps:
        lines.append("## Likely entry points (defines + referenced)")
        for ep in eps[:8]:
            lines.append(f"- **{ep['name']}** — {ep['defs']} def(s), {ep['refs']} ref(s)")
        lines.append("")

    prod_files = [d for d in defs if d["symbols"] and not d["is_test"]]
    test_files = [d for d in defs if d["symbols"] and d["is_test"]]
    if prod_files:
        lines.append("## Defined in (production)")
        for d in prod_files[:limit]:
            lines.append(f"- **{d['name']}** ({d['language']}) — {d['purpose'] or '_no purpose_'}")
            lines.append(f"  `{d['path']}`")
            syms = ", ".join(
                f"{s['name']} `{s['kind']}`" + (f":{s['line']}" if s.get("line") else "")
                for s in d["symbols"][:12]
            )
            extra = len(d["symbols"]) - 12
            lines.append(f"  - {syms}" + (f" _(+{extra} more)_" if extra > 0 else ""))
        lines.append("")

    if test_files:
        total = sum(len(d["symbols"]) for d in test_files)
        lines.append(f"## Also defined in tests ({total} symbol(s) across {len(test_files)} file(s))")
        lines.append(", ".join(d["name"] for d in test_files[:limit]))
        lines.append("")

    purpose_only = [d for d in defs if d["purpose_only"]]
    if purpose_only:
        lines.append("## Purpose mentions (no symbol match)")
        for d in purpose_only[:limit]:
            lines.append(f"- **{d['name']}** — {d['purpose']}")
        lines.append("")

    if refs:
        lines.append("## Referenced in (by mention count)")
        for r in refs[:limit]:
            lines.append(f"- {r['name']:<40} {r['count']}")
        if len(refs) > limit:
            lines.append(f"- _…and {len(refs) - limit} more file(s)._")
        lines.append("")

    if not prod_files and not test_files and not refs and not purpose_only:
        lines.append(f'_No cached file defines or references "{q}". '
                     "Have you run `memo cache-dir` on the repo?_")

    return "\n".join(lines).rstrip() + "\n"

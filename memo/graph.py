"""Relationship + value-search features built on the cache's structured symbols.

Everything here is agent-facing: it turns "trace this by reading five files" and
"grep then figure out which method the hit is in" into single commands that
return a compact, line-numbered digest.

Design notes / honesty boundaries:
  * The call graph is **name-based and recall-biased**. An edge means "a method's
    body contains `name(`", so it will include interface/impl fan-out and
    same-named methods across classes. Every edge carries a `path:line` so a
    single read confirms it. It does NOT resolve dynamic dispatch, reflection,
    delegates, or aliased calls.
  * "Enclosing symbol" and "method body" use a line-range heuristic: a symbol
    owns lines from its declaration up to the next symbol's declaration. Good
    enough for routing; not a parser.
"""

from __future__ import annotations

import math
import re
import shlex
from collections import Counter
from pathlib import Path

from .cache import CacheEntry
from .console import SYM
from . import lexical
from .analyzers.base import line_at, line_starts
from .graphindex import (
    _CALL, _IDENT, TIER_CONTRA, TIER_NAMES, TIER_OK, TIER_VERIFIED, TIER_WEAK, Graph,
    _body_range, _enclosing, _line_of, _sorted_symbols,
)
from .mapper import _collapse, _is_test, _matches, _read_text, _short_name, disambiguate
from .tokens import count_tokens


def build_symbol_index(entries: list[CacheEntry]) -> dict[str, list[tuple[CacheEntry, dict]]]:
    """Map lowercased short symbol name -> [(entry, symbol), ...] across the cache."""
    idx: dict[str, list[tuple[CacheEntry, dict]]] = {}
    for e in entries:
        for s in e.structured.get("symbols", []):
            key = _short_name(s.get("name", "")).lower()
            if key:
                idx.setdefault(key, []).append((e, s))
    return idx


# --------------------------------------------------------------------------- #
# find — value/literal search with enclosing-symbol attribution
# --------------------------------------------------------------------------- #

def _scan_text_for_hits(path: str, name: str, language: str, is_test: bool,
                        text: str, symbols: list[dict], pattern: re.Pattern) -> dict | None:
    """One file's hit list, or None if the pattern doesn't occur.

    Shared by `find_value` (cached files, real `symbols` for enclosing-symbol
    attribution) and `find_value_wide` (arbitrary files with no structured symbols —
    `symbols=[]` so every hit reads honestly as file scope, not a guessed method).
    """
    # Precomputed once per file, not once per hit — a file with many matches otherwise
    # costs time quadratic in its size (re-counting newlines / rebuilding splitlines()
    # from the top for every match).
    starts = line_starts(text)
    all_lines = text.splitlines()
    hits: list[dict] = []
    for m in pattern.finditer(text):
        line = line_at(starts, m.start())
        encl = _enclosing(symbols, line) if symbols else None
        line_text = all_lines[line - 1].strip() if line - 1 < len(all_lines) else ""
        hits.append({
            "line": line,
            "enclosing": encl["name"] if encl else "(file scope)",
            "text": line_text[:160],
        })
    if not hits:
        return None
    return {"path": path, "name": name, "language": language, "is_test": is_test,
            "hits": hits}


def find_value(entries: list[CacheEntry], query: str, regex: bool = False,
               ignore_case: bool = True) -> dict:
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile(query if regex else re.escape(query), flags)

    files: list[dict] = []
    scanned = missing = total_hits = 0
    for e in entries:
        p = Path(e.path)
        if not p.is_file():
            missing += 1
            continue
        scanned += 1
        text = _read_text(p)
        symbols = _sorted_symbols(e)
        hit = _scan_text_for_hits(e.path, p.name, e.language, _is_test(e.path),
                                  text, symbols, pattern)
        if hit:
            total_hits += len(hit["hits"])
            files.append(hit)
    files.sort(key=lambda f: (f["is_test"], -len(f["hits"])))
    return {"query": query, "regex": regex, "case_sensitive": not ignore_case, "files": files,
            "scanned": scanned, "missing": missing, "total_hits": total_hits}


def find_value_wide(paths: list[Path], query: str, regex: bool = False,
                    ignore_case: bool = True) -> list[dict]:
    """Scan arbitrary (non-cached) files for `query` — the `--all` closer for `find`'s
    default blind spot: the extension allowlist excludes docs/config/markdown from the
    index, so a literal living only in a README or a `.config` was invisible with no
    signal anything was missed. No structured symbols exist for these files, so every
    hit is honestly attributed to "(file scope)" rather than a guessed enclosing method.
    """
    flags = re.IGNORECASE if ignore_case else 0
    pattern = re.compile(query if regex else re.escape(query), flags)
    out: list[dict] = []
    for p in paths:
        if not p.is_file():
            continue
        hit = _scan_text_for_hits(str(p), p.name, "(unindexed)", _is_test(str(p)),
                                  _read_text(p), [], pattern)
        if hit:
            out.append(hit)
    return out


def _rerun_with_all(result: dict) -> str:
    """The exact command to re-run this same query with `--all` — reproducing the
    original flags (regex, case-sensitivity) so it isn't a trap that silently drops
    them. Handing back a ready-to-run command instead of naming a flag is the same
    fix `memo tests`'s `_run_command` already makes for its own suggestion: a caveat
    that leaves the caller to reconstruct the exact invocation is one round trip they
    shouldn't have to spend, and a real live trial showed an agent read the flag-only
    caveat, correctly understood the gap existed, and still reached for a raw grep of
    its own instead of the one command that was already the answer.
    """
    parts = ["python -m memo.cli find", shlex.quote(result["query"])]
    if result.get("regex"):
        parts.append("--regex")
    if result.get("case_sensitive"):
        parts.append("--case-sensitive")
    parts.append("--all")
    return " ".join(parts)


def render_find(result: dict, limit: int = 25, per_file: int = 12, terse: bool = False) -> str:
    q = result["query"]
    files = result["files"]
    wide = result.get("wide", False)
    if terse:
        lbl = disambiguate([f["path"] for f in files])
        out = [f'find "{q}" (terse{", --all" if wide else ""}) — {result["total_hits"]} hit(s)']
        for f in files[:limit]:
            for h in f["hits"][:per_file]:
                out.append(f"{lbl[f['path']]}:{h['line']} {h['enclosing']} | {h['text']}")
        if not files:
            out.append("(no hits)")
        if not wide:
            out.append(_rerun_with_all(result))
        return "\n".join(out) + "\n"
    lines = [f'# Find: "{q}"' + ("  (regex)" if result["regex"] else ""), ""]
    lines.append(f"{result['total_hits']} hit(s) in {len(files)} file(s) "
                 f"(scanned {result['scanned']}{' cached + non-cached' if wide else ' cached'}).")
    lines.append("")
    for f in files[:limit]:
        tag = " [test]" if f["is_test"] else ""
        lines.append(f"- **{f['name']}**{tag} — {len(f['hits'])} hit(s)")
        lines.append(f"  `{f['path']}`")
        for h in f["hits"][:per_file]:
            lines.append(f"  - :{h['line']} in `{h['enclosing']}` | {h['text']}")
        if len(f["hits"]) > per_file:
            lines.append(f"  - _…and {len(f['hits']) - per_file} more hit(s) in this file._")
    if len(files) > limit:
        lines.append(f"\n_…and {len(files) - limit} more file(s)._")
    if not files:
        lines.append(f'_No {"file" if wide else "cached file"} contains "{q}"._')
    if wide:
        # The gap is closed for this call: say so plainly rather than repeating the
        # caveat below, which would read as "still incomplete" when it no longer is.
        lines.append("\n_Scanned cached files plus every other tracked, non-ignored "
                     "file in the repo (--all) — docs/config/markdown included._")
    else:
        # Silent partial coverage is worse than an honest empty result: a query that hits
        # in a handful of .py files reads as complete, but `find` only scans *cached*
        # files by default, and the extension allowlist (config.py's DEFAULT_LANGUAGES)
        # excludes .md/.html/.json/.txt — so a literal that also lives in docs or config
        # is invisible here without ever looking like a miss. Measured live: a search for
        # a real filename constant found every .py occurrence and silently missed it in
        # README.md and a slide deck, undetected until a native grep fallback caught the
        # gap by accident. `--all` (added after that incident) closes it for real.
        #
        # A named flag alone wasn't enough: a follow-up live trial showed an agent read
        # this exact caveat, correctly understood the gap, and *still* reached for its
        # own raw grep instead of the flag it had just been told about — a ready-to-run
        # command is harder to route around than a flag name is.
        lines.append(f"\n_Scanned only cached files ({result['scanned']} of them) — the "
                     "extension allowlist may exclude docs/config/markdown. To also scan "
                     f"those, run:_\n\n```\n{_rerun_with_all(result)}\n```")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# call graph — callers / calls (recall-biased, name-based)
# --------------------------------------------------------------------------- #

def find_callers(graph: Graph, query: str, exact: bool = False) -> dict:
    """Methods that call any symbol matching `query` — from the persisted graph."""
    qc = _collapse(query)
    out = graph.callers(qc, exact)
    for r in out:
        if r["caller"] is None:
            r["caller"] = "(file scope)"
    defs = graph.matching_defs(qc, exact)
    return {"query": query, "callers": out, "count": len(out), "definitions": defs,
            "resolved_to": graph.unanimous_target(qc, exact)}


def find_calls(graph: Graph, query: str, exact: bool = False) -> dict:
    """Project-defined symbols invoked inside the method(s) matching `query`."""
    return {"query": query, "definitions": graph.calls(_collapse(query), exact)}


# --------------------------------------------------------------------------- #
# refs — field/property read and write sites (a distinct edge type from calls:
# "who touches this shared state," which no `name(` call-graph edge can answer)
# --------------------------------------------------------------------------- #

def find_field_refs(graph: Graph, query: str, exact: bool = False) -> dict:
    """Every recorded read/write of a field/property matching `query`.

    Only ever non-empty for kinds an analyzer declares as field-like (today: C#/VB.NET
    `property`) — Python/JS/markup files correctly produce nothing here, not a gap, since
    they declare no field symbols to track accesses of in the first place.
    """
    qc = _collapse(query)
    sites = graph.field_access_sites(qc, exact)
    defs = [s for s in graph.matching_defs(qc, exact) if s.get("kind") == "property"]
    return {"query": query, "sites": sites, "count": len(sites), "definitions": defs}


def render_field_refs(result: dict, limit: int = 40, terse: bool = False) -> str:
    q = result["query"]
    sites = result["sites"]
    if not result["definitions"] and not sites:
        return (f'_No field/property named "{q}" found — field tracking currently only '
               "covers C#/VB.NET `property` declarations._\n")
    if terse:
        lbl = disambiguate([s["path"] for s in sites])
        out = [f'refs "{q}" (terse, candidate) — {result["count"]} site(s)']
        for s in sites[:limit]:
            mark = "write" if s["is_write"] else "read"
            encl = s["enclosing"] or "(file scope)"
            out.append(f"{mark} {encl} {lbl[s['path']]}:{s['line']}")
        if not sites:
            out.append("(no recorded reads/writes)")
        return "\n".join(out) + "\n"
    lines = [f'# Field/property refs: "{q}"  _(candidate, name-based)_', ""]
    lines.append(f"{result['count']} site(s).")
    lines.append("")
    prod = [s for s in sites if not s["is_test"]]
    test = [s for s in sites if s["is_test"]]
    if prod:
        lines.append("## Production sites (writes first)")
        lbl = disambiguate([s["path"] for s in prod])
        for s in prod[:limit]:
            mark = "**write**" if s["is_write"] else "read"
            encl = s["enclosing"] or "(file scope)"
            lines.append(f"- {mark} in `{encl}` — {lbl[s['path']]}:{s['line']}")
    if test:
        lines.append("")
        lines.append(f"## Test sites ({len(test)})")
        lines.append(", ".join(sorted({s["enclosing"] or "(file scope)" for s in test})))
    if not sites:
        lines.append(f'_No recorded reads/writes of "{q}" — declared but never touched, '
                     "or only touched via reflection/serialization the graph can't see._")
    return "\n".join(lines).rstrip() + "\n"


def _ambiguity_note(result: dict) -> str | None:
    """If the query name resolves to >1 distinct definition, say so — name-based
    edges can't tell which one a call site targets.

    Kept verbatim unless the receiver actually settled it. Arity agreement alone must not
    silence this: same-arity overloads are exactly the case a count cannot separate, and a
    quiet wrong answer is worse than a noisy uncertain one.
    """
    defs = result.get("definitions", [])
    distinct = sorted({(d["name"], Path(d["path"]).name, d["line"]) for d in defs})
    if len(distinct) <= 1:
        return None
    resolved = result.get("resolved_to")
    if resolved:
        return (f'{SYM["ok"]} "{result["query"]}" matches {len(distinct)} definitions, '
                f"but every call site resolves to {resolved}.")
    listing = "; ".join(f"{n} ({f}:{ln})" for n, f, ln in distinct[:6])
    more = f" (+{len(distinct) - 6} more)" if len(distinct) > 6 else ""
    example_at = f"{distinct[0][1]}:{distinct[0][2]}"
    return (f'{SYM["warn"]} "{result["query"]}" matches {len(distinct)} definitions: '
            f'{listing}{more}. '
            "Name-based edges can't tell which a call targets — add the bare `--exact` "
            "flag (no value) to require the receiver's declared type to match. To see "
            "one definition's actual source instead, its `file:line` is already right "
            f'above — go straight there: `memo peek --at {example_at}`.')


def _tier_mark(tier: int | None, terse: bool = True) -> str:
    """The confidence marker appended to a row.

    In `--terse`, only a *contradiction* is marked. Every marker costs exactly one token
    (measured across `*`, `!`, `+`, leading and trailing — the tokenizer charges the same
    for all of them), and rows already arrive sorted by tier, so marking the confident
    ones spends a token per row to repeat what the ordering says. On the widest-fan-out
    benchmark scenario that tax was 199 tokens for no information. A contradiction is the
    one thing the sort order cannot express, and it is rare enough to be nearly free.

    Full output is read by people rather than counted against a budget, so it names every
    informative tier.
    """
    if tier is None or tier == TIER_WEAK or tier == TIER_OK:
        return ""
    if terse:
        if tier == TIER_CONTRA:
            return "!"
        if tier == TIER_VERIFIED:
            return "*"  # observed at runtime — the one static ordering can't express
        return ""
    # Bare word, no markdown punctuation: `  exact` costs 2 tokens where `  _(exact)_`
    # costs 4, for the same word. The wrapping was the expensive half.
    return f"  {TIER_NAMES[tier]}"


def render_callers(result: dict, limit: int = 40, terse: bool = False) -> str:
    q = result["query"]
    cs = result["callers"]
    note = _ambiguity_note(result)
    if terse:
        lbl = disambiguate([c["path"] for c in cs])
        out = [f'callers "{q}" (terse, candidate)']
        if note:
            out.append(note)
        for c in [c for c in cs if not c["is_test"]][:limit]:
            at = ",".join(str(l) for l in c["call_lines"][:6])
            out.append(f"{c['caller']} {lbl[c['path']]}:{c['caller_line']} @{at}"
                       f"{_tier_mark(c.get('tier'))}")
        tests = [c for c in cs if c["is_test"]]
        if tests:
            out.append(f"tests({len(tests)}): " + ", ".join(sorted({c['name'] for c in tests})[:10]))
        if not cs:
            out.append("(no callers)")
        return "\n".join(out) + "\n"
    lines = [f'# Callers of "{q}"  _(candidate, name-based)_', ""]
    if note:
        lines.append(note)
    lines.append(f"{result['count']} calling site group(s).")
    lines.append("")
    prod = [c for c in cs if not c["is_test"]]
    test = [c for c in cs if c["is_test"]]
    if prod:
        lines.append("## Production callers")
        for c in prod[:limit]:
            at = ", ".join(f":{ln}" for ln in c["call_lines"][:6])
            lines.append(f"- **{c['caller']}** — {c['name']}  (calls at {at})"
                         f"{_tier_mark(c.get('tier'), terse=False)}")
            lines.append(f"  `{c['path']}`")
    if test:
        lines.append("")
        lines.append(f"## Test callers ({len(test)})")
        lines.append(", ".join(sorted({c["name"] for c in test})))
    if not cs:
        lines.append(f'_No caller found for "{q}" in the cache._')
    return "\n".join(lines).rstrip() + "\n"


def render_calls(result: dict, limit: int = 40, terse: bool = False) -> str:
    q = result["query"]
    defs = result["definitions"]
    if terse:
        lbl = disambiguate([c["path"] for d in defs for c in d["callees"]])
        out = [f'calls by "{q}" (terse, candidate)']
        for d in defs:
            out.append(f"# {d['name']} ({Path(d['path']).name}:{d['line']})")
            for c in d["callees"][:limit]:
                out.append(f"{c['name']} {lbl[c['path']]}:{c['line']}"
                           f"{_tier_mark(c.get('tier'))}")
        if not defs:
            out.append("(no match)")
        return "\n".join(out) + "\n"
    lines = [f'# Calls made by "{q}"  _(candidate, name-based)_', ""]
    if not defs:
        lines.append(f'_No cached symbol matches "{q}"._')
        return "\n".join(lines) + "\n"
    for d in defs:
        lines.append(f"## {d['name']}  ({Path(d['path']).name}:{d['line']})")
        if not d["callees"]:
            lines.append("_No project-defined calls detected in its body._")
            continue
        for c in d["callees"][:limit]:
            lines.append(f"- {c['name']} `{c['kind']}` — {Path(c['path']).name}:{c['line']}"
                         f"{_tier_mark(c.get('tier'), terse=False)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# neighbors — callers + calls in one round trip
# --------------------------------------------------------------------------- #

def neighbors(graph: Graph, query: str, exact: bool = False) -> dict:
    """Both directions at once: who calls `query`, and what `query` calls.

    "How does this fit into the codebase" is the single most common orientation
    question, and it is exactly `callers` + `calls` — which otherwise costs two
    round trips for a question an agent asks once per symbol, not twice.
    """
    return {"query": query,
            "callers": find_callers(graph, query, exact=exact),
            "calls": find_calls(graph, query, exact=exact)}


def _drop_first_line(text: str) -> str:
    """Strip a rendered block's own header line.

    `render_callers`/`render_calls` each open with `callers "X" (terse, candidate)` /
    `calls by "X" (terse, candidate)` — correct standalone, but pure repetition once
    `render_neighbors` has already said "neighbors X (terse, candidate)" once and
    labeled the section with `-- callers --` / `-- calls --`. Measured on
    `memo neighbors build --exact --terse` against this repo: 255 -> 233 tokens
    (-8.6%), for zero information loss — the exact "no marker for what the
    ordering/section already says" rule the rest of this module already applies to
    confidence tiers.
    """
    _, _, rest = text.partition("\n")
    return rest


def render_neighbors(result: dict, limit: int = 20, terse: bool = False) -> str:
    q = result["query"]
    if terse:
        callers_text = _drop_first_line(render_callers(result["callers"], limit=limit, terse=terse))
        calls_text = _drop_first_line(render_calls(result["calls"], limit=limit, terse=terse))
        return (f'neighbors "{q}" (terse, candidate)\n'
                f'-- callers --\n{callers_text}-- calls --\n{calls_text}')
    callers_text = render_callers(result["callers"], limit=limit, terse=terse)
    calls_text = render_calls(result["calls"], limit=limit, terse=terse)
    return f'# Neighbors of "{q}"\n\n{callers_text}\n---\n\n{calls_text}'


# --------------------------------------------------------------------------- #
# trace — walk the caller/callee chain
# --------------------------------------------------------------------------- #

def trace(graph: Graph, query: str, direction: str, depth: int) -> dict:
    """Walk callers (up) or callees (down) up to `depth` levels. Cycle-guarded."""
    def children(name: str) -> list[dict]:
        if direction == "up":
            r = find_callers(graph, name)
            # Production execution path only — test harnesses would swamp the tree.
            return [{"name": c["caller"], "path": c["path"], "line": c["caller_line"]}
                    for c in r["callers"]
                    if c["caller"] != "(file scope)" and not c["is_test"]]
        else:
            r = find_calls(graph, name)
            seen, out = set(), []
            for d in r["definitions"]:
                for c in d["callees"]:
                    if c["name"] not in seen:
                        seen.add(c["name"])
                        out.append({"name": c["name"], "path": c["path"], "line": c["line"]})
            return out

    def walk(name: str, level: int, path_seen: set[str]) -> dict:
        node = {"name": name, "children": []}
        if level >= depth or name in path_seen:
            return node
        for ch in children(name):
            child = walk(ch["name"], level + 1, path_seen | {name})
            child["path"] = ch["path"]
            child["line"] = ch["line"]
            node["children"].append(child)
        return node

    return {"query": query, "direction": direction, "depth": depth,
            "tree": walk(query, 0, set())}


def render_trace(result: dict, terse: bool = False, limit: int = 0) -> str:
    arrow = "up" if result["direction"] == "up" else "down"
    if terse:
        lines = [f'trace {arrow} "{result["query"]}" (terse, candidate)']
    else:
        a = "callers ↑" if result["direction"] == "up" else "calls ↓"
        lines = [f'# Trace ({a}, depth {result["depth"]}): "{result["query"]}"',
                 "_(candidate edges, name-based)_", ""]

    def emit(node: dict, indent: int):
        loc = ""
        if node.get("path"):
            loc = f"  {Path(node['path']).name}:{node.get('line', 0)}"
        lines.append("  " * indent + f"- {node['name']}{loc}")
        kids = node["children"][:limit] if limit else node["children"]
        for ch in kids:
            emit(ch, indent + 1)
        if limit and len(node["children"]) > limit:
            lines.append("  " * (indent + 1) + f"- …(+{len(node['children']) - limit} more)")

    emit(result["tree"], 0)
    if not result["tree"]["children"]:
        rl = "callers" if result["direction"] == "up" else "callees"
        lines.append(f"\n_No {rl} found._")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# peek — return the exact method-complete source slice(s) for a symbol
# --------------------------------------------------------------------------- #

def _symbol_slice(e: CacheEntry, syms: list[dict], idx: int) -> dict:
    """The method-complete source slice for `syms[idx]`, doc-comment included.

    Shared by `peek` (found by name) and `peek_at` (found by coordinate) so the two
    entry points can never disagree about what "the slice" of a symbol is.
    """
    s = syms[idx]
    all_lines = _read_text(Path(e.path)).splitlines()
    start, end = _body_range(syms, idx, len(all_lines))
    # Include the immediately-preceding doc-comment / attribute block — the
    # spec/intent often lives there (and it's where RCA answers hide).
    j = start - 2  # 0-based line above the declaration
    while j >= 0:
        t = all_lines[j].strip()
        if t.startswith(("///", "//", "*", "/*", "'''", "[")) or t.endswith("*/"):
            start = j + 1
            j -= 1
        else:
            break
    code = all_lines[start - 1:end]  # 1-based inclusive
    return {"name": s["name"], "kind": s.get("kind", ""), "path": e.path,
            "start": start, "end": end, "is_test": _is_test(e.path), "code": code}


def peek(entries: list[CacheEntry], query: str, limit: int = 5) -> dict:
    """Return the source lines of symbol(s) matching `query`, method-complete.

    Replaces reading a whole file: a symbol owns [its line, next symbol's line),
    so the slice is the full declaration + body and nothing else. Production
    matches first; capped at `limit` (recall means a name can match several).
    """
    qc = _collapse(query)
    matches: list[tuple] = []  # (entry, sorted_syms, idx, symbol)
    for e in entries:
        syms = _sorted_symbols(e)
        for i, s in enumerate(syms):
            if _matches(_short_name(s["name"]), qc):
                matches.append((e, syms, i, s))

    def _rank(m):
        e, _syms, _i, s = m
        path = e.path.replace("\\", "/")
        generated = any(x in path for x in
                        ("/Connected Services/", "Reference.cs", ".g.cs",
                         ".designer.", ".Designer."))
        exact = _short_name(s["name"]).lower() != qc          # exact name first
        code_like = s.get("kind", "") not in ("method", "function", "constructor")  # logic first
        # non-test, non-generated, exact-name, method-like first
        return (_is_test(e.path), generated, exact, code_like, path, s["line"])

    matches.sort(key=_rank)

    slices: list[dict] = []
    for e, syms, i, _s in matches[:limit]:
        if not Path(e.path).is_file():
            continue
        slices.append(_symbol_slice(e, syms, i))
    return {"query": query, "total": len(matches), "shown": len(slices), "slices": slices}


def peek_at(entries: list[CacheEntry], location: str) -> dict:
    """The enclosing symbol's source at an exact `path:line` coordinate.

    The complement to name-based `peek`: every memo command that cites a coordinate
    (`callers`, `impact`, `find`, `changed`, `trace`) hands back exactly this shape,
    and without this an agent that wants more context than the citation alone has to
    fall back to a raw file Read — the one thing rule 1 ("cite as given, don't
    re-read") is meant to make unnecessary.

    `path` is matched by suffix, not equality: memo's own terse output prints a
    `disambiguate()`-shortened path (often just the basename), so a coordinate copied
    verbatim from another memo command has to resolve without the caller reconstructing
    the full absolute path themselves.
    """
    path_str, sep, line_str = location.rpartition(":")
    if not sep or not line_str.lstrip("-").isdigit():
        return {"location": location, "error": "expected <path>:<line>", "slices": []}
    line = int(line_str)
    path_norm = path_str.replace("\\", "/")
    candidates = [e for e in entries if e.path.replace("\\", "/").endswith(path_norm)]
    if not candidates:
        return {"location": location, "error": f'no cached file matches "{path_str}"',
                "slices": []}
    if len(candidates) > 1:
        # Suffix matching is inherently ambiguous for a short path (`Service.cs` could
        # be several files); shortest absolute path is the least surprising guess, but
        # say so rather than silently picking.
        candidates.sort(key=lambda c: len(c.path))
    e = candidates[0]
    if not Path(e.path).is_file():
        return {"location": location, "error": f"{e.path} no longer exists on disk",
                "slices": []}
    syms = _sorted_symbols(e)
    encl = _enclosing(syms, line)
    if encl is None:
        return {"location": location,
                "error": f"no cached symbol encloses {path_str}:{line}", "slices": []}
    idx = syms.index(encl)
    ambiguous_note = (f'{len(candidates)} cached files match "{path_str}"; used '
                      f"{e.path}" if len(candidates) > 1 else None)
    return {"location": location, "error": None, "note": ambiguous_note,
            "slices": [_symbol_slice(e, syms, idx)]}


def render_peek_at(result: dict, terse: bool = False) -> str:
    loc = result["location"]
    if result.get("error"):
        return f'_{result["error"]} ({loc})._\n'
    s = result["slices"][0]
    lines = [f"## {loc} -> {s['name']} `{s['kind']}`  (lines {s['start']}-{s['end']})"]
    if result.get("note"):
        lines.append(f"_{result['note']}_")
    lines.append("")
    if terse:
        lines.extend(s["code"])
    else:
        for n, line in enumerate(s["code"], start=s["start"]):
            lines.append(f"{n}: {line}")
    return "\n".join(lines).rstrip() + "\n"


def render_peek(result: dict, terse: bool = False) -> str:
    q = result["query"]
    slices = result["slices"]
    lines = [f'# peek "{q}" — {result["shown"]} of {result["total"]} match(es)', ""]
    lbl = disambiguate([s["path"] for s in slices])
    for s in slices:
        tag = " [test]" if s["is_test"] else ""
        lines.append(f"## {lbl[s['path']]}:{s['start']}-{s['end']}  {s['name']} `{s['kind']}`{tag}")
        if terse:  # omit per-line number prefixes to save tokens (header gives the range)
            lines.extend(s["code"])
        else:
            for n, line in enumerate(s["code"], start=s["start"]):
                lines.append(f"{n}: {line}")
        lines.append("")
    if not slices:
        lines.append(f'_No cached symbol matches "{q}"._')
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# brief — one call: rank the most relevant methods for a query, return their code
# --------------------------------------------------------------------------- #

_GENERATED = ("/connected services/", "reference.cs", ".g.cs", ".designer.")
# How much more a `brief` candidate's name_score counts when the query matches its
# collapsed name exactly, vs. only as a prefix (`_matches` treats "build_graph" as
# matching both `build_graph` and `_build_graph_config` — see brief()'s scoring loop).
_EXACT_NAME_MATCH_MULTIPLIER = 3.0
_STOPWORDS = {
    "the", "a", "an", "is", "are", "to", "in", "on", "of", "for", "and", "or",
    "when", "does", "do", "how", "what", "why", "with", "by", "from", "get",
    "set", "case", "cases", "code", "data", "value", "result", "request",
}


def _extend_over_docs(all_lines: list[str], start: int) -> int:
    """Back `start` (1-based) up over an immediately-preceding doc/attribute block."""
    j = start - 2
    while j >= 0:
        t = all_lines[j].strip()
        if t.startswith(("///", "//", "*", "/*", "'''", "[")) or t.endswith("*/"):
            start = j + 1
            j -= 1
        else:
            break
    return start


def brief(entries: list[CacheEntry], query: str, top: int = 8, max_tokens: int = 6000,
          graph: Graph | None = None) -> dict:
    """Composed retrieval: rank symbols across the repo by relevance to `query`
    (name match + call-graph centrality + file relevance), then return the CODE of
    the top ones in a single token-capped bundle. Designed to replace the
    map->peek->peek... loop with one call.

    `graph`, when passed, upgrades the centrality signal from a crude proxy (how
    often this identifier's *text* appears anywhere in the repo — which a common
    utility name wins regardless of whether it's actually structurally central) to
    real PageRank over the confidence-filtered call graph (`Graph.pagerank_score`),
    so trimming to `max_tokens` favors symbols other code actually depends on over
    ones that merely share a lexically common name. Optional and backward
    compatible: without it, ranking falls back to the original text-frequency proxy.

    Also folds in BM25 (`lexical.score`) over each candidate's name + one-line doc,
    when `rank_bm25` is installed (`pip install -e ".[search]"`) — this is what lets
    a query like "retry failed payments" surface a method whose *docstring* says
    that even though its name doesn't (`handle_txn_error`). Silently a no-op without
    the extra installed: `lexical.score` returns {} and every bm25_norm below is 0,
    so ranking is identical to before this existed.
    """
    raw_terms = [t for t in query.split() if len(t) > 1 and t.lower() not in _STOPWORDS]
    terms = [(t, _collapse(t)) for t in raw_terms]

    # Inverse-frequency term weights: a term that name-matches MANY symbols is
    # low-signal ("service", "date"); a rare term is high-signal ("retro", "expire").
    all_syms = [(_short_name(s.get("name", "")), s.get("kind", ""))
                for e in entries for s in e.structured.get("symbols", [])]
    term_weight: dict[str, float] = {}
    for t, qc in terms:
        nmatch = sum(1 for sn, _k in all_syms if _matches(sn, qc))
        term_weight[qc] = 1.0 / (1.0 + math.log(1 + nmatch))

    _LOGIC = ("method", "function")

    # One scan: file text (cached), identifier centrality, weighted file relevance.
    freq: Counter = Counter()
    file_rel: dict[str, float] = {}
    texts: dict[str, str] = {}
    for e in entries:
        p = Path(e.path)
        if not p.is_file():
            continue
        text = _read_text(p)
        texts[e.path] = text
        rel = 0.0
        for m in _IDENT.finditer(text):
            ident = m.group(0)
            freq[ident.lower()] += 1
            for _t, qc in terms:
                if _matches(ident, qc):
                    rel += term_weight[qc]  # weighted by term rarity
        file_rel[e.path] = rel

    max_rel = max(file_rel.values(), default=1.0) or 1.0

    # Real call-graph importance, when a Graph is available — normalized the same
    # way rel_norm is, so its weight below is comparable across repos of any size.
    max_pr = 0.0
    if graph is not None:
        pr_values = graph.pagerank().values()
        max_pr = max(pr_values, default=0.0)

    # BM25 over name + one-line doc — {} (and max_bm25 == 0) when rank_bm25 isn't
    # installed, which the gate/score below both already treat as "no signal".
    bm25_scores = lexical.score(query, entries)
    max_bm25 = max(bm25_scores.values(), default=0.0)

    # Score candidate LOGIC methods only (no trivial properties/classes/constructors).
    cand: list[tuple] = []
    for e in entries:
        if e.path not in texts:
            continue
        rel = file_rel.get(e.path, 0.0)
        rel_norm = rel / max_rel
        path_l = e.path.replace("\\", "/").lower()
        generated = any(x in path_l for x in _GENERATED)
        is_test = _is_test(e.path)
        syms = _sorted_symbols(e)
        for i, s in enumerate(syms):
            if is_test or generated:
                continue  # never surface test/generated methods in a brief
            if s.get("kind", "") not in _LOGIC:
                continue
            sn = _short_name(s.get("name", ""))
            # _matches is a prefix/boundary match, not equality: "build_graph" matches
            # both the function actually named that AND "_build_graph_config" (its
            # collapsed form "buildgraph" is a prefix of "buildgraphconfig"). Without
            # a bonus for the collapsed form matching exactly, those two rank as if
            # equally relevant — measured directly, this let a same-file compound
            # name (`ChatService._build_graph_config`) outrank the actual queried
            # function. An exact match is what a precise, already-known identifier in
            # the query is almost certainly asking for.
            name_score = 0.0
            for _t, qc in terms:
                if not _matches(sn, qc):
                    continue
                weight = term_weight[qc]
                if _collapse(sn) == qc:
                    weight *= _EXACT_NAME_MATCH_MULTIPLIER
                name_score += weight
            bm25_norm = (bm25_scores.get((e.path, s["line"]), 0.0) / max_bm25
                        if max_bm25 > 0 else 0.0)
            # Gate: must be genuinely query-relevant — a name match, a method in a
            # file that is strongly about the query, or (with rank_bm25 installed) a
            # real lexical match in its own doc even without one in its name. This
            # blocks ubiquitous utilities (LogInfo, SanitizeForLog) leaking in on
            # centrality alone.
            if name_score == 0 and rel_norm < 0.5 and bm25_norm == 0:
                continue
            score = name_score * 100                    # rare-term name match dominates
            score += rel_norm * 40                      # file relevance (normalized)
            score += bm25_norm * 35                      # doc/name lexical match beyond exact substring
            score += min(freq.get(sn.lower(), 0), 300) * 0.05  # text-frequency: tiebreak only
            if max_pr > 0:
                pr_norm = graph.pagerank_score(sn) / max_pr
                score += pr_norm * 30  # real call-graph importance, when available
            cand.append((score, e, syms, i, s))

    cand.sort(key=lambda c: -c[0])

    slices: list[dict] = []
    skipped_big: list[str] = []
    used = 0
    seen: set[tuple] = set()
    for score, e, syms, i, s in cand:
        if len(slices) >= top:
            break
        key = (e.path, s["line"])
        if key in seen:
            continue
        seen.add(key)
        all_lines = texts[e.path].splitlines()
        start, end = _body_range(syms, i, len(all_lines))
        start = _extend_over_docs(all_lines, start)
        code = all_lines[start - 1:end]
        n = count_tokens("\n".join(code))[0]
        if n > max_tokens:
            skipped_big.append(f"{s['name']} ({Path(e.path).name}:{s['line']}, ~{n} tok)")
            continue
        if slices and used + n > max_tokens:
            continue  # keep trying smaller, higher-ranked-remaining ones
        slices.append({
            "name": s["name"], "kind": s.get("kind", ""), "path": e.path,
            "start": start, "end": end, "tokens": n, "code": code,
        })
        used += n

    return {"query": query, "slices": slices, "used_tokens": used,
            "candidates": len(cand), "skipped_big": skipped_big}


def render_brief(result: dict, terse: bool = False) -> str:
    q = result["query"]
    slices = result["slices"]
    files = sorted({Path(s["path"]).name for s in slices})
    lines = [f'# brief "{q}" — {len(slices)} method(s) across {len(files)} file(s), ~{result["used_tokens"]} tok', ""]
    lbl = disambiguate([s["path"] for s in slices])
    for s in slices:
        lines.append(f"## {lbl[s['path']]}:{s['start']}-{s['end']}  {s['name']} `{s['kind']}`")
        if terse:  # omit per-line number prefixes to save tokens
            lines.extend(s["code"])
        else:
            for n, line in enumerate(s["code"], start=s["start"]):
                lines.append(f"{n}: {line}")
        lines.append("")
    if result["skipped_big"]:
        lines.append("_Skipped (too large — `peek` directly): " + "; ".join(result["skipped_big"][:5]) + "_")
    if slices:
        # Say how many candidates were ranked away, not just that more exist. A measured
        # A/B on "where is site lookup handled" had an agent report 4 of 6 lookup handlers
        # because the top-8 cut the two least-referenced ones; "for more, raise --top" is
        # boilerplate a reader skims, whereas "8 of 47" tells them the cut was severe.
        #
        # The fact ("8 of 47") is per-call information and survives --terse. The
        # methodology sentence after it never changes call to call and is already stated
        # once in the guide an agent reads before ever calling `brief` — repeating it on
        # every terse call is exactly the four-figure-token-count-to-advertise-itself
        # waste `_load_graph_or_die` was already trimmed for (see its docstring). Measured:
        # 42 tokens -> 10 for the fact alone.
        dropped = max(0, result.get("candidates", 0) - len(slices))
        of = f" of {result['candidates']} candidate(s)" if dropped else ""
        if terse:
            lines.append(f"_Showing {len(slices)}{of}._")
        else:
            lines.append(f"_Showing {len(slices)}{of}, ranked by name-match + call-graph "
                         "centrality + file relevance. "
                         "For more, raise --top or `peek`/`callers` a specific symbol._")
    else:
        lines.append(f'_Nothing relevant to "{q}" in the cache._')
    return "\n".join(lines).rstrip() + "\n"

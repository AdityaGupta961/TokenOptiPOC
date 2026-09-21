"""trace-stack — resolve the frames of a pasted exception trace to current coordinates.

A stack trace is the most common triage input there is, and the most expensive one to
act on: the frames name symbols, not locations, so answering "where is this code now"
costs one search per frame. This resolves every frame in a single pass over the symbol
table that is already indexed.

Two things here matter more than the lookup itself.

**Build drift.** A production trace was produced by a build that no longer matches the
working tree, so its `:line 120` is frequently wrong while its *symbol name* is still
right. An agent that trusts the line number reads the wrong code and forms a wrong
hypothesis, which is the most expensive failure mode available — it is not caught by
anything downstream. Every frame whose trace line falls outside the symbol's current
range is therefore flagged explicitly.

**Framework noise.** Real traces are mostly `System.*` / `Microsoft.*` frames. Marking
them as external lets the reader skip them instead of spending a lookup on each.

Nothing is inferred that the index cannot support: an unresolved frame is reported as
unresolved rather than guessed at.
"""

from __future__ import annotations

import re
from pathlib import Path

from .console import SYM
from .graphindex import Graph
from .mapper import disambiguate

# --- frame parsing ----------------------------------------------------------
# Deliberately liberal. A trace pasted from a log has been through a formatter, a
# ticket system and a terminal; anything unparseable is skipped rather than fatal.

# Python:  File "/app/svc.py", line 42, in save
_PY = re.compile(r'^\s*File\s+"(?P<path>.+?)",\s+line\s+(?P<line>\d+),\s+in\s+(?P<name>\S+)')

# JS/TS:   at save (/app/svc.js:42:10)   |   at /app/svc.js:42:10
# The path deliberately allows ':' so a Windows `E:\...` drive letter survives; what
# delimits it is the `:line:col` anchored at end-of-string, not the first colon.
_JS = re.compile(r"^\s*at\s+(?:(?P<name>[^\s(]+)\s+)?\(?"
                 r"(?P<path>[^\s()]+?):(?P<line>\d+):(?P<col>\d+)\)?\s*$")

# .NET:    at Ns.Class.Method(Args) in C:\src\File.cs:line 42
_NET = re.compile(r"^\s*at\s+(?P<qual>[^\s(]+)\s*\((?P<args>[^)]*)\)"
                  r"(?:\s+in\s+(?P<path>.+?):line\s+(?P<line>\d+))?\s*$")

# Compiler-generated wrappers that every async or lambda-bearing .NET trace is full of:
#   Shop.OrderService+<SaveAsync>d__12.MoveNext()          async state machine
#   Shop.OrderService.<>c__DisplayClass0_0.<Save>b__0()    lambda closure
# The user-written method name is inside the angle brackets; the wrapper name is noise.
_WRAPPER = re.compile(r"<(?P<inner>[A-Za-z_][A-Za-z0-9_]*)>[a-z]__[0-9_]+")
_GENERIC_ARITY = re.compile(r"`\d+")

_FRAMEWORK_PREFIXES = (
    "system.", "microsoft.", "mscorlib", "netstandard", "newtonsoft.", "serilog.",
    "nlog.", "log4net.", "automapper.", "mediatr.", "castle.", "nhibernate.",
    "entityframework", "dapper.", "polly.", "xunit.", "nunit.", "moq.",
    "internal/", "node:", "webpack", "react-dom", "next/dist",
)
_FRAMEWORK_PATH_HINTS = ("node_modules", "site-packages", "/usr/lib/", "\\lib\\site-packages")


def _clean_dotnet(qual: str) -> tuple[list[str], str]:
    """Reduce a .NET frame name to (owning segments, method name).

    Handles nested-type `+`, generic arity backticks, and the compiler-generated async
    and lambda wrappers, because a trace from any `async` code path is otherwise all
    `MoveNext` and resolves to nothing useful.
    """
    qual = _GENERIC_ARITY.sub("", qual).replace("+", ".")
    m = _WRAPPER.search(qual)
    if m:
        # Drop the wrapper segment and everything after it (`.MoveNext`, `.b__0`),
        # putting the real method name in its place.
        head = qual[:m.start()].rstrip(".")
        segs = [s for s in head.split(".") if s and not s.startswith("<")]
        return segs + [m.group("inner")], m.group("inner")
    segs = [s for s in qual.split(".") if s]
    return segs, (segs[-1] if segs else "")


def parse_frames(text: str) -> list[dict]:
    frames: list[dict] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        # Python first: its shape is unambiguous. JS before .NET, because a JS frame's
        # parenthesised `(path:line:col)` also satisfies .NET's `(args)` group.
        m = _PY.match(raw)
        if m:
            name = m.group("name")
            frames.append({"raw": raw.strip(), "segments": [name], "method": name,
                           "path": m.group("path"), "line": int(m.group("line"))})
            continue
        m = _JS.match(raw)
        if m:
            name = (m.group("name") or "").split(".")[-1]
            segs = [s for s in (m.group("name") or "").split(".") if s]
            frames.append({"raw": raw.strip(), "segments": segs, "method": name,
                           "path": m.group("path"), "line": int(m.group("line"))})
            continue
        m = _NET.match(raw)
        if m:
            segs, method = _clean_dotnet(m.group("qual"))
            frames.append({"raw": raw.strip(), "segments": segs, "method": method,
                           "path": m.group("path"),
                           "line": int(m.group("line")) if m.group("line") else None})
    return frames


def _is_framework(frame: dict) -> bool:
    dotted = ".".join(frame["segments"]).lower()
    if any(dotted.startswith(p) for p in _FRAMEWORK_PREFIXES):
        return True
    p = (frame["path"] or "").replace("\\", "/").lower()
    return any(h.replace("\\", "/") in p for h in _FRAMEWORK_PATH_HINTS)


def _score(sym: dict, frame: dict) -> int:
    """How well an indexed symbol explains a frame. Higher wins; the method name is
    already known to match, so this only breaks ties between same-named symbols.

    Namespaces are not captured by the C#/VB analyzers, so matching can only ever be a
    suffix comparison — a frame's leading `Shop.Api.` has nothing to match against.
    """
    score = 0
    owners = {s.lower() for s in frame["segments"][:-1]}
    if sym.get("container") and sym["container"].lower() in owners:
        score += 4  # class agrees: the strongest signal available
    if frame["path"]:
        if Path(frame["path"]).name.lower() == Path(sym["path"]).name.lower():
            score += 3  # same filename
        if frame["path"].replace("\\", "/").lower() == sym["path"].replace("\\", "/").lower():
            score += 2  # exact path, on top of the filename match
    if not sym.get("is_test"):
        score += 1
    if not sym.get("generated"):
        score += 1
    return score


def _range_of(sym: dict) -> tuple[int, int] | None:
    lo, hi = sym.get("line") or 0, sym.get("end_line") or 0
    return (lo, hi) if lo and hi >= lo else None


def resolve_stack(graph: Graph, text: str) -> dict:
    by_name: dict[str, list[dict]] = {}
    for s in graph.symbols:
        by_name.setdefault(s["name"].lower(), []).append(s)

    frames = parse_frames(text)
    out: list[dict] = []
    for i, f in enumerate(frames, 1):
        rec = {"n": i, "raw": f["raw"], "method": f["method"],
               "trace_path": f["path"], "trace_line": f["line"],
               "status": "unresolved", "path": None, "line": None,
               "container": None, "drift": False, "span": None}

        cands = by_name.get(f["method"].lower(), []) if f["method"] else []
        if cands:
            best = max(cands, key=lambda s: _score(s, f))
            span = _range_of(best)
            rec.update(status="project", path=best["path"], line=best["line"],
                       container=best.get("container") or None, span=span)
            # Drift: the trace's line is outside the symbol's current extent. The name
            # resolved, so the symbol is real — it is the trace's line that is stale.
            if f["line"] and span and not (span[0] <= f["line"] <= span[1]):
                rec["drift"] = True
        elif _is_framework(f):
            rec["status"] = "external"
        out.append(rec)

    project = [r for r in out if r["status"] == "project"]
    return {"frames": out, "total": len(out),
            "project": len(project),
            "external": sum(1 for r in out if r["status"] == "external"),
            "unresolved": sum(1 for r in out if r["status"] == "unresolved"),
            "drift": [r["n"] for r in out if r["drift"]],
            "start_here": project[0] if project else None}


# --- rendering --------------------------------------------------------------

def render_stack(r: dict, terse: bool = False) -> str:
    if not r["total"]:
        return ("_No stack frames recognised. Supported shapes: .NET `at Ns.Class.Method(...)"
                "[ in file:line N]`, JS `at name (file:line:col)`, Python `File \"f\", line N, in name`._\n")

    paths = [f["path"] for f in r["frames"] if f["path"]]
    lbl = disambiguate(paths) if paths else {}

    def loc(f):
        return f"{lbl.get(f['path'], f['path'])}:{f['line']}" if f["path"] else ""

    if terse:
        L = [f"stack — {r['total']} frames ({r['project']} project, "
             f"{r['external']} external, {r['unresolved']} unresolved)"]
        for f in r["frames"]:
            if f["status"] == "project":
                L.append(f"{f['n']} {f['method']} {loc(f)}" + ("  !drift" if f["drift"] else ""))
            else:
                L.append(f"{f['n']} {f['method']} ({f['status']})")
        return "\n".join(L) + "\n"

    L = [f"# Stack trace — {r['total']} frame(s): {r['project']} in project, "
         f"{r['external']} framework, {r['unresolved']} unresolved", ""]
    if r["start_here"]:
        s = r["start_here"]
        L += [f"**Start here:** frame {s['n']} — `{s['method']}` at {loc(s)}"
              " _(topmost frame that is your code)_", ""]

    for f in r["frames"]:
        if f["status"] == "project":
            owner = f"{f['container']}." if f["container"] else ""
            # SYM, not a literal glyph: agents capture stdout through a pipe, which on
            # Windows is cp1252 and cannot encode most symbols. init_streams() swaps in
            # ASCII fallbacks, but only for glyphs that go through SYM.
            flag = f"  {SYM['warn']} line drift" if f["drift"] else ""
            L.append(f"{f['n']:>3}. {owner}{f['method']} — {loc(f)}{flag}")
        elif f["status"] == "external":
            L.append(f"{f['n']:>3}. {f['method']} _(framework)_")
        else:
            L.append(f"{f['n']:>3}. {f['method']} _(not in index — check it is an "
                     f"indexed file type)_")
    L.append("")

    if r["drift"]:
        L += ["## Build drift", "",
              "These frames name a symbol that exists, but the trace's line number falls "
              "outside where that symbol now lives — the trace came from a different "
              "build. **Trust the symbol, not the trace's line.**", ""]
        for f in r["frames"]:
            if f["drift"]:
                L.append(f"- frame {f['n']}: trace says line {f['trace_line']}, "
                         f"`{f['method']}` now spans {f['span'][0]}-{f['span'][1]}")
        L.append("")

    L.append("_Frames are matched by name against the index; namespaces are not captured "
             "for C#/VB, so an overloaded or same-named method may resolve to a sibling. "
             "Every row carries file:line — one glance confirms it._")
    return "\n".join(L).rstrip() + "\n"

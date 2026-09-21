"""Repo-scale insights built on the persisted graph — the CLI equivalents of
codebase-memory-mcp's get_architecture / detect_changes / dead-code, in memo's
no-model, name-based-graph form (recall-biased; every result carries file:line).
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

from .console import SYM
from .graphindex import Graph
from .mapper import _collapse, _is_test, _matches, _short_name, common_root, disambiguate

_ENTRY_PAT = re.compile(r"(controller|consumer|processor|handler|endpoint|worker|program|startup)", re.I)
_LOGIC = ("method", "function")


# --------------------------------------------------------------------------- #
# arch — repository overview
# --------------------------------------------------------------------------- #

def arch(graph: Graph, entries) -> dict:
    langs = Counter(e.language for e in entries)
    prod_syms = [s for s in graph.symbols if not s["is_test"] and not s["generated"]]
    kinds = Counter(s["kind"] for s in prod_syms)

    root = common_root([s["path"] for s in graph.symbols])
    pkgs: Counter = Counter()
    for e in entries:
        rel = e.path.replace("\\", "/")
        if root and rel.startswith(root):
            rel = rel[len(root):].lstrip("/")
        seg = "/".join(rel.split("/")[:2]) if "/" in rel else rel
        pkgs[seg] += 1

    entry_syms = [s for s in prod_syms
                  if s["kind"] in _LOGIC and _ENTRY_PAT.search(Path(s["path"]).name)]

    # Hotspots by call-graph IN-DEGREE (how many production call sites target it),
    # methods only. This surfaces genuinely central logic (e.g. LogInfo, MappingPayload)
    # instead of ubiquitous DTO property names that raw identifier frequency inflates.
    # `tiered_indegree()` counts only sites with real evidence (see its own docstring
    # for the incident that made this matter: a raw count put a near-never-called
    # one-liner at the top of this exact ranking).
    indeg = graph.tiered_indegree()
    logic_syms: dict[str, dict] = {}
    for s in prod_syms:
        if s["kind"] in _LOGIC:
            logic_syms.setdefault(s["name"].lower(), s)  # representative prod definition
    hot = [(n, c) for n, c in indeg.items() if n in logic_syms]
    hot.sort(key=lambda x: -x[1])
    hotspots = []
    for name, cnt in hot[:15]:
        s = logic_syms[name]
        hotspots.append({"name": s["name"], "kind": s["kind"],
                         "path": s["path"], "line": s["line"], "callers": cnt})

    return {
        "root": root, "files": len(entries), "languages": dict(langs.most_common()),
        "symbol_kinds": dict(kinds.most_common()),
        "packages": pkgs.most_common(12),
        "entry_points": entry_syms, "hotspots": hotspots,
    }


def render_arch(a: dict, limit: int = 20, terse: bool = False) -> str:
    # `terse` was accepted here but never read — every other render_* in the codebase
    # branches on it, this one silently didn't, so `memo arch --terse` produced the
    # exact same output as `memo arch`. No test exercised render_arch at all, which is
    # how that shipped unnoticed. Flat, no markdown headers, `disambiguate()` labels —
    # matching the terse style every other command already uses.
    if terse:
        L = [f"arch {a['root']} (terse)",
             f"files={a['files']} langs=" + ",".join(f"{k}:{v}" for k, v in a["languages"].items()),
             "symbols=" + ",".join(f"{k}:{v}" for k, v in a["symbol_kinds"].items())]
        if a["packages"]:
            L.append("pkgs=" + " ".join(f"{seg}({n})" for seg, n in a["packages"][:limit]))
        if a["entry_points"]:
            lbl = disambiguate([s["path"] for s in a["entry_points"]])
            L.append("entry=" + " ".join(f"{s['name']}@{lbl[s['path']]}:{s['line']}"
                                         for s in a["entry_points"][:limit]))
        if a["hotspots"]:
            lbl = disambiguate([s["path"] for s in a["hotspots"]])
            L.append("hot=" + " ".join(f"{s['name']}({s['callers']})@{lbl[s['path']]}:{s['line']}"
                                       for s in a["hotspots"][:limit]))
        return "\n".join(L) + "\n"

    L = [f"# Architecture: {a['root']}", ""]
    L.append(f"Files: {a['files']}  |  languages: " +
             ", ".join(f"{k} {v}" for k, v in a["languages"].items()))
    L.append("Symbols: " + ", ".join(f"{v} {k}" for k, v in a["symbol_kinds"].items()))
    L.append("")
    L.append("## Top packages (by files)")
    for seg, n in a["packages"][:limit]:
        L.append(f"- {seg} ({n})")
    L.append("")
    if a["entry_points"]:
        L.append("## Entry points (controllers / consumers / processors)")
        lbl = disambiguate([s["path"] for s in a["entry_points"]])
        for s in a["entry_points"][:limit]:
            L.append(f"- {s['name']} `{s['kind']}` — {lbl[s['path']]}:{s['line']}")
        L.append("")
    if a["hotspots"]:
        L.append("## Hotspots (most-called methods — call-graph in-degree)")
        lbl = disambiguate([s["path"] for s in a["hotspots"]])
        for s in a["hotspots"][:limit]:
            L.append(f"- {s['name']} `{s['kind']}` ({s['callers']} callers) — {lbl[s['path']]}:{s['line']}")
        # This count now comes from `Graph.tiered_indegree()` — the same _resolve_tier
        # reasoning `callers`/`impact`/`query` already use, not a raw same-name match
        # (that was the original bug here: a generic short name like `get` collected
        # every dict.get()/config.get() call in the repo toward one arbitrarily-picked
        # definition, 229 "callers" for a function genuinely called once). Still
        # imperfect, honestly: it's recall-biased in the *conservative* direction now —
        # a real cross-file Python call can still read as zero evidence, because Python
        # arity is deliberately left unknown by the analyzer (param_arity's own
        # docstring: a string-derived Python signature drops defaults/kwargs and would
        # misclassify a real call as contradicted). Under-crediting a real caller is the
        # honest failure mode for a name-based tool; over-crediting 229 fake ones was not.
        if a["hotspots"]:
            L.append("\n_Counts require some resolution evidence (receiver type or arity), "
                     "not a raw name match — but a cross-file Python call can still miss "
                     "credit, since Python arity isn't parsed. Verify with `memo callers "
                     "<name> --exact` if a number looks surprising either way._")
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# impact — transitive callers (blast radius)
# --------------------------------------------------------------------------- #

def impact(graph: Graph, query: str, max_depth: int = 4, exact: bool = False) -> dict:
    qc = _collapse(query)
    seeds = {s["name"].lower() for s in graph.symbols
             if (_collapse(_short_name(s["name"])) == qc if exact else _matches(s["name"], qc))}
    affected: list[dict] = []
    seen_methods: set[tuple] = set()
    seen_names = set(seeds)
    frontier = set(seeds)
    for depth in range(1, max_depth + 1):
        nxt: set[str] = set()
        for cs in graph.call_sites:
            if cs["caller"] is None or cs["is_test"]:
                continue
            if cs["callee"].lower() in frontier:
                m = (cs["caller"].lower(), cs["caller_path"], cs["caller_line"])
                if m not in seen_methods:
                    seen_methods.add(m)
                    affected.append({"name": cs["caller"], "path": cs["caller_path"],
                                     "line": cs["caller_line"], "depth": depth})
                if cs["caller"].lower() not in seen_names:
                    nxt.add(cs["caller"].lower())
        seen_names |= nxt
        frontier = nxt
        if not frontier:
            break
    entry_hits = [a for a in affected if _ENTRY_PAT.search(Path(a["path"]).name)]
    return {"query": query, "seeds": sorted(seeds), "affected": affected,
            "risk": len(affected), "entry_points": entry_hits}


def _truncation_note(total: int, limit: int) -> list[str]:
    """Say so when rows were dropped, rather than letting the reader miscount.

    `impact` printed an honest total in its header and then silently listed only the
    first `limit` rows, so a 103-method blast radius rendered as 40 lines under a "103
    affected" heading — the reader cannot tell which number to believe, and an agent
    parsing rows simply under-reports. `deadcode` already reports its own cap; this makes
    the two consistent. A cap that isn't announced reads as "that's all there is".
    """
    hidden = total - limit
    return [f"_(+{hidden} more not shown — raise `--limit`)_"] if hidden > 0 else []


def render_impact(r: dict, limit: int = 40, terse: bool = False) -> str:
    if not r["seeds"]:
        return f'_No symbol matches "{r["query"]}"._\n'
    lbl = disambiguate([a["path"] for a in r["affected"]]) if r["affected"] else {}
    if terse:
        L = [f'impact "{r["query"]}" — {r["risk"]} affected (terse)']
        for a in r["affected"][:limit]:
            L.append(f"d{a['depth']} {a['name']} {lbl[a['path']]}:{a['line']}")
        L.extend(_truncation_note(len(r["affected"]), limit))
        if not r["affected"]:
            L.append("(none — no production callers)")
        return "\n".join(L) + "\n"
    L = [f'# Impact of "{r["query"]}" — {r["risk"]} production method(s) affected (transitive callers)', ""]
    by_depth: dict[int, list] = {}
    for a in r["affected"]:
        by_depth.setdefault(a["depth"], []).append(a)
    for d in sorted(by_depth):
        L.append(f"## Depth {d} ({len(by_depth[d])})")
        for a in by_depth[d][:limit]:
            L.append(f"- {a['name']} — {lbl[a['path']]}:{a['line']}")
        L.extend(_truncation_note(len(by_depth[d]), limit))
        L.append("")
    if r["entry_points"]:
        L.append("## Reaches entry points")
        for a in r["entry_points"]:
            L.append(f"- {a['name']} — {lbl[a['path']]}:{a['line']}")
    if not r["affected"]:
        L.append("_No production callers — nothing else depends on it (or it's an entry point / DI/reflection-invoked)._")
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# tests — the test methods that reach a symbol (minimal verification set)
# --------------------------------------------------------------------------- #

# Enough to build a runnable filter for the stacks memo targets. Anything else
# still gets the method list, just without a prebuilt command.
_RUNNER_BY_SUFFIX = {
    ".cs": "dotnet", ".vb": "dotnet",
    ".py": "pytest",
    ".ts": "jest", ".tsx": "jest", ".js": "jest", ".jsx": "jest",
}
_MAX_FILTER_NAMES = 12  # keep the generated command a usable length


def covering_tests(graph: Graph, query: str, max_depth: int = 4,
              exact: bool = False) -> dict:
    """Test methods that transitively reach `query`, nearest first.

    This is `impact` with the test filter inverted, but not *only* inverted, and the
    difference matters. `impact` skips test call sites outright because it reports
    production blast radius. Simply keeping them instead would find only tests that
    call the target directly, and most do not — the usual shape is
    `TestMethod -> ProductionService -> target`, or `TestMethod -> TestHelper ->
    target`. So the frontier expands through *every* caller while only test-file
    callers are harvested as results.

    Expanding through tests as well is safe: the walk goes upward, and essentially
    nothing calls a test method, so those branches terminate on their own.
    """
    qc = _collapse(query)
    seeds = {s["name"].lower() for s in graph.symbols
             if (_collapse(_short_name(s["name"])) == qc if exact else _matches(s["name"], qc))}

    found: list[dict] = []
    seen_tests: set[tuple] = set()
    seen_names = set(seeds)
    frontier = set(seeds)
    for depth in range(1, max_depth + 1):
        nxt: set[str] = set()
        for cs in graph.call_sites:
            if cs["caller"] is None or cs["callee"].lower() not in frontier:
                continue
            if cs["is_test"]:
                key = (cs["caller"].lower(), cs["caller_path"], cs["caller_line"])
                if key not in seen_tests:
                    seen_tests.add(key)
                    sym = graph.sym_by_pos.get((cs["caller_path"], cs["caller_line"])) or {}
                    found.append({
                        "name": cs["caller"],
                        "qualified": sym.get("qualified") or cs["caller"],
                        "container": sym.get("container") or "",
                        "path": cs["caller_path"],
                        "line": cs["caller_line"],
                        "depth": depth,
                        "via": cs["callee"],
                    })
            if cs["caller"].lower() not in seen_names:
                nxt.add(cs["caller"].lower())
        seen_names |= nxt
        frontier = nxt
        if not frontier:
            break

    return {"query": query, "seeds": sorted(seeds), "tests": found,
            "count": len(found), "command": _run_command(found)}


def _run_command(found: list[dict]) -> str:
    """A ready-to-run filter for the dominant framework among the hits.

    The list of names is the finding; the command is the deliverable. Handing back
    names alone leaves the caller to work out their runner's filter syntax, which is
    exactly the step that costs another round trip.
    """
    if not found:
        return ""
    runners = Counter(_RUNNER_BY_SUFFIX.get(Path(t["path"]).suffix.lower())
                      for t in found)
    runners.pop(None, None)
    if not runners:
        return ""
    runner = runners.most_common(1)[0][0]

    # Match on Container.Method where the container is known — a bare method name is
    # ambiguous across test classes. Preserve nearest-first order, then dedupe.
    names: list[str] = []
    for t in found:
        if _RUNNER_BY_SUFFIX.get(Path(t["path"]).suffix.lower()) != runner:
            continue
        n = f"{t['container']}.{t['name']}" if t["container"] else t["name"]
        if n not in names:
            names.append(n)
    if not names:
        return ""
    shown, extra = names[:_MAX_FILTER_NAMES], len(names) - _MAX_FILTER_NAMES

    if runner == "dotnet":
        cmd = 'dotnet test --filter "%s"' % "|".join(
            f"FullyQualifiedName~{n}" for n in shown)
    elif runner == "pytest":
        cmd = 'python -m pytest -k "%s"' % " or ".join(
            n.split(".")[-1] for n in shown)
    else:
        cmd = 'npx jest -t "%s"' % "|".join(n.split(".")[-1] for n in shown)
    return cmd + (f"   # +{extra} more not included" if extra > 0 else "")


_TESTS_CAVEAT = (
    "_Edges are name-based, so tests wired up by reflection, DI or a data-driven "
    "attribute may be missing. Treat this as the starting set, not proof of coverage._"
)


def render_tests(r: dict, limit: int = 40, terse: bool = False) -> str:
    if not r["seeds"]:
        # Distinct from "no tests found" on purpose: this one means the query is
        # wrong, and telling the two apart saves a pointless second lookup.
        return f'_No symbol matches "{r["query"]}" — check the name, or try --exact off._\n'
    if not r["tests"]:
        # A real answer, not an empty result. "This has no test reaching it" is a
        # high-value triage fact and should read like a finding.
        return (f'NONE — no test method reaches "{r["query"]}" within the searched '
                f"depth.\n\nThat is either a coverage gap or a symbol exercised only "
                f"through reflection/DI.\n" + ("" if terse else f"\n{_TESTS_CAVEAT}\n"))

    lbl = disambiguate([t["path"] for t in r["tests"]])
    if terse:
        L = [f'tests "{r["query"]}" — {r["count"]} (terse)']
        for t in r["tests"][:limit]:
            L.append(f"d{t['depth']} {t['name']} {lbl[t['path']]}:{t['line']}")
        L.extend(_truncation_note(len(r["tests"]), limit))
        if r["command"]:
            L.append(r["command"])
        return "\n".join(L) + "\n"

    L = [f'# Tests covering "{r["query"]}" — {r["count"]} test method(s)', ""]
    by_depth: dict[int, list] = {}
    for t in r["tests"]:
        by_depth.setdefault(t["depth"], []).append(t)
    for d in sorted(by_depth):
        label = "direct" if d == 1 else f"{d} hops out"
        L.append(f"## Depth {d} ({len(by_depth[d])}, {label})")
        for t in by_depth[d][:limit]:
            via = "" if d == 1 else f"  — via {t['via']}"
            L.append(f"- {t['name']} — {lbl[t['path']]}:{t['line']}{via}")
        L.extend(_truncation_note(len(by_depth[d]), limit))
        L.append("")
    if r["command"]:
        L += ["## Run just these", "```", r["command"], "```", ""]
    L.append(_TESTS_CAVEAT)
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# changed — git diff -> affected symbols (blast radius of a change)
# --------------------------------------------------------------------------- #

def changed(graph: Graph, ref: str, max_depth: int = 3) -> dict:
    root = common_root([s["path"] for s in graph.symbols])
    # `git diff --name-only` reports paths relative to the repository's actual top
    # level, not relative to `-C root` — and `root` here is only the common directory
    # of the *indexed* files, which is the repo top level purely by coincidence (it
    # isn't when the indexed tree is a subdirectory, e.g. only `src/` was cached, or
    # non-source files at the real top level like a `.sln` aren't on the extension
    # allowlist). Blindly prepending `root` to git's paths then builds an absolute path
    # that does not exist (`root/src/Foo.cs` when the real path was already
    # `root/Foo.cs`), so every touched symbol silently fails to match and `changed`
    # reports zero risk for a real change. Ask git for its own top level instead of
    # assuming `root` is it.
    top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if top.returncode != 0:
        return {"ref": ref, "error": (top.stderr or "not a git repository").strip(),
                "changed_files": []}
    git_root = top.stdout.strip().replace("\\", "/")
    r = subprocess.run(["git", "-C", root, "diff", "--name-only", ref],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return {"ref": ref, "error": (r.stderr or "git diff failed").strip(), "changed_files": []}
    changed_files = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    changed_abs = {(git_root + "/" + f).replace("\\", "/") for f in changed_files}

    # symbols defined in changed files
    touched = [s for s in graph.symbols
               if s["path"].replace("\\", "/") in changed_abs and not s["generated"]]
    results = []
    for s in touched:
        if s["kind"] not in _LOGIC:
            continue
        imp = impact(graph, s["name"], max_depth=max_depth)
        results.append({"symbol": s["name"], "path": s["path"], "line": s["line"],
                        "risk": imp["risk"]})
    results.sort(key=lambda x: -x["risk"])
    return {"ref": ref, "changed_files": changed_files, "touched_symbols": len(touched),
            "results": results}


def render_changed(r: dict, limit: int = 40, terse: bool = False) -> str:
    if r.get("error"):
        return f'_git error for ref "{r["ref"]}": {r["error"]}_\n'
    L = [f'# Changed vs "{r["ref"]}" — {len(r["changed_files"])} file(s), '
         f'{r["touched_symbols"]} symbol(s) touched', ""]
    if r["results"]:
        if not terse:
            L.append("## Blast radius (changed symbol -> production methods affected)")
        lbl = disambiguate([x["path"] for x in r["results"]])
        for x in r["results"][:limit]:
            flag = f"  {SYM['warn']} high" if x["risk"] >= 10 else ""
            L.append(f"- {x['symbol']} ({lbl[x['path']]}:{x['line']}) -> {x['risk']} affected{flag}")
    else:
        L.append("_No cached production symbols in the changed files._")
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# deadcode — production methods with zero callers
# --------------------------------------------------------------------------- #

def deadcode(graph: Graph, limit: int = 60) -> dict:
    called = {cs["callee"].lower() for cs in graph.call_sites}
    dead = []
    for s in graph.symbols:
        if s["is_test"] or s["generated"] or s["kind"] not in _LOGIC:
            continue
        if _ENTRY_PAT.search(Path(s["path"]).name):
            continue  # entry points are called externally
        if s["name"].lower() in called:
            continue
        dead.append(s)
    dead.sort(key=lambda s: (Path(s["path"]).name, s["line"]))
    return {"count": len(dead), "dead": dead[:limit], "shown": min(len(dead), limit)}


def render_deadcode(r: dict, terse: bool = False) -> str:
    L = [f"# Dead code candidates — {r['count']} production method(s) with no in-repo caller"]
    if not terse:
        L.append("")
        L.append("_Heuristic (name-based): may include interface/override implementations, "
                 "DI/reflection/serialization targets, and event handlers. Verify before deleting._")
    L.append("")
    lbl = disambiguate([s["path"] for s in r["dead"]])
    for s in r["dead"]:
        L.append(f"- {s['name']} `{s['kind']}` — {lbl[s['path']]}:{s['line']}")
    if r["count"] > r["shown"]:
        L.append(f"\n_…and {r['count'] - r['shown']} more._")
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# routes — HTTP endpoints as a first-class, indexed entity (ASP.NET attribute
# routing, Flask/Express-style decorators, Express app.get/post/...).
#
# Structural extraction, from `Graph.routes` (populated once at index time, see
# graphindex._scan_file), not a query-time regex scan — `memo routes` answers
# "what endpoints does this repo expose" straight from `.memo/`, same as every
# other command. This is the same regex `services.suggest_links` already used to
# find cross-repo link candidates; it moved to graphindex.py so both share one
# definition instead of two that could drift.
# --------------------------------------------------------------------------- #

def routes(graph: Graph, limit: int = 0) -> dict:
    rows = [r for r in graph.routes if not r["is_test"]]
    rows.sort(key=lambda r: r["pattern"])
    if limit:
        rows = rows[:limit]
    return {"routes": rows, "count": len(graph.routes),
           "shown": len(rows), "test_count": sum(1 for r in graph.routes if r["is_test"])}


def render_routes(r: dict, terse: bool = False) -> str:
    if terse:
        lines = [f"routes ({r['shown']} of {r['count']}, terse)"]
        lbl = disambiguate([x["path"] for x in r["routes"]])
        for x in r["routes"]:
            handler = x["handler"] or "(unattributed)"
            lines.append(f"{x['pattern']} -> {handler} {lbl[x['path']]}:{x['line']}")
        if not r["routes"]:
            lines.append("(none found)")
        return "\n".join(lines) + "\n"
    lines = [f"# Routes — {r['count']} endpoint(s) declared", ""]
    if not r["routes"]:
        lines.append("_No route declarations found (ASP.NET attribute routing, "
                     "Flask/Express-style decorators, or `app.get`/`app.post`/... — "
                     "other frameworks aren't recognized yet)._")
        return "\n".join(lines).rstrip() + "\n"
    lbl = disambiguate([x["path"] for x in r["routes"]])
    for x in r["routes"]:
        handler = x["handler"] or "(unattributed — module-level or unparsed enclosing scope)"
        lines.append(f"- `{x['pattern']}` -> {handler} — {lbl[x['path']]}:{x['line']}")
    if r["test_count"]:
        lines.append(f"\n_{r['test_count']} more route(s) in test files, not shown._")
    return "\n".join(lines).rstrip() + "\n"

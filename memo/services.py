"""Cross-repo/service tracing — `codebase-memory-mcp`'s `trace_path(mode=cross_service)`,
in memo's per-repo, no-database form.

memo indexes one repo at a time (`.memo/` lives in that repo), so a call chain that
crosses a process boundary — a frontend's `fetch("/api/orders")` landing in a separate
backend repo's `OrdersController` — is invisible to `memo trace` by construction: the
callee is text in a different codebase.

This closes that gap honestly rather than by guessing at HTTP-call heuristics that
would silently misroute as often as they helped: a **global registry** of repos
(`~/.memo/registry.json`, outside any one repo, since a cross-repo fact belongs to
neither side alone) records where sibling repos live, and a small set of **declared
links** name the boundary explicitly — `OrdersController.Create` in this repo *is*
`SaveOrder` in `orders-service`. `memo trace --cross-service` then walks the normal
in-repo call graph and, at any node matching a link, keeps walking into the linked
repo's own graph.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .cache import Cache
from .config import find_cache_root
from .graphindex import _ROUTE_DECL, _enclosing
from .mapper import _collapse, _matches, _read_text

REGISTRY_DIR = Path.home() / ".memo"
REGISTRY_FILE = REGISTRY_DIR / "registry.json"


def _load_registry() -> dict:
    if not REGISTRY_FILE.is_file():
        return {"repos": {}, "links": []}
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"repos": {}, "links": []}
    data.setdefault("repos", {})
    data.setdefault("links", [])
    return data


def _save_registry(data: dict) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def register(path: Path, name: str | None = None) -> str:
    path = path.resolve()
    name = name or path.name
    data = _load_registry()
    data["repos"][name] = str(path)
    _save_registry(data)
    return name


def unregister(name: str) -> bool:
    data = _load_registry()
    if name not in data["repos"]:
        return False
    del data["repos"][name]
    _save_registry(data)
    return True


def list_repos() -> dict:
    return _load_registry()["repos"]


def _name_for_path(path: Path) -> str | None:
    """The registry name whose path matches `path`, if any."""
    path = str(path.resolve())
    for name, p in list_repos().items():
        if str(Path(p).resolve()) == path:
            return name
    return None


def current_repo_name(repo_root: Path) -> str:
    """The registry name for `repo_root`, auto-registering it under its directory
    name if it isn't there yet — cross-service tracing needs *a* name for "here"
    even if the user never ran `memo register` on this side of the link."""
    existing = _name_for_path(repo_root)
    if existing:
        return existing
    return register(repo_root)


def add_link(from_symbol: str, from_repo: str, to_symbol: str, to_repo: str,
             kind: str = "manual") -> dict:
    data = _load_registry()
    link = {"from": from_symbol, "from_repo": from_repo, "to": to_symbol,
            "to_repo": to_repo, "kind": kind}
    data["links"].append(link)
    _save_registry(data)
    return link


def list_links() -> list[dict]:
    return _load_registry()["links"]


def remove_link(index: int) -> bool:
    data = _load_registry()
    if 0 <= index < len(data["links"]):
        data["links"].pop(index)
        _save_registry(data)
        return True
    return False


class _RepoNotFound(Exception):
    pass


def _open_repo(name: str):
    from . import graphindex
    repos = list_repos()
    if name not in repos:
        raise _RepoNotFound(name)
    root = find_cache_root(Path(repos[name]))
    c = Cache(root)
    return c, graphindex.load_or_build(c)


def cross_trace(root_name: str, root_graph, query: str, direction: str, depth: int) -> dict:
    """Same shape as `graph.trace()`, extended to hop repos at declared links.

    Kept separate from `graph.trace` rather than adding a repo parameter to it: the
    single-repo path is memo's common case and must stay free of this machinery, and
    every existing caller of `trace()`/`find_callers`/`find_calls` keeps working
    against exactly the graph it already had.
    """
    from .graph import find_callers, find_calls

    links = list_links()
    graphs: dict[str, object] = {root_name: root_graph}
    errors: list[str] = []

    def get_graph(name: str):
        if name in graphs:
            return graphs[name]
        try:
            _c, g = _open_repo(name)
        except _RepoNotFound:
            errors.append(f'repo "{name}" is not registered (`memo register <path> --name {name}`)')
            graphs[name] = None
            return None
        graphs[name] = g
        return g

    def local_children(g, name: str) -> list[dict]:
        if direction == "up":
            r = find_callers(g, name)
            return [{"name": c["caller"], "path": c["path"], "line": c["caller_line"]}
                    for c in r["callers"] if c["caller"] != "(file scope)" and not c["is_test"]]
        r = find_calls(g, name)
        seen, out = set(), []
        for d in r["definitions"]:
            for c in d["callees"]:
                if c["name"] not in seen:
                    seen.add(c["name"])
                    out.append({"name": c["name"], "path": c["path"], "line": c["line"]})
        return out

    def cross_children(repo_name: str, name: str) -> list[tuple[str, str]]:
        qc = _collapse(name)
        out = []
        for l in links:
            if direction == "down" and l["from_repo"] == repo_name and _matches(l["from"], qc):
                out.append((l["to_repo"], l["to"]))
            elif direction == "up" and l["to_repo"] == repo_name and _matches(l["to"], qc):
                out.append((l["from_repo"], l["from"]))
        return out

    def walk(repo_name: str, name: str, level: int, path_seen: set) -> dict:
        node = {"name": name, "repo": repo_name, "children": []}
        key = (repo_name, name)
        if level >= depth or key in path_seen:
            return node
        g = get_graph(repo_name)
        if g is None:
            return node
        for ch in local_children(g, name):
            child = walk(repo_name, ch["name"], level + 1, path_seen | {key})
            child["path"], child["line"] = ch["path"], ch["line"]
            node["children"].append(child)
        for to_repo, to_name in cross_children(repo_name, name):
            child = walk(to_repo, to_name, level + 1, path_seen | {key})
            child["cross"] = True
            node["children"].append(child)
        return node

    tree = walk(root_name, query, 0, set())
    return {"query": query, "direction": direction, "depth": depth, "repo": root_name,
            "tree": tree, "errors": errors}


def render_cross_trace(result: dict, terse: bool = False, limit: int = 0) -> str:
    arrow = "up" if result["direction"] == "up" else "down"
    if terse:
        lines = [f'trace {arrow} "{result["query"]}" [{result["repo"]}] (terse, cross-service)']
    else:
        a = "callers ↑" if result["direction"] == "up" else "calls ↓"
        lines = [f'# Cross-service trace ({a}, depth {result["depth"]}): '
                 f'"{result["query"]}" in {result["repo"]}',
                 "_(candidate edges, name-based; `⇢ repo` marks a declared cross-repo link)_", ""]

    def emit(node: dict, indent: int, base_repo: str):
        loc = f"  {Path(node['path']).name}:{node.get('line', 0)}" if node.get("path") else ""
        cross = f"  ⇢ {node['repo']}" if node.get("cross") else ""
        lines.append("  " * indent + f"- {node['name']}{loc}{cross}")
        kids = node["children"][:limit] if limit else node["children"]
        for ch in kids:
            emit(ch, indent + 1, node["repo"])
        if limit and len(node["children"]) > limit:
            lines.append("  " * (indent + 1) + f"- …(+{len(node['children']) - limit} more)")

    emit(result["tree"], 0, result["repo"])
    if not result["tree"]["children"]:
        rl = "callers" if result["direction"] == "up" else "callees"
        lines.append(f"\n_No {rl} found._")
    if result["errors"]:
        lines.append("")
        for e in result["errors"]:
            lines.append(f"! {e}")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# suggest_links — propose (never apply) cross-service links from HTTP-shaped code.
#
# Deliberately a *suggestion* tool, not `link --auto`: guessing HTTP-call boundaries
# from string literals is exactly the kind of heuristic that misfires often enough to
# be worse than no answer at all, which is why `memo link` itself stays fully manual.
# This exists only to remove the tedium of *finding* candidates by hand — every result
# still requires a human (or an agent explicitly asked to) to run `memo link` to
# confirm it. Path-token overlap is a weak signal on purpose: it is a starting point
# for a person to look at two lines of code, not a fact to be trusted uncritically.
# --------------------------------------------------------------------------- #

_OUTBOUND_CALL = re.compile(
    r'(?:\bfetch|axios\s*\.\s*\w+|requests\s*\.\s*\w+|'
    r'\.\w*(?:Get|Post|Put|Delete|Patch)Async)\s*\(\s*[\'"`]([^\'"`]+)[\'"`]'
)
_STOP_SEGMENTS = frozenset(("api", "v1", "v2", "v3", "index", ""))


def _path_tokens(path: str) -> set:
    path = path.split("?", 1)[0]
    out = set()
    for seg in path.strip("/").split("/"):
        s = seg.lower()
        if s in _STOP_SEGMENTS or s.startswith(("{", ":", "<")) or s.isdigit():
            continue
        out.add(s)
    return out


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _outbound_calls(graph) -> list[dict]:
    calls = []
    by_path: dict[str, list[dict]] = {}
    for s in graph.symbols:
        by_path.setdefault(s["path"], []).append(s)
    for path, syms in by_path.items():
        p = Path(path)
        if not p.is_file():
            continue
        text = _read_text(p)
        sorted_syms = sorted(syms, key=lambda s: s["line"])
        for m in _OUTBOUND_CALL.finditer(text):
            toks = _path_tokens(m.group(1))
            if not toks:
                continue
            line = _line_of(text, m.start())
            encl = _enclosing(sorted_syms, line)
            if not encl:
                continue
            calls.append({"caller": encl["qualified"], "path": path, "line": line,
                         "url": m.group(1), "tokens": toks})
    return calls


def _route_defs(graph) -> list[dict]:
    routes = []
    by_path: dict[str, list[dict]] = {}
    for s in graph.symbols:
        by_path.setdefault(s["path"], []).append(s)
    for path, syms in by_path.items():
        p = Path(path)
        if not p.is_file():
            continue
        text = _read_text(p)
        sorted_syms = sorted(syms, key=lambda s: s["line"])
        for m in _ROUTE_DECL.finditer(text):
            route = next(g for g in m.groups() if g)
            toks = _path_tokens(route)
            if not toks:
                continue
            line = _line_of(text, m.start())
            encl = _enclosing(sorted_syms, line)
            routes.append({"route": route, "path": path, "line": line,
                          "target": encl["qualified"] if encl else None, "tokens": toks})
    return routes


def suggest_links(root_name: str, root_graph) -> dict:
    """Candidate links from this repo's outbound HTTP-shaped calls to route-shaped
    declarations in other registered repos, ranked by path-segment overlap."""
    calls = _outbound_calls(root_graph)
    candidates = []
    for other_name, other_path in list_repos().items():
        if other_name == root_name or not calls:
            continue
        try:
            _c, other_g = _open_repo(other_name)
        except _RepoNotFound:
            continue
        for r in _route_defs(other_g):
            if r["target"] is None:
                continue
            for call in calls:
                overlap = call["tokens"] & r["tokens"]
                if overlap:
                    candidates.append({
                        "from": call["caller"], "from_path": call["path"],
                        "from_line": call["line"], "from_url": call["url"],
                        "to_repo": other_name, "to": r["target"],
                        "to_path": r["path"], "to_line": r["line"], "to_route": r["route"],
                        "evidence": sorted(overlap),
                    })
    candidates.sort(key=lambda c: -len(c["evidence"]))
    return {"repo": root_name, "count": len(candidates), "candidates": candidates}


def render_suggestions(result: dict, limit: int = 20, terse: bool = False) -> str:
    cands = result["candidates"][:limit]
    if terse:
        L = [f"suggest-links {result['repo']} ({result['count']}, terse)"]
        for c in cands:
            L.append(f"{c['from']} {Path(c['from_path']).name}:{c['from_line']} -> "
                     f"{c['to_repo']}:{c['to']} {Path(c['to_path']).name}:{c['to_line']} "
                     f"[{','.join(c['evidence'])}]")
        if not cands:
            L.append("(none)")
        return "\n".join(L) + "\n"
    L = [f"# Suggested cross-service links from {result['repo']} — {result['count']} candidate(s)",
         "", "_Heuristic (path-segment overlap between an outbound HTTP call and a route "
         "declaration). Verify before trusting — confirm with `memo link`, which is the "
         "only thing that actually creates a link._", ""]
    for c in cands:
        L.append(f"- {c['from']} ({Path(c['from_path']).name}:{c['from_line']}, "
                 f"calls `{c['from_url']}`)")
        L.append(f"  -> {c['to_repo']}:{c['to']} ({Path(c['to_path']).name}:{c['to_line']}, "
                 f"route `{c['to_route']}`)  — matched on: {', '.join(c['evidence'])}")
        L.append(f"  confirm: memo link \"{c['from']}\" {c['to_repo']} \"{c['to']}\"")
    if not cands:
        L.append("_No candidates — register the repos it calls into "
                 "(`memo register`) and re-run, or link manually with `memo link`._")
    return "\n".join(L).rstrip() + "\n"


def render_registry(repos: dict, links: list[dict]) -> str:
    lines = [f"# memo service registry ({REGISTRY_FILE})", "", f"## Repos ({len(repos)})"]
    for name, path in sorted(repos.items()):
        lines.append(f"- {name} — {path}")
    lines += ["", f"## Links ({len(links)})"]
    for i, l in enumerate(links):
        lines.append(f"- [{i}] {l['from_repo']}:{l['from']} -> {l['to_repo']}:{l['to']}")
    if not links:
        lines.append("_None yet — add one with `memo link <from-symbol> <to-repo> <to-symbol>`._")
    return "\n".join(lines).rstrip() + "\n"

"""Leiden community detection over the call graph — the de-facto module boundaries,
which often cut across the folder layout (codebase-memory-mcp's `get_architecture`
does the same thing; this is memo's equivalent, in memo's optional-dependency,
graceful-degradation style rather than requiring it).

Optional by design, same pattern as `tiktoken` in tokens.py: `python-igraph` is a
compiled C library, and an optional feature must never be able to block `pip install
memo-cache`. Without it, every caller here gets `None` back and falls further back to
directory-based grouping (`arch`'s own two-level package split), which is what the UI
graph already did before this module existed — nothing regresses, a capability is
added on top.

Deliberately file-level, not symbol-level. Symbol-level nodes would number in the
thousands even on memo's own small repo, and the call graph is name-based and
recall-biased (README: "Honest limits") — a single spurious same-name edge between two
unrelated classes would misplace one symbol, but at file granularity, one spurious edge
is diluted among all the real edges between the same two files, so the clustering is
far less sensitive to exactly the noise memo already admits its graph has.
"""

from __future__ import annotations

from pathlib import Path

from .graphindex import Graph
from .mapper import common_root


def _try_igraph():
    try:
        import igraph
        return igraph
    except ImportError:
        return None


def available() -> bool:
    return _try_igraph() is not None


def _file_edges(graph: Graph) -> tuple[list[str], dict[tuple[str, str], int]]:
    """Weighted file-to-file call edges: how many call sites in file A target a
    symbol defined in file B. Self-edges (A calls A) are dropped — they say nothing
    about which *other* files a file is coupled to, which is what clustering needs.
    """
    files = sorted({s["path"] for s in graph.symbols if not s["generated"]})
    weights: dict[tuple[str, str], int] = {}
    for cs in graph.call_sites:
        if cs["caller"] is None or cs["is_test"]:
            continue
        for s in graph.sym_by_name.get(cs["callee"].lower(), ()):
            if s["path"] == cs["caller_path"]:
                continue
            pair = tuple(sorted((cs["caller_path"], s["path"])))
            weights[pair] = weights.get(pair, 0) + 1
            break  # one target per call site, same convention as elsewhere in the graph
    return files, weights


def compute(graph: Graph) -> dict | None:
    """Cluster this graph's files by Leiden community detection over the (weighted,
    symmetrized) call graph. Returns None if `python-igraph` isn't installed — check
    `available()` first, or just handle None, which every caller here does.
    """
    igraph = _try_igraph()
    if igraph is None:
        return None

    files, weights = _file_edges(graph)
    if len(files) < 2 or not weights:
        return None  # nothing to cluster; a single node/no edges isn't a partition

    idx = {f: i for i, f in enumerate(files)}
    edges = [(idx[a], idx[b]) for a, b in weights]
    g = igraph.Graph(n=len(files), edges=edges)
    g.es["weight"] = list(weights.values())
    result = g.community_leiden(objective_function="modularity",
                                weights="weight", n_iterations=4)

    root = common_root(files)
    clusters: dict[int, dict] = {}
    for file_idx, cluster_id in enumerate(result.membership):
        c = clusters.setdefault(cluster_id, {"id": cluster_id, "files": []})
        c["files"].append(files[file_idx])

    # In-cluster call-weight per file, to name a cluster after the file its own
    # members depend on most — not just whichever file sorts first alphabetically.
    # Mirrors `arch`'s own hotspot selection (rank by in-degree), applied within one
    # cluster instead of across the whole repo.
    in_cluster_weight: dict[str, int] = {}
    for (a, b), w in weights.items():
        if result.membership[idx[a]] == result.membership[idx[b]]:
            in_cluster_weight[a] = in_cluster_weight.get(a, 0) + w
            in_cluster_weight[b] = in_cluster_weight.get(b, 0) + w

    for c in clusters.values():
        c["files"].sort()
        c["size"] = len(c["files"])
        dir_label = common_root(c["files"]).replace("\\", "/")
        if root and dir_label.startswith(root):
            dir_label = dir_label[len(root):].lstrip("/")
        if dir_label:
            c["label"] = dir_label  # a real shared subdirectory beats any single file
        else:
            rep = max(c["files"], key=lambda f: in_cluster_weight.get(f, 0))
            c["label"] = Path(rep).name

    return {
        "root": root,
        "modularity": result.modularity,
        "clusters": sorted(clusters.values(), key=lambda c: -c["size"]),
        "file_to_cluster": {files[i]: m for i, m in enumerate(result.membership)},
    }


def render(result: dict | None, terse: bool = False) -> str:
    if result is None:
        return ("_Leiden clustering unavailable — install `python-igraph` "
               "(`pip install -e \".[graph]\"`) or fall back to `memo arch`'s "
               "directory-based package grouping._\n")

    # A file that shares no call-graph edge with any other file forms its own
    # size-1 "cluster" — technically correct, but a repo where every test file only
    # calls production code (never each other) produces dozens of these, all sharing
    # one label since it's derived from the single file's own directory. Twenty
    # identical "## tests — 1 file(s)" headings bury the two or three genuinely
    # multi-file clusters that are the actual finding here. Split them: real clusters
    # get shown in full, isolated files get one honest summary line instead of a
    # heading each. `--json` still returns every cluster, singleton or not.
    grouped = [c for c in result["clusters"] if c["size"] > 1]
    isolated = [c for c in result["clusters"] if c["size"] == 1]

    if terse:
        lines = [f"clusters (terse) — modularity={result['modularity']:.2f}"]
        for c in grouped:
            lines.append(f"[{c['id']}] {c['label']} ({c['size']} files)")
        if isolated:
            lines.append(f"isolated({len(isolated)}): " +
                         ", ".join(Path(c["files"][0]).name for c in isolated[:15]))
        return "\n".join(lines) + "\n"

    lines = [f"# Call-graph clusters — modularity {result['modularity']:.2f}", "",
            "_De-facto module boundaries from Leiden community detection over the "
            "call graph — these can cut across the folder layout; that's the point._",
            ""]
    for c in grouped:
        lines.append(f"## {c['label']} — {c['size']} file(s)")
        for f in c["files"][:12]:
            lines.append(f"- {Path(f).name}")
        if c["size"] > 12:
            lines.append(f"- _…and {c['size'] - 12} more._")
        lines.append("")
    if isolated:
        lines.append(f"_{len(isolated)} file(s) share no call-graph edge with any "
                     "other file (isolated in the graph — often standalone tests or "
                     "utilities): " +
                     ", ".join(Path(c["files"][0]).name for c in isolated[:15]) +
                     ("…" if len(isolated) > 15 else "") + "_")
    if not grouped:
        lines.append("_No multi-file clusters found — every file is isolated in the "
                     "call graph, or the graph is too small/sparse to partition._")
    return "\n".join(lines).rstrip() + "\n"

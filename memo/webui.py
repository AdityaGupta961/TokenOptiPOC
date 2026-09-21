"""`memo ui` — a local, read-only dashboard over this repo's index.

Binds to 127.0.0.1 only and never makes an outbound network call: memo's whole claim is
"local, offline, no LLM API, no keys" (README), and a dashboard that phones home or
needs a CDN to render its own page would quietly break that promise the first time a
user is on an offline or CDN-blocking network. So the page is a single self-contained
HTML/CSS/JS string with no external `<script src>` and no fetch to anywhere but this
same server — vendoring a real graph-drawing library was deliberately skipped in favor
of ~150 lines of hand-rolled canvas force-layout, because that's a small enough amount
of code to own outright rather than a dependency to audit for "does this call home."

Everything served here is read *from* the existing `.memo/` index — this module writes
nothing, and adds no new persisted state. It is a view, not a fifth cache format.
"""

from __future__ import annotations

import json
import re
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, adr as adr_mod, clusters, graphindex, services
from .cache import Cache
from .config import find_cache_root
from .insights import arch as build_arch
from .mapper import _is_test, _read_text, common_root
from .tokens import count_tokens

# --------------------------------------------------------------------------- #
# JSON payload builders — thin wrappers around functions the CLI already calls,
# so the dashboard can never disagree with `memo arch`/`memo show`/`memo status`
# about what the index actually contains.
# --------------------------------------------------------------------------- #


def _status_payload(root: Path, cache: Cache, graph) -> dict:
    entries = cache.list_entries()
    index = cache.index_snapshot()
    repo_name = next((n for n, p in services.list_repos().items()
                      if Path(p).resolve() == root.parent.resolve()), None)
    return {
        "repo_root": str(root.parent),
        "memo_version": __version__,
        "has_index": bool(entries),
        "indexed_files": len(index),
        "graph_symbols": len(graph.symbols) if graph else 0,
        "graph_edges": len(graph.call_sites) if graph else 0,
        "adr_count": len(adr_mod.list_all(root)),
        "registered_as": repo_name,
        "tokens_exact": count_tokens("x")[1],
    }


def _files_payload(cache: Cache) -> dict:
    entries = cache.list_entries()
    rows = []
    for e in entries:
        ratio = (e.raw_tokens / e.summary_tokens) if e.summary_tokens else 0
        rows.append({
            "path": e.path, "name": Path(e.path).name, "language": e.language,
            "raw_tokens": e.raw_tokens, "summary_tokens": e.summary_tokens,
            "ratio": round(ratio, 2), "is_test": _is_test(e.path),
        })
    rows.sort(key=lambda r: -r["raw_tokens"])
    return {"files": rows, "count": len(rows)}


def _file_detail_payload(cache: Cache, path: str) -> dict:
    """Pre/post-index view for one file: raw source next to memo's summary.

    Matches by suffix, the same tolerant lookup `peek --at` uses, so a path copied
    from the file list (already relative-ish for display) still resolves.
    """
    entries = cache.list_entries()
    norm = path.replace("\\", "/")
    candidates = [e for e in entries if e.path.replace("\\", "/").endswith(norm)]
    if not candidates:
        return {"error": f'no cached file matches "{path}"'}
    e = min(candidates, key=lambda c: len(c.path))
    p = Path(e.path)
    if not p.is_file():
        return {"error": f"{e.path} no longer exists on disk"}
    return {
        "path": e.path, "name": p.name, "language": e.language,
        "raw_text": _read_text(p), "summary_text": e.summary_text,
        "raw_tokens": e.raw_tokens, "summary_tokens": e.summary_tokens,
        "ratio": round((e.raw_tokens / e.summary_tokens) if e.summary_tokens else 0, 2),
    }


def _arch_payload(graph, entries) -> dict:
    return build_arch(graph, entries)


# Leiden clustering (~86ms on memo's own 57-file repo, and it scales with graph size —
# see clusters.py) is by far the most expensive part of building a graph payload
# (everything else here is ~3ms), and `/api/graph` recomputes on every request with no
# caching otherwise. Keyed on the graph's own fingerprint — the same content-hash digest
# graphindex.py already computes to detect a stale index — so a cache entry is reused
# for exactly as long as the underlying index hasn't changed, and is invalidated for
# free the moment it has, with no separate freshness check to get wrong. Capped rather
# than unbounded: one `memo ui` process serves one repo, so in practice this holds at
# most a handful of entries (one per re-index during the session) even over a long-
# running server, but nothing stops a future caller from calling this across many repos.
_CLUSTER_CACHE: dict[str, dict | None] = {}
_CLUSTER_CACHE_MAX = 8


def _cached_clusters(graph):
    key = graph.fingerprint  # "" on a pre-fingerprint graph: degrades to no caching,
    if key:                  # not to a wrong answer — see graphindex.fingerprint_of.
        cached = _CLUSTER_CACHE.get(key)
        if cached is not None or key in _CLUSTER_CACHE:
            return cached
    result = clusters.compute(graph)
    if key:
        if len(_CLUSTER_CACHE) >= _CLUSTER_CACHE_MAX:
            _CLUSTER_CACHE.clear()  # simple eviction: this is a rare-write cache, not a hot one
        _CLUSTER_CACHE[key] = result
    return result


def _graph_payload(graph, limit: int) -> dict:
    """A drawable subgraph: capped to the top `limit` symbols by in-repo call-graph
    in-degree, plus edges between exactly those nodes.

    The full graph (2,000+ edges on memo's own 59-file repo) is not a useful picture —
    it is a hairball, and rendering it wastes both browser cycles and the reader's
    attention on library glue nobody came to look at. Capping to the most-called
    symbols first, matching `arch`'s own hotspot selection, means the graph opens on
    the part of the codebase actually worth seeing.
    """
    # tiered_indegree() only counts call sites with real evidence (receiver/arity),
    # not a raw callee-name match — see its docstring for why a raw count is exactly
    # wrong for common short names, which is what this graph's node sizing depends on.
    indeg = graph.tiered_indegree()

    prod_syms = [s for s in graph.symbols if not s["is_test"] and not s["generated"]
                 and s.get("kind") in ("method", "function", "constructor")]
    root = common_root([s["path"] for s in prod_syms]) if prod_syms else ""

    # Real Leiden clusters when `python-igraph` is installed — the de-facto module
    # boundaries from actual call coupling, which is a more honest "why are these two
    # things the same color" than a folder name. Falls back to the directory-based
    # grouping below when clusters.compute() returns None (not installed, or too small
    # a graph to partition), so the graph view never breaks for lack of an optional dep.
    cluster_result = _cached_clusters(graph)
    file_to_cluster = cluster_result["file_to_cluster"] if cluster_result else {}

    def package_of(path: str) -> str:
        """Grouping key for the graph's node color.

        Prefers a real Leiden cluster id when available. Falls back to `arch`'s own
        top-packages grouping (first two path segments under the common root) so the
        legend can never disagree with `memo arch`'s package table when clustering
        isn't available.

        A one-level directory split looked right in isolation but broke on this exact
        repo: one stray non-source file elsewhere (a C# test fixture in `samples/`)
        pulls the *common* root up a level, so every real file under `memo/` then
        shares one first segment ("memo") and the whole graph renders as a single
        color. Two levels survives that: `memo/cli.py` and `memo/analyzers` stay
        visually distinct even when a root one level higher gets picked.
        """
        if path in file_to_cluster:
            return f"cluster-{file_to_cluster[path]}"
        rel = path.replace("\\", "/")
        if root and rel.startswith(root):
            rel = rel[len(root):].lstrip("/")
        return "/".join(rel.split("/")[:2]) if "/" in rel else rel

    seen: dict[tuple, dict] = {}
    for s in prod_syms:
        key = (s["path"], s["line"])
        if key in seen:
            continue
        seen[key] = {
            "id": f"{s['path']}:{s['line']}", "name": s["name"], "kind": s["kind"],
            "path": s["path"], "line": s["line"],
            "package": package_of(s["path"]),
            "callers": indeg.get(s["name"].lower(), 0),
        }
    ranked = sorted(seen.values(), key=lambda n: -n["callers"])[:limit]
    node_ids = {n["id"] for n in ranked}
    by_pos = {(n["path"], n["line"]): n["id"] for n in ranked}

    edges = []
    edge_seen: set[tuple] = set()
    for cs in graph.call_sites:
        if cs["caller"] is None or cs["is_test"]:
            continue
        src = by_pos.get((cs["caller_path"], cs["caller_line"]))
        if src is None or src not in node_ids:
            continue
        callee_l = cs["callee"].lower()
        for s in graph.sym_by_name.get(callee_l, ()):
            dst = f"{s['path']}:{s['line']}"
            if dst in node_ids and dst != src:
                pair = (src, dst)
                if pair not in edge_seen:
                    edge_seen.add(pair)
                    edges.append({"source": src, "target": dst})
                break  # one edge per call site is enough for a picture, not a proof

    return {"nodes": ranked, "edges": edges, "total_symbols": len(seen),
            "clustered": cluster_result is not None,
            "modularity": cluster_result["modularity"] if cluster_result else None}


# --------------------------------------------------------------------------- #
# HTTP layer — stdlib only, threaded so one slow request can't block another tab.
# --------------------------------------------------------------------------- #

_ROUTES = {
    "/api/status", "/api/files", "/api/file", "/api/arch", "/api/graph",
}


def _make_handler(root: Path):
    class Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler logs every request to stderr by default, which is
        # noise for a dashboard someone leaves open in a tab. Silence it.
        def log_message(self, *args) -> None:
            pass

        def _send_json(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's naming)
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            if parsed.path == "/" or parsed.path == "/index.html":
                self._send_html(PAGE)
                return
            if parsed.path not in _ROUTES:
                self._send_json({"error": "not found"}, status=404)
                return

            cache = Cache(root)
            entries = cache.list_entries()
            graph = graphindex.load_or_build(cache) if entries else None

            if parsed.path == "/api/status":
                self._send_json(_status_payload(root, cache, graph))
            elif parsed.path == "/api/files":
                self._send_json(_files_payload(cache) if entries else
                                {"files": [], "count": 0})
            elif parsed.path == "/api/file":
                paths = qs.get("path")
                if not paths:
                    self._send_json({"error": "missing ?path="}, status=400)
                else:
                    self._send_json(_file_detail_payload(cache, paths[0]))
            elif parsed.path == "/api/arch":
                self._send_json(_arch_payload(graph, entries) if graph else
                                {"error": "no index yet"})
            elif parsed.path == "/api/graph":
                limit = int(qs.get("limit", ["60"])[0])
                self._send_json(_graph_payload(graph, limit) if graph else
                                {"nodes": [], "edges": [], "total_symbols": 0})

    return Handler


def _free_port(preferred: int) -> int:
    """Use `preferred` if free, else let the OS pick one — never fail to start
    a purely local dashboard just because a stray previous instance is still
    holding the default port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def serve(root: Path, port: int, open_browser: bool = True) -> None:
    """Start the dashboard and block until interrupted. `root` is the `.memo/` dir."""
    actual_port = _free_port(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", actual_port), _make_handler(root))
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"memo ui: {url}  (Ctrl+C to stop; bound to localhost only)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------- #
# The page. One file, no build step, no CDN — see module docstring for why.
# --------------------------------------------------------------------------- #

PAGE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>memo</title>
<style>
  :root {
    --bg: #0f1117; --panel: #171923; --border: #2a2e3a; --text: #e6e8ee;
    --muted: #8b93a7; --accent: #6ea8fe; --warn: #e2b93b; --good: #4caf7d;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  header { display: flex; align-items: center; gap: 16px; padding: 10px 16px;
           border-bottom: 1px solid var(--border); background: var(--panel); }
  header h1 { font-size: 14px; margin: 0; font-weight: 600; }
  header .repo { color: var(--muted); overflow: hidden; text-overflow: ellipsis;
                 white-space: nowrap; }
  header .badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; }
  .badge.good { background: rgba(76,175,125,.15); color: var(--good); }
  .badge.warn { background: rgba(226,185,59,.15); color: var(--warn); }
  nav { display: flex; gap: 4px; padding: 8px 16px; border-bottom: 1px solid var(--border); }
  nav button { background: none; border: 1px solid transparent; color: var(--muted);
               padding: 6px 12px; border-radius: 6px; cursor: pointer; font: inherit; }
  nav button.active { background: var(--panel); color: var(--text); border-color: var(--border); }
  main { padding: 16px; }
  .tab { display: none; }
  .tab.active { display: block; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap: 10px; margin-bottom: 16px; }
  .stat { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
  .stat .n { font-size: 20px; font-weight: 700; }
  .stat .l { color: var(--muted); font-size: 11px; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; }
  tr:hover td { background: rgba(255,255,255,.02); cursor: pointer; }
  .split { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .pane { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
          padding: 10px; max-height: 70vh; overflow: auto; }
  .pane h3 { margin: 0 0 8px; font-size: 12px; color: var(--muted); }
  pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 12px; }
  input[type=text] { background: var(--panel); border: 1px solid var(--border); color: var(--text);
                      padding: 6px 10px; border-radius: 6px; width: 100%; font: inherit; margin-bottom: 10px; }
  .muted { color: var(--muted); }
  .caveat { background: rgba(226,185,59,.08); border: 1px solid rgba(226,185,59,.3);
            border-radius: 6px; padding: 8px 10px; margin: 10px 0; color: var(--warn); font-size: 12px; }
  #graphCanvas { background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
                 display: block; width: 100%; height: 70vh; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; font-size: 11px; color: var(--muted); }
  .legend span { display: inline-flex; align-items: center; gap: 5px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
</style>
</head>
<body>
<header>
  <h1>memo</h1>
  <div class="repo" id="repoPath">loading…</div>
  <div id="freshBadge"></div>
</header>
<nav>
  <button data-tab="overview" class="active">Overview</button>
  <button data-tab="files">Index Explorer</button>
  <button data-tab="graph">Graph</button>
</nav>
<main>
  <section class="tab active" id="tab-overview">
    <div class="grid" id="overviewStats"></div>
    <div id="overviewLangs" class="muted"></div>
    <h3 style="margin-top:20px;">Hotspots (most-called methods)</h3>
    <div class="caveat">Counts require resolution evidence (receiver type or arity), not
      a raw name match — but a cross-file Python call can still miss credit, since
      Python arity isn't parsed. Verify with <code>memo callers &lt;name&gt; --exact</code>
      if a number looks surprising either way.</div>
    <table id="hotspotTable"><thead><tr><th>Name</th><th>Kind</th><th>Callers</th><th>Location</th></tr></thead><tbody></tbody></table>
  </section>

  <section class="tab" id="tab-files">
    <input type="text" id="fileSearch" placeholder="Filter files…">
    <div class="split">
      <div class="pane">
        <h3>Cached files</h3>
        <table id="fileTable"><thead><tr><th>File</th><th>Raw</th><th>Summary</th><th>Ratio</th></tr></thead><tbody></tbody></table>
      </div>
      <div class="pane" id="fileDetail"><span class="muted">Select a file to see raw source vs. memo's summary, side by side.</span></div>
    </div>
  </section>

  <section class="tab" id="tab-graph">
    <div class="muted" style="margin-bottom:8px;" id="graphSubtitle">Top symbols by call-graph in-degree. Drag to rearrange, click a node for details.</div>
    <canvas id="graphCanvas"></canvas>
    <div class="legend" id="graphLegend"></div>
  </section>
</main>

<script>
const $ = (s, el) => (el||document).querySelector(s);
const $$ = (s, el) => Array.from((el||document).querySelectorAll(s));

document.querySelectorAll('nav button').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
    $('#tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'graph') drawGraph();
  };
});

async function j(url) { const r = await fetch(url); return r.json(); }

async function loadStatus() {
  const s = await j('/api/status');
  $('#repoPath').textContent = s.repo_root || '(no repo)';
  const badge = $('#freshBadge');
  if (!s.has_index) {
    badge.innerHTML = '<span class="badge warn">no index — run: memo cache-dir . --recursive</span>';
  } else {
    badge.innerHTML = `<span class="badge good">${s.indexed_files} files · ${s.graph_symbols} symbols · ${s.graph_edges} edges</span>`;
  }
  return s;
}

async function loadOverview() {
  const a = await j('/api/arch');
  if (a.error) { $('#overviewStats').innerHTML = `<div class="muted">${a.error}. Run <code>memo cache-dir . --recursive</code> first.</div>`; return; }
  const stats = [
    ['Files', a.files], ['Languages', Object.keys(a.languages).length],
    ['Packages', a.packages.length], ['Entry points', a.entry_points.length],
  ];
  $('#overviewStats').innerHTML = stats.map(([l,n]) => `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
  $('#overviewLangs').textContent = 'Languages: ' + Object.entries(a.languages).map(([k,v]) => `${k} (${v})`).join(', ');
  $('#hotspotTable tbody').innerHTML = a.hotspots.map(h =>
    `<tr><td>${h.name}</td><td>${h.kind}</td><td>${h.callers}</td><td class="muted">${h.path.split('/').pop()}:${h.line}</td></tr>`
  ).join('');
}

let allFiles = [];
async function loadFiles() {
  const f = await j('/api/files');
  allFiles = f.files;
  renderFileTable(allFiles);
}
function renderFileTable(files) {
  $('#fileTable tbody').innerHTML = files.map(f =>
    `<tr data-path="${encodeURIComponent(f.path)}"><td>${f.name}</td><td>${f.raw_tokens}</td><td>${f.summary_tokens}</td><td>${f.ratio}x</td></tr>`
  ).join('');
  $$('#fileTable tbody tr').forEach(tr => tr.onclick = () => showFile(decodeURIComponent(tr.dataset.path)));
}
$('#fileSearch').oninput = (e) => {
  const q = e.target.value.toLowerCase();
  renderFileTable(allFiles.filter(f => f.name.toLowerCase().includes(q) || f.path.toLowerCase().includes(q)));
};
async function showFile(path) {
  const d = await j('/api/file?path=' + encodeURIComponent(path));
  if (d.error) { $('#fileDetail').innerHTML = `<span class="muted">${d.error}</span>`; return; }
  $('#fileDetail').innerHTML = `
    <h3>${d.name} — ${d.raw_tokens} → ${d.summary_tokens} tokens (${d.ratio}x smaller)</h3>
    <div class="split">
      <div><div class="muted" style="margin-bottom:4px;">Raw source</div><pre>${escapeHtml(d.raw_text.slice(0, 6000))}</pre></div>
      <div><div class="muted" style="margin-bottom:4px;">memo summary</div><pre>${escapeHtml(d.summary_text)}</pre></div>
    </div>`;
}
function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

let graphLoaded = false;
async function drawGraph() {
  if (graphLoaded) return;
  graphLoaded = true;
  const g = await j('/api/graph?limit=60');
  const canvas = $('#graphCanvas');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr; canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;

  const packages = [...new Set(g.nodes.map(n => n.package))];
  const palette = ['#6ea8fe','#e2b93b','#4caf7d','#e2708f','#9d7bea','#5bc0de','#f0a35a','#8b93a7'];
  const colorOf = p => palette[packages.indexOf(p) % palette.length];
  $('#graphLegend').innerHTML = packages.map(p => `<span><span class="dot" style="background:${colorOf(p)}"></span>${p}</span>`).join('');
  $('#graphSubtitle').textContent = g.clustered
    ? `Top symbols by call-graph in-degree, colored by Leiden cluster (modularity ${g.modularity.toFixed(2)}) — de-facto module boundaries from real call coupling, not folder names. Drag to rearrange, click a node for details.`
    : 'Top symbols by call-graph in-degree, colored by directory (install python-igraph for real clustering — pip install -e ".[graph]"). Drag to rearrange, click a node for details.';

  const nodes = g.nodes.map((n, i) => ({
    ...n, x: W/2 + Math.cos(i) * 100, y: H/2 + Math.sin(i) * 100, vx: 0, vy: 0,
  }));
  const idx = Object.fromEntries(nodes.map((n, i) => [n.id, i]));
  const edges = g.edges.map(e => [idx[e.source], idx[e.target]]).filter(([a,b]) => a !== undefined && b !== undefined);

  function tick() {
    for (const n of nodes) { n.fx = 0; n.fy = 0; }
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i+1; j < nodes.length; j++) {
        let dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        let d2 = Math.max(dx*dx + dy*dy, 25); let f = 800 / d2;
        let d = Math.sqrt(d2);
        nodes[i].fx += (dx/d)*f; nodes[i].fy += (dy/d)*f;
        nodes[j].fx -= (dx/d)*f; nodes[j].fy -= (dy/d)*f;
      }
    }
    for (const [a,b] of edges) {
      let dx = nodes[b].x - nodes[a].x, dy = nodes[b].y - nodes[a].y;
      let d = Math.sqrt(dx*dx+dy*dy) || 1; let f = (d - 60) * 0.02;
      nodes[a].fx += (dx/d)*f; nodes[a].fy += (dy/d)*f;
      nodes[b].fx -= (dx/d)*f; nodes[b].fy -= (dy/d)*f;
    }
    for (const n of nodes) {
      if (n.dragging) continue;
      n.vx = (n.vx + n.fx) * 0.85; n.vy = (n.vy + n.fy) * 0.85;
      n.x += n.vx; n.y += n.vy;
      n.x = Math.max(10, Math.min(W-10, n.x)); n.y = Math.max(10, Math.min(H-10, n.y));
    }
  }

  function render() {
    ctx.clearRect(0,0,W,H);
    ctx.strokeStyle = 'rgba(255,255,255,.08)';
    for (const [a,b] of edges) { ctx.beginPath(); ctx.moveTo(nodes[a].x, nodes[a].y); ctx.lineTo(nodes[b].x, nodes[b].y); ctx.stroke(); }
    for (const n of nodes) {
      const r = 4 + Math.min(10, Math.sqrt(n.callers));
      ctx.fillStyle = colorOf(n.package);
      ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 7); ctx.fill();
    }
  }

  let frame = 0;
  function loop() { tick(); render(); if (frame++ < 400) requestAnimationFrame(loop); }
  loop();

  let dragTarget = null;
  canvas.onmousedown = (e) => {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    dragTarget = nodes.find(n => Math.hypot(n.x-mx, n.y-my) < 10);
    if (dragTarget) dragTarget.dragging = true;
  };
  canvas.onmousemove = (e) => {
    if (!dragTarget) return;
    const r = canvas.getBoundingClientRect();
    dragTarget.x = e.clientX - r.left; dragTarget.y = e.clientY - r.top;
    render();
  };
  window.addEventListener('mouseup', () => { if (dragTarget) dragTarget.dragging = false; dragTarget = null; });
  canvas.onclick = (e) => {
    const r = canvas.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const hit = nodes.find(n => Math.hypot(n.x-mx, n.y-my) < 10);
    if (hit) alert(`${hit.name} (${hit.kind})\n${hit.path}:${hit.line}\ncallers: ${hit.callers}`);
  };
}

(async () => {
  await loadStatus();
  await loadOverview();
  await loadFiles();
})();
</script>
</body>
</html>
"""

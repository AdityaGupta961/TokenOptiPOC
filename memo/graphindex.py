"""Persistent code graph — the 'index once, query fast' idea (borrowed from
codebase-memory-mcp's SQLite knowledge graph, adapted to memo's flat-file, no-model,
pure-Python constraints).

At cache time we scan every cached file ONCE and record:
  - symbols   : the symbol table (name, kind, path, line, is_test, generated)
  - call_sites: caller-method -> callee-name edges with file:line (name-based,
                recall-biased — same tradeoff as memo's live call graph)
  - freq      : global identifier frequency (a centrality proxy)

This is persisted to `.memo/graph.json`, so `callers`/`calls`/`trace`/`arch`/
`impact`/`deadcode` become lookups instead of re-scanning the repo per query.
Freshness follows memo's model: the graph reflects the last `cache-dir`.
"""

from __future__ import annotations

import json
import re
import hashlib
import sys
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .analyzers.base import (
    UNKNOWN_ARITY,
    arity_fits,
    base_types,
    declared_types,
    line_at,
    line_of,
    line_starts,
    param_arity,
    scan_arg_counts,
)
from .mapper import _collapse, _is_test, _matches, _read_text, _short_name

_CALL = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")

# HTTP route/endpoint declarations — ASP.NET attribute routing, Flask/Express-style
# decorators, and Express-style `app.get(...)`. Lives here (not in services.py, the
# only caller until now) because it's genuinely shared: `_scan_file` below records
# routes as a first-class per-file candidate list for `memo routes`/`Graph.routes`,
# and services.py's `suggest_links` already matched on the same pattern for
# cross-service link candidates — one definition instead of two that could drift.
_ROUTE_DECL = re.compile(
    r'\[Http\w+\(\s*["\']([^"\']+)["\']|\[Route\(\s*["\']([^"\']+)["\']|'
    r'@\w*\.(?:route|get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']|'
    r'\bapp\.\w+\(\s*["\']([^"\']+)["\']'
)
_GENERATED = ("/connected services/", "reference.cs", ".g.cs", ".designer.")
GRAPH_FILE = "graph.json"

# How a call site named its target. Only IDENT and SELF carry information about *which*
# same-named definition is meant; EXPR and BARE deliberately carry none, and scoring must
# not invent any. See `_classify_receiver`.
RECV_BARE = 0  # `Foo(` — nothing in front of it
RECV_EXPR = 1  # `a[0].Foo(`, `x().Foo(`, `T::Foo(` — receiver is an expression
RECV_IDENT = 2  # `svc.Foo(`, `Mapper.Foo(` — a single named receiver
RECV_SELF = 3  # `this.Foo(`, `Me.Foo(`, `self.foo(`
RECV_NEW = 4  # `new Foo(` — a constructor

_SELF_WORDS = frozenset(("this", "me", "mybase", "myclass", "self", "cls", "mymodule"))
_CONTAINER_KINDS = frozenset(
    ("class", "module", "struct", "record", "interface", "component"))

# Only "property" today — currently the sole field-like kind any analyzer emits (both
# C# and VB.NET; see graphindex's field-access section below). Python/JS/markup files
# declare no separate field symbols, so `known_fields` (built from this set) is simply
# empty for them and field-access tracking is a correct, silent no-op there — not a
# gap, since there's nothing declared to track accesses *of* in those languages today.
_FIELD_KINDS = frozenset(("property",))

# How much a name-based edge is worth once receiver and arity are taken into account.
# These order results and pick between same-named definitions; they never remove an edge.
TIER_CONTRA = -1  # argument count cannot reach this definition
TIER_WEAK = 0  # no evidence either way
TIER_OK = 1  # arity fits, nothing contradicts
TIER_STRONG = 2  # same file with a fitting arity, or a container match
TIER_EXACT = 3  # the receiver names this definition's container
# Not a static inference at all: this edge was observed actually happening, via
# `memo ingest-traces`. Outranks every static signal, including a declared-type match,
# because it is not a heuristic — it is what reflection, DI and dynamic dispatch hide
# from every other tier. See `merge_runtime_edges`.
TIER_VERIFIED = 4
TIER_NAMES = {TIER_CONTRA: "contra", TIER_WEAK: "weak", TIER_OK: "ok",
              TIER_STRONG: "strong", TIER_EXACT: "exact", TIER_VERIFIED: "verified"}


def _satisfies(recv_type: str, container: str, implementors: dict | None) -> bool:
    """Whether a receiver declared `recv_type` can be dispatching to `container`.

    Either the same type, or `container` names `recv_type` among its bases — the
    interface-to-implementation step, which matters because an interface's declaration has
    no body to read. Resolving `_processor.HandleException(m)` to `ICaseProcessor` is
    technically right and practically useless; `CaseProcessor` is the answer wanted.

    One level only. A deeper hierarchy is rarer than the cost of walking it wrongly, and
    walking it wrongly means pointing at an unrelated class.
    """
    if not recv_type or not container:
        return False
    if recv_type.lower() == container.lower():
        return True
    return container in (implementors or {}).get(recv_type.lower(), ())


def _try_igraph():
    """Same optional-dependency pattern as `clusters.py`'s own copy of this: a
    compiled C library must never be able to block `pip install memo-cache`, so every
    caller here gets `None` back and falls further back to a pure-Python computation."""
    try:
        import igraph
        return igraph
    except ImportError:
        return None


def _power_iteration_pagerank(edges: list[tuple[str, str]], nodes: set[str],
                              damping: float, max_iter: int, tol: float) -> dict[str, float]:
    """Hand-rolled PageRank — used only when `python-igraph` isn't installed.
    Standard formulation: a dangling node (no outgoing edges) redistributes its mass
    uniformly across every node each iteration, so rank isn't lost to sinks.
    """
    n = len(nodes)
    if n == 0:
        return {}
    node_list = sorted(nodes)
    out_edges: dict[str, list[str]] = {node: [] for node in node_list}
    for a, b in edges:
        out_edges[a].append(b)
    out_degree = {node: len(out_edges[node]) for node in node_list}

    rank = {node: 1.0 / n for node in node_list}
    for _ in range(max_iter):
        dangling_mass = sum(rank[node] for node in node_list if out_degree[node] == 0)
        base = (1.0 - damping) / n + damping * dangling_mass / n
        new_rank = {node: base for node in node_list}
        for a, b in edges:
            if out_degree[a]:
                new_rank[b] += damping * rank[a] / out_degree[a]
        delta = sum(abs(new_rank[node] - rank[node]) for node in node_list)
        rank = new_rank
        if delta < tol:
            break
    return rank


def _resolve_tier(cs: dict, sym: dict, caller_container: str = "",
                  type_names: frozenset = frozenset(),
                  implementors: dict | None = None) -> int:
    """How well call site `cs` matches definition `sym` — a TIER_* value.

    A *declared* receiver type is the only signal that separates same-name, same-arity
    methods on different types, which is what most real C# ambiguity is: on a 350-file
    service, 90% of ambiguous callees were `Foo.Save`/`IFoo.Save` pairs that arity and
    receiver names cannot touch. A type mismatch is deliberately *not* treated as a
    contradiction — the method may be declared on a base class, and memo tracks only one
    level of hierarchy.

    Three further rules keep this from over-claiming, all of them learned from real code:

    RECV_EXPR and RECV_BARE earn no container credit. A fluent chain, an indexer and a
    plain call all sit in one of those two states, so crediting them would promote a
    local method on the strength of `items.Where(..).Select(Map)` having a dot in it.

    A named receiver counts as naming a *type* only when it really is one — it has to
    appear in `type_names`, matched case-sensitively. Otherwise `a.Run(1)` scores against
    class `A`, and `order.Save()` against class `Order`, purely because a variable was
    named after its type. That convention is usually right and occasionally very wrong,
    which makes it a bad thing to report as certainty. A static `Mapper.Map(` still
    resolves, because `Mapper` is a type.

    A fitting argument count alone caps at TIER_OK. VB's `arr(0) = 5` is indistinguishable
    from a one-argument call to `arr`, so arity agreement on its own is not enough to
    outrank a container match — it needs the receiver to agree too.
    """
    if cs.get("verified"):
        return TIER_VERIFIED  # runtime evidence, not a heuristic — see merge_runtime_edges
    container = sym.get("container") or ""
    recv_kind = cs.get("recv_kind", RECV_BARE)
    fits = arity_fits(cs.get("argc", UNKNOWN_ARITY), sym.get("pmin", UNKNOWN_ARITY),
                      sym.get("pmax", UNKNOWN_ARITY), sym.get("pflags", 0))

    if container and fits is not False:
        recv = cs.get("recv") or ""
        # Strongest available signal: the receiver's *declared* type. This is the only
        # thing that separates same-name same-arity methods on different types, which is
        # what the bulk of real C# ambiguity is.
        if _satisfies(cs.get("recv_type") or "", container, implementors):
            return TIER_EXACT
        if recv_kind == RECV_IDENT and recv == container and recv in type_names:
            return TIER_EXACT  # `Mapper.Map(` against `Map` inside class `Mapper`
        if recv_kind == RECV_SELF and caller_container \
                and container.lower() == caller_container.lower():
            return TIER_EXACT

    if fits is False:
        return TIER_CONTRA
    if fits and cs.get("caller_path") == sym.get("path"):
        return TIER_STRONG
    if container and caller_container and recv_kind == RECV_SELF \
            and container.lower() == caller_container.lower():
        return TIER_STRONG
    if fits:
        return TIER_OK
    return TIER_WEAK


def _uniquely_narrowed(defs: list[dict], tiers: dict[int, int]) -> bool:
    """True when contradiction alone leaves exactly one candidate standing.

    Arity's discriminating power is *relative*, not absolute. Five methods sharing a name
    all score TIER_OK on their own, but if the argument count rules out four of them the
    survivor is the answer — and no per-candidate tier can say so, because each is judged
    in isolation. Without this, the commonest way arity actually helps went unused: on a
    350-file C# service it recovered 82 of 1,729 ambiguous sites that promotion missed.

    Requires something to have been ruled out. One surviving candidate out of one is not
    evidence, it is just a unique name.
    """
    survivors = [s for s in defs if tiers[id(s)] != TIER_CONTRA]
    if len(survivors) == len(defs):
        return False
    return len({(s["path"], s["line"]) for s in survivors}) == 1


def _line_of(text: str, index: int) -> int:
    return line_of(text, index)


def _is_generated(path: str) -> bool:
    p = path.replace("\\", "/").lower()
    return any(x in p for x in _GENERATED)


def _sorted_symbols(entry) -> list[dict]:
    syms = [s for s in entry.structured.get("symbols", []) if s.get("line")]
    return sorted(syms, key=lambda s: s["line"])


def _enclosing(symbols: list[dict], line: int) -> dict | None:
    """The innermost symbol containing `line` (symbols must be sorted by line).

    Where an analyzer supplied `end_line`, a symbol that provably closed before `line`
    is skipped — otherwise a call sitting after the last method of a class was
    attributed to that method rather than to the class.

    Binary search, then walk back over any symbols that already closed. This is called
    once per call-site candidate — 174,674 times on a 1,433-file repo, where the former
    linear scan over each file's symbol list was the single largest cost in indexing
    (7.9s of 39s). Result is identical; only the search is cheaper.
    """
    # Rightmost symbol whose declaration is at or above `line`.
    i = bisect_right(_LineKeys(symbols), line) - 1
    while i >= 0:
        s = symbols[i]
        end = s.get("end_line") or 0
        if not end or line <= end:
            return s
        i -= 1
    return None


def _next_symbol_after(symbols: list[dict], line: int, max_gap: int = 5) -> dict | None:
    """The nearest symbol declared AT OR AFTER `line`, within `max_gap` lines — for
    attributing a decorator/attribute (`[HttpGet]`, `@app.route`) to the method it
    decorates, which `_enclosing` structurally cannot do: a decorator always precedes
    its target, and `_enclosing` only ever looks backward for a declaration at or
    before the query line. Caught live: without this, a route attribute on the line
    directly above its handler method attributed to the *class* (C#) or to nothing at
    all (Python, where the decorator has no enclosing class to fall back to).

    `max_gap` keeps this from reaching past an unrelated method several lines down when
    the "decorator" line turns out to be something else the route regex matched loosely.
    """
    i = bisect_right(_LineKeys(symbols), line - 1)
    if i < len(symbols) and symbols[i]["line"] - line <= max_gap:
        return symbols[i]
    return None


def _classify_receiver(text: str, start: int) -> tuple[str | None, int]:
    """What, if anything, the call at `start` was invoked on: `(receiver, RECV_*)`.

    Read backwards from the callee rather than with a second regex, so the set of
    discovered calls cannot drift from `_CALL`'s.

    The distinction that matters is between a *named* receiver and an expression one.
    `items.Where(x => ...).Select(Map).ToList()` puts `Select` and `ToList` directly after
    a `.`, exactly as `a[0].Run(` and `x().Go()` do, and none of them name a type. Reading
    those as bare calls — which is what a naive "is there a dot?" test does — would hand
    every fluent chain and every indexer the same-container bonus that belongs only to a
    genuine `this.Foo()`. So they get RECV_EXPR, which scores nothing.
    """
    j = start - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return None, RECV_BARE
    if text[j] == ":":  # `T::Static(` — a qualified expression, not a named receiver
        return None, RECV_EXPR
    if text[j] != ".":
        end = j + 1
        while j >= 0 and (text[j].isalnum() or text[j] in "_$"):
            j -= 1
        word = text[j + 1:end]
        # `new Foo(` reads as prev-char `w`, so the keyword has to be matched explicitly.
        return (None, RECV_NEW) if word.lower() == "new" else (None, RECV_BARE)

    j -= 1  # step over the `.`
    while j >= 0 and text[j] in " \t\r\n!?":  # `obj?.Go()`, `obj!.Go()`
        j -= 1
    if j < 0 or not (text[j].isalnum() or text[j] in "_$"):
        return None, RECV_EXPR  # `)`, `]`, `>` — an expression receiver
    end = j + 1
    while j >= 0 and (text[j].isalnum() or text[j] in "_$"):
        j -= 1
    recv = text[j + 1:end]
    if recv[0].isdigit():  # `1.ToString(` — a literal, not a name
        return None, RECV_EXPR
    if recv.lower() in _SELF_WORDS:
        return recv, RECV_SELF
    return recv, RECV_IDENT


def _containers_by_line(syms: list[dict]) -> dict[int, str]:
    """Map each symbol's declaration line to the name of its innermost container.

    Two sources, in order of reliability. Where an analyzer already qualifies a name —
    Python always (`FileSummary.to_dict`), TypeScript for class members (`UserCard.render`)
    — the container is read off the name. C# and VB emit bare names, so theirs comes from
    the `end_line` spans the analyzers already compute for `peek`.

    A container whose extent is unknown (`end_line == 0`) is not pushed: guessing that it
    encloses everything below it would attribute half a file to the wrong class. An empty
    container is the honest answer and simply scores nothing downstream.

    Deliberately derived here rather than added to `Symbol`, so no analyzer changes and no
    cache-format change: this reads only fields already present on cached entries.
    """
    out: dict[int, str] = {}
    stack: list[tuple[int, str]] = []  # (end_line, name)
    for s in syms:
        line = s["line"]
        while stack and stack[-1][0] < line:
            stack.pop()
        qualified = s["name"]
        parts = re.split(r"[.#]", qualified)
        if len(parts) > 1:
            out[line] = parts[-2]
        elif stack:
            out[line] = stack[-1][1]
        end = s.get("end_line") or 0
        if s.get("kind") in _CONTAINER_KINDS and end >= line:
            stack.append((end, _short_name(qualified)))
    return out


class _LineKeys:
    """Read-only view of `symbols` as their `line` values, for bisect without copying.

    Building a key list per call would cost more than the linear scan it replaces.
    """

    __slots__ = ("_s",)

    def __init__(self, symbols: list[dict]):
        self._s = symbols

    def __len__(self) -> int:
        return len(self._s)

    def __getitem__(self, i: int) -> int:
        return self._s[i]["line"]


def _body_range(symbols: list[dict], idx: int, total_lines: int) -> tuple[int, int]:
    """The 1-based inclusive line span of `symbols[idx]`.

    Prefers the analyzer's real `end_line` (brace/End-statement matched, or exact from
    Python's AST). The old fallback — "up to the next symbol's line" — collapses any
    container to a single line, because a class is immediately followed by its own
    first method. It is kept only for symbols whose extent we genuinely don't know.
    """
    s = symbols[idx]
    start = s["line"]
    end = s.get("end_line") or 0
    if end >= start:
        return start, min(end, total_lines)
    nxt = symbols[idx + 1]["line"] - 1 if idx + 1 < len(symbols) else total_lines
    return start, max(start, nxt)


def fingerprint_of(entries) -> str:
    """Stable digest of exactly what a graph was built from.

    `files=len(entries)` was the only staleness signal, and it cannot see any edit that
    leaves the count unchanged — which is the common case, not the corner case: editing
    a file in place, or re-running `memo cache` on one that is already indexed. The
    graph then keeps the old symbol lines and nothing warns.

    That silence is expensive rather than merely untidy. The generated rule files tell
    agents to cite memo's `file:line` *without* re-reading to confirm, so a stale line
    is not caught downstream — the agent either re-reads (cancelling the whole saving)
    or edits the wrong place and has to redo the work.

    Hashing the (path, content_hash) pairs detects it. Both `build` and the CLI guard
    already hold the entry list, so this costs no extra file IO on the query path.

    Measured cost on the query path: 0.05 ms at 48 entries, 6.3 ms at 3,360 (memo's
    stated target scale), against a 528 ms end-to-end command whose interpreter floor is
    alone 215 ms — so ~1%. It could be precomputed into the manifest, which is
    invalidated on any index change and would make this O(1); deliberately not done,
    because that adds a second invalidation path inside the code whose entire job is
    detecting invalidation. Revisit only if the entry count grows an order of magnitude.
    """
    h = hashlib.sha256()
    for path, content_hash in sorted((e.path, e.content_hash) for e in entries):
        h.update(path.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
        h.update(content_hash.encode("ascii"))
        h.update(b"\n")
    # 16 hex chars is ample for detecting "not the same set of files" and keeps
    # graph.json small; this is a change detector, not a security boundary.
    return h.hexdigest()[:16]


@dataclass
class Graph:
    symbols: list[dict]
    call_sites: list[dict]
    freq: dict
    files: int = 0  # number of files indexed when built (staleness check)
    # Digest of the (path, content_hash) set this graph was built from. Empty when the
    # graph predates the field, in which case callers must fall back to `files` rather
    # than treat "" as a mismatch. See fingerprint_of.
    fingerprint: str = ""
    # Field/property reads and writes — a distinct edge type from call_sites, because
    # "who reads/writes this shared field" is a different question from "who calls this
    # method" and call-graph edges (which all require `name(`) structurally can't answer
    # it. Empty for a graph built before this existed, or for a repo whose analyzers
    # emit no field-like symbols (Python/JS/markup today — see _FIELD_KINDS).
    field_accesses: list[dict] = field(default_factory=list)
    # HTTP route/endpoint declarations, first-class instead of the query-time-only
    # regex scan `services.suggest_links` used before this existed — `memo routes`
    # answers "what endpoints does this repo expose" from the index, no live re-scan.
    routes: list[dict] = field(default_factory=list)
    sym_by_name: dict = field(default_factory=dict)
    sym_by_pos: dict = field(default_factory=dict)
    type_names: frozenset = field(default_factory=frozenset)
    implementors: dict = field(default_factory=dict)
    _indegree_cache: dict | None = field(default=None, repr=False, compare=False)
    _pagerank_cache: dict | None = field(default=None, repr=False, compare=False)

    def index(self) -> "Graph":
        self.sym_by_name = {}
        self.sym_by_pos = {}
        types: set[str] = set()
        # base type (lowercased) -> the concrete types declaring it. Lets a receiver
        # declared as `ICaseProcessor` reach `CaseProcessor.HandleException` rather than
        # stopping at the interface's own declaration, which has no body to read.
        self.implementors = {}
        for s in self.symbols:
            self.sym_by_name.setdefault(s["name"].lower(), []).append(s)
            self.sym_by_pos[(s["path"], s["line"])] = s
            if s.get("kind") in _CONTAINER_KINDS:
                types.add(s["name"])  # case-sensitive: see `_resolve_tier`
                for b in (s.get("bases") or "").split(","):
                    if b:
                        self.implementors.setdefault(b.lower(), set()).add(s["name"])
        self.type_names = frozenset(types)
        return self

    def satisfies(self, recv_type: str, container: str) -> bool:
        return _satisfies(recv_type, container, self.implementors)

    # --- queries ---------------------------------------------------------
    def _hit(self, name: str, qc: str, exact: bool) -> bool:
        return (_collapse(name) == qc) if exact else _matches(name, qc)

    def matching_defs(self, qc: str, exact: bool = False) -> list[dict]:
        """Distinct production definitions whose short name matches the query."""
        seen: set[tuple] = set()
        out: list[dict] = []
        for s in self.symbols:
            if s.get("generated") or s.get("is_test"):
                continue
            if self._hit(_short_name(s["name"]), qc, exact):
                key = (s["name"], s["path"], s["line"])
                if key not in seen:
                    seen.add(key)
                    out.append(s)
        return out

    def callers(self, qc: str, exact: bool = False) -> list[dict]:
        """Grouped caller methods of any symbol matching collapsed query `qc`.

        Every group is kept, whatever its tier. A group's tier is the best of its call
        sites, so one confident site is not buried by a weaker one beside it, and
        contradicted groups sink rather than vanish.
        """
        groups: dict[tuple, dict] = {}
        for cs in self.call_sites:
            if cs["caller"] is None or not self._hit(cs["callee"], qc, exact):
                continue
            key = (cs["caller_path"], cs["caller"], cs["caller_line"])
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "path": cs["caller_path"], "name": Path(cs["caller_path"]).name,
                    "caller": cs["caller"], "caller_line": cs["caller_line"],
                    "is_test": cs["is_test"], "call_lines": [], "tier": TIER_WEAK,
                }
            g["call_lines"].append(cs["line"])
            caller_container = self._container_at(cs["caller_path"], cs["caller_line"])
            best = max((_resolve_tier(cs, d, caller_container, self.type_names,
                                      self.implementors)
                        for d in self.sym_by_name.get(cs["callee"].lower(), ())),
                       default=TIER_WEAK)
            g["tier"] = max(g["tier"], best)
        out = list(groups.values())
        out.sort(key=lambda r: (r["is_test"], -r["tier"], -len(r["call_lines"])))
        return out

    def _container_at(self, path: str, line: int) -> str:
        """The container of the symbol declared at `path:line`, if it is one we know."""
        sym = self.sym_by_pos.get((path, line))
        return (sym.get("container") or "") if sym else ""

    def unanimous_target(self, qc: str, exact: bool = False) -> str | None:
        """`Class.Method` when every confident call site agrees, else None.

        Lets an ambiguity warning be replaced by an answer, but only when *every* call
        site agrees. "Every confident site agrees" is not good enough, and the difference
        is not academic: on a real C# service, `callers HandleException` matched three
        definitions and a single same-file site resolved confidently to
        `HandleExceptionAsync` — a different method the prefix query also caught. Counting
        only that site turned a useful warning into a wrong reassurance. One unresolved
        site now keeps the warning.

        Also requires the matching definitions to share a short name. A prefix query
        spanning `Save` and `SaveDraft` is not an ambiguity that resolution can settle;
        it is a query that needs narrowing, which is what the warning already says.
        """
        allowed = {(d["path"], d["line"]) for d in self.matching_defs(qc, exact)}
        if len({_short_name(d["name"]).lower()
                for d in self.matching_defs(qc, exact)}) > 1:
            return None
        agreed: set[tuple] = set()
        for cs in self.call_sites:
            if cs["caller"] is None or not self._hit(cs["callee"], qc, exact):
                continue
            caller_container = self._container_at(cs["caller_path"], cs["caller_line"])
            here = {(d["qualified"], d["path"], d["line"])
                    for d in self.sym_by_name.get(cs["callee"].lower(), ())
                    if (d["path"], d["line"]) in allowed
                    and _resolve_tier(cs, d, caller_container, self.type_names,
                                      self.implementors) >= TIER_STRONG}
            if not here:
                return None  # this site is unresolved, so the query is still ambiguous
            agreed |= here
        if len(agreed) != 1:
            return None
        name, path, line = agreed.pop()
        return f"{name} ({Path(path).name}:{line})"

    def _best_def(self, name_lower: str, cs: dict | None = None,
                  caller_container: str = "") -> dict | None:
        """Resolve a callee name to its single most-likely definition: prefer
        production (non-test, non-generated) logic methods. Collapses the recall
        fan-out (generated Reference.cs, DTO properties, test copies) that would
        otherwise bloat a call skeleton.

        Where `cs` names a receiver or counts arguments, that outranks the legacy tie
        break — which was alphabetical path order, so a genuinely ambiguous callee was
        resolved by filename. The old key stays as the tail, so nothing moves when the
        evidence is silent.

        Guarded on purpose: unlike `callers`, this *substitutes* a target rather than
        reordering a list, so a wrong tier sends `calls` and `trace --down` somewhere
        false that the agent is told to trust. Weak evidence therefore changes nothing —
        only tier 2 and above may move the answer.
        """
        defs = self.sym_by_name.get(name_lower)
        if not defs:
            return None

        def legacy(s: dict):
            logic = s.get("kind") in ("method", "function", "constructor")
            return (bool(s.get("is_test")), bool(s.get("generated")), not logic, s["path"])

        if cs is None or len(defs) == 1:
            return sorted(defs, key=legacy)[0]
        tiers = {id(s): _resolve_tier(cs, s, caller_container, self.type_names,
                                      self.implementors)
                 for s in defs}
        if max(tiers.values()) < TIER_STRONG and not _uniquely_narrowed(defs, tiers):
            return sorted(defs, key=legacy)[0]
        return sorted(defs, key=lambda s: (-tiers[id(s)],) + legacy(s))[0]

    def calls(self, qc: str, exact: bool = False) -> list[dict]:
        """For each PRODUCTION method matching `qc`, the project-defined symbols it
        calls (one best definition per callee) — a lean flow skeleton."""
        by_def: dict[tuple, dict] = {}
        for cs in self.call_sites:
            if cs["caller"] is None or cs["is_test"]:
                continue  # skip test-method callers (they'd swamp a common name)
            if not self._hit(cs["caller"], qc, exact):
                continue
            key = (cs["caller_path"], cs["caller_line"])
            d = by_def.get(key)
            if d is None:
                sym = self.sym_by_pos.get(key)
                d = by_def[key] = {
                    "name": sym["qualified"] if sym else cs["caller"],
                    "path": cs["caller_path"], "line": cs["caller_line"],
                    "callees": [], "_seen": set(),
                }
            cl = cs["callee"].lower()
            if cl in d["_seen"]:
                continue
            d["_seen"].add(cl)
            caller_container = self._container_at(cs["caller_path"], cs["caller_line"])
            ds = self._best_def(cl, cs, caller_container)
            if ds:
                d["callees"].append({
                    "name": ds["qualified"], "kind": ds["kind"],
                    "path": ds["path"], "line": ds["line"],
                    "tier": _resolve_tier(cs, ds, caller_container, self.type_names,
                                          self.implementors),
                })
        out = list(by_def.values())
        for d in out:
            d.pop("_seen", None)
        return out

    def tiered_indegree(self) -> dict[str, int]:
        """Call-graph in-degree per (lowercased) production callee name, counting only
        sites where `_resolve_tier` finds SOME evidence (> TIER_WEAK) — not a raw
        callee-name match. Shared by `caller_count`, `arch`'s hotspot ranking, and
        `memo ui`'s graph view, so the fix below applies everywhere at once instead of
        drifting across three independent re-implementations.

        Measured directly on memo's own repo before this existed: `get` (a one-line
        function in adr.py) reported 229 "callers" under raw counting — every one of
        them a `dict.get(...)`/`config.get(...)` call with the same bare name and zero
        relation to the actual function, and every one of them sat at TIER_WEAK (zero
        evidence in any dimension) when checked individually. Only one call site in the
        whole repo genuinely called it. A raw name-match count is exactly wrong for any
        short, common verb name (get/add/run/set/...) — precisely the shape of name an
        "in-degree" ranking is most likely to surface, so this was the metric's most
        visible failure mode, not an edge case.

        Cached on the instance: computing it is O(edges), the same cost as the raw
        count used to be, but callers like `query`'s `--json` output ask for one name's
        count per symbol in a loop — recomputing per call would make that O(edges x
        symbols) instead. A `Graph` is rebuilt (not mutated) on every re-index, so
        there's no separate cache-invalidation path to get wrong here.
        """
        if self._indegree_cache is not None:
            return self._indegree_cache
        indeg: dict[str, int] = {}
        for cs in self.call_sites:
            if cs["caller"] is None or cs["is_test"]:
                continue
            callee_l = cs["callee"].lower()
            defs = self.sym_by_name.get(callee_l)
            if not defs:
                continue
            caller_container = self._container_at(cs["caller_path"], cs["caller_line"])
            best = max((_resolve_tier(cs, d, caller_container, self.type_names,
                                      self.implementors) for d in defs), default=TIER_WEAK)
            if best > TIER_WEAK:
                indeg[callee_l] = indeg.get(callee_l, 0) + 1
        self._indegree_cache = indeg
        return indeg

    def caller_count(self, short_name: str) -> int:
        return self.tiered_indegree().get(short_name.lower(), 0)

    def pagerank(self, damping: float = 0.85, max_iter: int = 100,
                 tol: float = 1e-8) -> dict[str, float]:
        """PageRank importance score per (lowercased) short name, over the same
        confidence-filtered edges `tiered_indegree` uses (tier > TIER_WEAK only — see
        that method's docstring for why raw name-matched edges are the wrong input:
        a hub of spurious same-name matches would otherwise rank as "important" for
        exactly the reason it's actually noise).

        Prefers `python-igraph`'s native pagerank — the same optional `[graph]` extra
        `clusters.py` already uses for Leiden clustering — falling back to a small
        hand-rolled power iteration when it isn't installed, so this works with zero
        new hard dependencies. Cached on the instance for the same reason
        `tiered_indegree` is: per-symbol lookups in a loop (`query`'s --json output,
        `brief`'s budget packer) would otherwise recompute the whole graph every time.
        """
        if self._pagerank_cache is not None:
            return self._pagerank_cache

        edges: list[tuple[str, str]] = []
        nodes: set[str] = set()
        for cs in self.call_sites:
            if cs["caller"] is None or cs["is_test"]:
                continue
            callee_l = cs["callee"].lower()
            defs = self.sym_by_name.get(callee_l)
            if not defs:
                continue
            caller_container = self._container_at(cs["caller_path"], cs["caller_line"])
            best = max((_resolve_tier(cs, d, caller_container, self.type_names,
                                      self.implementors) for d in defs), default=TIER_WEAK)
            if best <= TIER_WEAK:
                continue
            caller_l = cs["caller"].lower()
            edges.append((caller_l, callee_l))
            nodes.add(caller_l)
            nodes.add(callee_l)

        if not edges:
            self._pagerank_cache = {}
            return self._pagerank_cache

        igraph = _try_igraph()
        if igraph is not None:
            ordered = sorted(nodes)
            idx = {n: i for i, n in enumerate(ordered)}
            g = igraph.Graph(n=len(ordered),
                              edges=[(idx[a], idx[b]) for a, b in edges], directed=True)
            scores = g.pagerank(damping=damping)
            self._pagerank_cache = dict(zip(ordered, scores))
        else:
            self._pagerank_cache = _power_iteration_pagerank(
                edges, nodes, damping=damping, max_iter=max_iter, tol=tol)
        return self._pagerank_cache

    def pagerank_score(self, short_name: str) -> float:
        return self.pagerank().get(short_name.lower(), 0.0)

    def field_access_sites(self, qc: str, exact: bool = False) -> list[dict]:
        """Every recorded read/write of a field/property matching collapsed query `qc`,
        production sites first, writes before reads within a site's own enclosing
        method (mutation is usually the more interesting half of "who touches this").
        """
        out = [a for a in self.field_accesses if self._hit(a["field"], qc, exact)]
        out.sort(key=lambda a: (a["is_test"], not a["is_write"], a["path"], a["line"]))
        return out

    # --- persistence -----------------------------------------------------
    # Stored columnar rather than as lists-of-dicts, because the graph is re-read on
    # every relationship query and the dict form was overwhelmingly repetition: for a
    # 541-file repo, call_sites alone was 9.0 MB of a 10.7 MB file — 3.1 MB of that
    # being 447 distinct paths repeated ~98x each, plus the six JSON key names repeated
    # 44,206 times. Interning paths into a table and dropping the keys cuts the file
    # several-fold, and load time with it. Purely a serialization change: the in-memory
    # shape the queries see is unchanged.

    SYMBOL_COLS = ("name", "qualified", "kind", "path", "line", "end_line",
                   "is_test", "generated", "container", "pmin", "pmax", "pflags",
                   "bases")
    CALL_COLS = ("caller", "caller_path", "caller_line", "callee", "line", "is_test",
                 "recv", "recv_kind", "argc", "recv_type")

    def to_json(self) -> dict:
        paths = sorted({s["path"] for s in self.symbols}
                       | {cs["caller_path"] for cs in self.call_sites}
                       | {a["path"] for a in self.field_accesses}
                       | {r["path"] for r in self.routes})
        pidx = {p: i for i, p in enumerate(paths)}
        return {
            "version": 4,
            "paths": paths,
            "symbols": [
                [s["name"], s["qualified"], s.get("kind", ""), pidx[s["path"]],
                 s["line"], s.get("end_line") or 0,
                 1 if s.get("is_test") else 0, 1 if s.get("generated") else 0,
                 s.get("container", ""), s.get("pmin", UNKNOWN_ARITY),
                 s.get("pmax", UNKNOWN_ARITY), s.get("pflags", 0),
                 s.get("bases", "")]
                for s in self.symbols
            ],
            "call_sites": [
                [cs["caller"], pidx[cs["caller_path"]], cs["caller_line"],
                 cs["callee"], cs["line"], 1 if cs["is_test"] else 0,
                 cs.get("recv"), cs.get("recv_kind", RECV_BARE),
                 cs.get("argc", UNKNOWN_ARITY), cs.get("recv_type", "")]
                for cs in self.call_sites
            ],
            "freq": self.freq,
            "files": self.files,
            # Additive and read with .get, so no version bump: an older memo ignores it
            # and a newer memo reading an older graph falls back to the count.
            "fingerprint": self.fingerprint,
            # Same additive convention as fingerprint above: an older memo reading this
            # file ignores the key; from_json below defaults to [] for a graph written
            # before field-access tracking existed.
            "field_accesses": [
                [a["field"], pidx[a["path"]], a["line"], a["enclosing"],
                 a["enclosing_line"], 1 if a["is_write"] else 0, 1 if a["is_test"] else 0]
                for a in self.field_accesses
            ],
            # Additive, same convention as field_accesses/fingerprint above.
            "routes": [
                [r["pattern"], pidx[r["path"]], r["line"], r["handler"],
                 r["handler_line"], 1 if r["is_test"] else 0]
                for r in self.routes
            ],
        }

    @staticmethod
    def _decode_field_accesses(d: dict, paths: list[str]) -> list[dict]:
        """Absent for any graph written before field-access tracking existed (or one
        with no field-like symbols to track) — `.get` defaults to `[]` in that case,
        same additive convention as `fingerprint`."""
        it = sys.intern
        return [
            {"field": it(c0), "path": paths[c1], "line": c2,
             "enclosing": c3 if c3 is None else it(c3), "enclosing_line": c4,
             "is_write": c5 == 1, "is_test": c6 == 1}
            for c0, c1, c2, c3, c4, c5, c6 in d.get("field_accesses", [])
        ]

    @staticmethod
    def _decode_routes(d: dict, paths: list[str]) -> list[dict]:
        it = sys.intern
        return [
            {"pattern": c0, "path": paths[c1], "line": c2,
             "handler": c3 if c3 is None else it(c3), "handler_line": c4,
             "is_test": c5 == 1}
            for c0, c1, c2, c3, c4, c5 in d.get("routes", [])
        ]

    @classmethod
    def from_json(cls, d: dict) -> "Graph":
        version = d.get("version", 1)
        if version < 2:
            # v1 graphs stored plain dicts. Read them so an existing .memo keeps
            # working; the next index writes the current version.
            return cls(d.get("symbols", []), d.get("call_sites", []), d.get("freq", {}),
                       d.get("files", 0), d.get("fingerprint", "")).index()

        # Hot path: tens of thousands of rows are rebuilt on every relationship query,
        # so this uses dict literals rather than dict(zip(...)) (measured ~1.8x faster)
        # and interns the repeated name strings, which the query paths hash constantly.
        paths = [sys.intern(p) for p in d.get("paths", [])]
        it = sys.intern
        if version < 4:
            # Older graphs predate some of the resolution columns. Absent evidence must
            # read as *unknown*, not as neutral-looking zeros, so every tier collapses to
            # WEAK and ranking falls back to the pre-confidence order until the next index
            # rewrites the file. `load` checks only this integer, never memo_version, so a
            # stale graph really is served after an upgrade.
            symbols = [
                {"name": it(r[0]), "qualified": r[1], "kind": r[2], "path": paths[r[3]],
                 "line": r[4], "end_line": r[5], "is_test": r[6] == 1,
                 "generated": r[7] == 1,
                 "container": it(r[8]) if len(r) > 8 else "",
                 "pmin": r[9] if len(r) > 9 else UNKNOWN_ARITY,
                 "pmax": r[10] if len(r) > 10 else UNKNOWN_ARITY,
                 "pflags": r[11] if len(r) > 11 else 0,
                 "bases": r[12] if len(r) > 12 else ""}
                for r in d.get("symbols", [])
            ]
            call_sites = [
                {"caller": r[0] if r[0] is None else it(r[0]), "caller_path": paths[r[1]],
                 "caller_line": r[2], "callee": it(r[3]), "line": r[4],
                 "is_test": r[5] == 1,
                 "recv": r[6] if len(r) > 6 and r[6] is None else (
                     it(r[6]) if len(r) > 6 else None),
                 "recv_kind": r[7] if len(r) > 7 else RECV_BARE,
                 "argc": r[8] if len(r) > 8 else UNKNOWN_ARITY,
                 "recv_type": r[9] if len(r) > 9 else ""}
                for r in d.get("call_sites", [])
            ]
            return cls(symbols, call_sites, d.get("freq", {}), d.get("files", 0),
                      d.get("fingerprint", ""),
                      field_accesses=cls._decode_field_accesses(d, paths),
                      routes=cls._decode_routes(d, paths)).index()

        symbols = [
            {"name": it(c0), "qualified": c1, "kind": c2, "path": paths[c3],
             "line": c4, "end_line": c5, "is_test": c6 == 1, "generated": c7 == 1,
             "container": it(c8), "pmin": c9, "pmax": c10, "pflags": c11,
             "bases": c12}
            for c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12
            in d.get("symbols", [])
        ]
        call_sites = [
            {"caller": c0 if c0 is None else it(c0), "caller_path": paths[c1],
             "caller_line": c2, "callee": it(c3), "line": c4, "is_test": c5 == 1,
             "recv": c6 if c6 is None else it(c6), "recv_kind": c7, "argc": c8,
             "recv_type": it(c9)}
            for c0, c1, c2, c3, c4, c5, c6, c7, c8, c9 in d.get("call_sites", [])
        ]
        return cls(symbols, call_sites, d.get("freq", {}), d.get("files", 0),
                  d.get("fingerprint", ""),
                  field_accesses=cls._decode_field_accesses(d, paths),
                  routes=cls._decode_routes(d, paths)).index()


SHARD_FILE = "shards.json"


# Purely a noise cut for field-access candidates, not a correctness filter: a keyword
# that slips through still can't match a real field/property's short name at assembly
# time (build()'s global filter), so it costs a wasted candidate row, never a wrong
# answer. Kept small and language-agnostic on purpose — one shared list instead of
# five per-language ones, since being wrong here is cheap and being incomplete is free.
_ACCESS_SKIP_WORDS = frozenset((
    "if", "else", "elif", "for", "foreach", "while", "do", "switch", "case", "return",
    "break", "continue", "class", "struct", "interface", "enum", "def", "function",
    "public", "private", "protected", "internal", "static", "readonly", "const",
    "var", "let", "val", "new", "true", "false", "null", "none", "nil", "undefined",
    "self", "this", "me", "mybase", "myclass", "cls", "try", "catch", "finally",
    "throw", "raise", "import", "from", "using", "namespace", "package", "void",
    "int", "string", "bool", "float", "double", "long", "object", "and", "or", "not",
    "in", "is", "as", "async", "await", "yield", "lambda", "with", "pass", "get", "set",
))

# A field/property is being written, not read, when the next non-space token after it
# is a plain or compound assignment — never `==`/`!=`/`<=`/`>=`, which the negative
# lookahead on `=` excludes explicitly so `if (x == y)` never reads as a write to `x`.
#
# No leading `^`: this is matched via `pattern.match(text, pos)` at an arbitrary offset,
# and `^` in a plain (non-MULTILINE) pattern anchors to true position 0 of the whole
# string, not to `pos` — `match()` already only tries at `pos`, so `^` doesn't add
# anchoring, it silently *removes* every match except the one at the very start of the
# file. Caught live: `UserId = id;` inside a real method still classified as a read
# until this was fixed, because `^` at offset ~40 in the file could never match.
_ASSIGN_OP = re.compile(r"\s*(?:=(?!=)|\+=|-=|\*=|/=|%=|\|=|&=|\^=|<<=|>>=|\?\?=)")


def _scan_file(text: str, syms: list[dict], dbl: dict[int, str],
               language: str = "") -> dict:
    """Per-file scan results that depend ONLY on the file's content.

    Split out so it can be cached per content hash. Deliberately does *not* filter
    callees (or field-access candidates) against the set of known project symbols:
    that set is global, so filtering here would make a shard depend on every other
    file and thus uncacheable. The filtering happens at assembly instead, which
    yields an identical graph.

    Discovery still runs `_CALL` over raw text, unchanged and deliberately so. Making it
    string-aware to help the argument counter would silently drop real edges: `$"{Fmt(x)}"`
    yields an edge for `Fmt` today, and losing it is far worse than not knowing its arity.
    Receiver and argument count are *measurements* layered onto the same matches, and
    either may be unknown.
    """
    freq: Counter = Counter()
    for m in _IDENT.finditer(text):
        freq[m.group(0).lower()] += 1

    # One pass to map offsets to lines, instead of re-counting newlines from the top of
    # the file for every call site. This was the dominant cost on large files.
    starts = line_starts(text)
    argcs = scan_arg_counts(text, language)  # likewise once per file, not per call site
    types = declared_types(text, language)
    cands: list[list] = []
    call_positions: set[int] = set()
    for m in _CALL.finditer(text):
        callee = m.group(1)
        line = line_at(starts, m.start())
        call_positions.add(m.start())
        if dbl.get(line) == callee.lower():  # the declaration itself, not a call
            continue
        encl = _enclosing(syms, line)
        if encl and encl.get("kind") == "interface":
            continue  # `Name(` inside an interface is a signature, not a call
        recv, recv_kind = _classify_receiver(text, m.start())
        named = recv if recv_kind == RECV_IDENT else None
        cands.append([callee, line,
                      encl["name"] if encl else None,
                      encl["line"] if encl else 0,
                      named, recv_kind,
                      argcs.get(m.end() - 1, UNKNOWN_ARITY),
                      types.get(named or "", "")])

    # Field/property access candidates: every bare identifier that ISN'T also a call
    # site (already recorded above) and isn't a language keyword. Narrowed to known
    # field/property names at assembly time, exactly like `cands` is narrowed to known
    # callees — see the docstring above for why that has to happen there, not here.
    accesses: list[list] = []
    for m in _IDENT.finditer(text):
        if m.start() in call_positions:
            continue
        name = m.group(0)
        if name.lower() in _ACCESS_SKIP_WORDS:
            continue
        line = line_at(starts, m.start())
        if dbl.get(line) == name.lower():
            continue  # the declaration line itself, not a use of it
        encl = _enclosing(syms, line)
        if encl and encl.get("kind") == "interface":
            continue
        is_write = bool(_ASSIGN_OP.match(text, m.end()))
        accesses.append([name, line, encl["name"] if encl else None,
                         encl["line"] if encl else 0, is_write])

    # Route/endpoint declarations. No global filter needed here, unlike calls and
    # field accesses: a route match is a self-contained fact (a decorator/attribute
    # literally naming a path), not a name that only means something once checked
    # against a separately-declared symbol table — so unlike `cands`/`accesses`,
    # every candidate found here is already the final answer.
    routes: list[list] = []
    for m in _ROUTE_DECL.finditer(text):
        pattern = next(g for g in m.groups() if g)
        line = line_at(starts, m.start())
        target = _next_symbol_after(syms, line)
        if target is not None and target.get("kind") in ("method", "function", "constructor"):
            handler_name, handler_line = target["name"], target["line"]
        else:
            # No method declared within the window right after this attribute/decorator
            # — fall back to whatever container already encloses this line.
            encl = _enclosing(syms, line)
            handler_name = encl["name"] if encl else None
            handler_line = encl["line"] if encl else 0
        routes.append([pattern, line, handler_name, handler_line])

    return {"freq": dict(freq), "cands": cands, "accesses": accesses, "routes": routes}


def _load_shards(cache_root: Path | None) -> dict:
    if cache_root is None:
        return {}
    f = cache_root / SHARD_FILE
    if not f.is_file():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # Shards embed analyzer-derived positions, so they're only valid for this memo.
    if data.get("memo_version") != __version__:
        return {}
    return data.get("shards", {})


def _save_shards(cache_root: Path | None, shards: dict) -> None:
    if cache_root is None:
        return
    try:
        tmp = (cache_root / SHARD_FILE).with_suffix(".tmp")
        tmp.write_text(json.dumps({"memo_version": __version__, "shards": shards}),
                       encoding="utf-8")
        tmp.replace(cache_root / SHARD_FILE)
    except OSError:
        pass  # a missing shard cache only costs speed, never correctness


def build(entries, cache_root: Path | None = None) -> Graph:
    """Build the symbol table + call-site edges for all cached entries.

    The expensive part is scanning every file's text with two regexes; on a 541-file
    repo that dominated re-indexing even when nothing had changed. Results are cached
    per *content hash* in `.memo/shards.json`, so an unchanged file is neither re-read
    nor re-scanned. Pass cache_root to enable that; without it this is a full scan.
    """
    file_syms: dict[str, list[dict]] = {}
    defs_by_pos: dict[str, dict[int, str]] = {}
    symbols: list[dict] = []
    known: set[str] = set()
    known_fields: set[str] = set()
    for e in entries:
        syms = _sorted_symbols(e)
        file_syms[e.path] = syms
        gen, test = _is_generated(e.path), _is_test(e.path)
        dbl = defs_by_pos.setdefault(e.path, {})
        containers = _containers_by_line(syms)
        for s in syms:
            short = _short_name(s["name"])
            kind = s.get("kind", "")
            pmin, pmax, pflags = param_arity(s.get("signature") or "", e.language, kind)
            bases = (",".join(base_types(s.get("signature") or "", e.language))
                     if kind in _CONTAINER_KINDS else "")
            symbols.append({"name": short, "qualified": s["name"],
                            "kind": kind, "path": e.path,
                            "line": s["line"], "end_line": s.get("end_line") or 0,
                            "is_test": test, "generated": gen,
                            "container": containers.get(s["line"], ""),
                            "pmin": pmin, "pmax": pmax, "pflags": pflags,
                            "bases": bases})
            known.add(short.lower())
            dbl[s["line"]] = short.lower()
            if kind in _FIELD_KINDS:
                known_fields.add(short.lower())

    cached = _load_shards(cache_root)
    fresh: dict = {}
    call_sites: list[dict] = []
    field_accesses: list[dict] = []
    routes: list[dict] = []
    freq: Counter = Counter()
    for e in entries:
        shard = cached.get(e.content_hash)
        if shard is None:
            p = Path(e.path)
            if not p.is_file():
                continue
            shard = _scan_file(_read_text(p), file_syms[e.path],
                               defs_by_pos.get(e.path, {}), e.language)
        fresh[e.content_hash] = shard

        freq.update(shard["freq"])
        test = _is_test(e.path)
        for (callee, line, encl_name, encl_line, recv, recv_kind, argc,
                recv_type) in shard["cands"]:
            if callee.lower() not in known:
                continue  # global filter, applied here so shards stay file-local
            call_sites.append({
                "caller": encl_name, "caller_path": e.path,
                "caller_line": encl_line, "callee": callee,
                "line": line, "is_test": test,
                "recv": recv, "recv_kind": recv_kind, "argc": argc,
                "recv_type": recv_type,
            })
        for (name, line, encl_name, encl_line, is_write) in shard.get("accesses", []):
            if name.lower() not in known_fields:
                continue  # same global filter, same reason: keeps shards file-local
            field_accesses.append({
                "field": name, "path": e.path, "line": line,
                "enclosing": encl_name, "enclosing_line": encl_line,
                "is_write": is_write, "is_test": test,
            })
        for (pattern, line, encl_name, encl_line) in shard.get("routes", []):
            routes.append({
                "pattern": pattern, "path": e.path, "line": line,
                "handler": encl_name, "handler_line": encl_line, "is_test": test,
            })

    _save_shards(cache_root, fresh)  # pruned to the current content hashes
    return Graph(symbols, call_sites, dict(freq), files=len(entries),
             fingerprint=fingerprint_of(entries),
             field_accesses=field_accesses, routes=routes).index()


def save(graph: Graph, cache_root: Path) -> None:
    (cache_root / GRAPH_FILE).write_text(
        json.dumps(graph.to_json()), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Runtime trace ingestion — upgrades name-based candidate edges to TIER_VERIFIED.
#
# `memo ingest-traces` (memo/traces.py) records observed (caller, callee) pairs from
# real execution into RUNTIME_FILE. This is the one place they take effect: merged into
# the in-memory Graph on every load, never written back into graph.json. That keeps
# ingested evidence reversible (delete the file, it's gone) and additive to a
# from-source re-index, instead of a second, harder-to-invalidate copy of the graph.
# --------------------------------------------------------------------------- #

RUNTIME_FILE = "runtime_edges.json"


def _find_symbol(graph: "Graph", ref: str) -> dict | None:
    """Resolve a trace-recorded name (`"Class.method"`, `"module.func"`, bare `"func"`)
    to the single most-likely symbol definition, or None if nothing matches.

    A trace only ever names *what* ran, not *which* memo already knows about, so this
    reuses the same short-name index `callers`/`calls` use rather than requiring an exact
    qualified match — a bare `func` from a Python trace still resolves.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    parts = re.split(r"[.#]", ref)
    short = parts[-1].lower()
    candidates = graph.sym_by_name.get(short)
    if not candidates:
        return None
    if len(parts) > 1:
        container = parts[-2].lower()
        for s in candidates:
            if (s.get("container") or "").lower() == container:
                return s
    if len(candidates) == 1:
        return candidates[0]
    # Ambiguous and receiver didn't settle it: prefer real, non-generated production
    # logic over a test double or a DTO property sharing the same name.
    prod = [s for s in candidates
            if not s.get("is_test") and not s.get("generated")
            and s.get("kind") in ("method", "function", "constructor")]
    return (prod or candidates)[0]


def _load_runtime_edges(cache_root: Path | None) -> list[dict]:
    if cache_root is None:
        return []
    f = cache_root / RUNTIME_FILE
    if not f.is_file():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data.get("edges", []) if isinstance(data, dict) else []


def merge_runtime_edges(graph: "Graph", cache_root: Path | None) -> int:
    """Append verified call_sites for every ingested (caller, callee) pair that
    resolves against this graph's symbol table. Returns how many were added.

    An edge whose callee cannot be resolved is dropped rather than kept as a dangling
    reference — nothing downstream expects a call_site whose callee isn't in
    `sym_by_name`. An edge whose caller cannot be resolved (the recorder saw the frame
    but memo never indexed it — a lambda, a decorator wrapper) is still kept with
    `caller=None`, the same convention memo already uses for "file scope" call sites.
    """
    added = 0
    for rec in _load_runtime_edges(cache_root):
        callee_sym = _find_symbol(graph, rec.get("callee", ""))
        if callee_sym is None:
            continue
        caller_sym = _find_symbol(graph, rec.get("caller", ""))
        graph.call_sites.append({
            "caller": _short_name(caller_sym["qualified"]) if caller_sym else None,
            "caller_path": caller_sym["path"] if caller_sym else (callee_sym["path"]),
            "caller_line": caller_sym["line"] if caller_sym else 0,
            "callee": _short_name(callee_sym["qualified"]),
            "line": caller_sym["line"] if caller_sym else callee_sym["line"],
            "is_test": bool(caller_sym.get("is_test")) if caller_sym else False,
            "recv": None, "recv_kind": RECV_BARE, "argc": UNKNOWN_ARITY, "recv_type": "",
            "verified": True, "hits": rec.get("count", 1),
        })
        added += 1
    return added


def load(cache_root: Path) -> Graph | None:
    f = cache_root / GRAPH_FILE
    if not f.is_file():
        return None
    try:
        g = Graph.from_json(json.loads(f.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, KeyError):
        return None
    merge_runtime_edges(g, cache_root)
    return g


def load_or_build(cache) -> Graph:
    """Prefer the persisted graph; build in-memory from the cache if absent."""
    g = load(cache.root)
    if g is not None:
        return g
    g = build(cache.list_entries(), cache_root=cache.root)
    merge_runtime_edges(g, cache.root)
    return g

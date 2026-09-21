"""`memo query` — ad-hoc filtering over the persisted graph.

`codebase-memory-mcp` answers open-ended structural questions with Cypher over a real
graph database. memo has no database and no query engine to embed one in, so this is
deliberately smaller: a flat predicate filter over the two tables the graph already is
(`symbols`, `call_sites`) — enough to answer "what the fixed commands can't" without
requiring a query language anyone has to learn from scratch.

Syntax:  memo query "<target> [field<op>value ...]"

    target   symbols | edges           (default: symbols)
    op       =  !=  ~  >  <  >=  <=    (~ is substring/contains, case-insensitive)
    value    a bare word, or "quoted for spaces"

Examples:
    memo query "symbols kind=method callers>5"
    memo query "symbols path~insights.py is_test=false"
    memo query "edges callee=SaveNote tier>=strong"

Fields on `symbols`: name, qualified, kind, path, line, end_line, is_test, generated,
container, callers (computed: in-repo call-graph in-degree), pagerank (computed:
structural importance — PageRank over the same confidence-filtered call edges
`callers` uses; higher means more central to the call graph, not just more-called).

Fields on `edges` (call_sites): caller, callee, caller_path, caller_line, line, is_test,
recv, recv_kind, argc, tier (computed per-edge: contra/weak/ok/strong/exact/verified —
same scoring `memo callers`/`memo calls` use), verified (true only for
`memo ingest-traces` edges).

Every predicate is ANDed; there is no OR and no nesting. That is the deliberate edge
of "small DSL, not a query language" — a question that needs more than a conjunction
of field comparisons is better served by `memo callers`/`impact`/`brief`, or by asking
for the raw rows with `--json` and filtering downstream.
"""

from __future__ import annotations

import shlex

from .graph import _tier_mark
from .graphindex import TIER_NAMES, TIER_WEAK, Graph, _resolve_tier
from .mapper import _short_name, disambiguate

OPS = (">=", "<=", "!=", "=", "~", ">", "<")  # order matters: >= before >, etc.
_OPS = OPS  # internal alias, kept for brevity in this module

_SYMBOL_FIELDS = ("name", "qualified", "kind", "path", "line", "end_line",
                  "is_test", "generated", "container", "callers", "pagerank")
_EDGE_FIELDS = ("caller", "callee", "caller_path", "caller_line", "line", "is_test",
                "recv", "recv_kind", "argc", "tier", "verified")
_INT_FIELDS = {"line", "end_line", "callers", "caller_line", "argc"}
_FLOAT_FIELDS = {"pagerank"}
_BOOL_FIELDS = {"is_test", "generated", "verified"}
_TIER_RANK = {name: val for val, name in TIER_NAMES.items()}


class QueryError(ValueError):
    pass


def parse(qs: str) -> tuple[str, list[tuple[str, str, str]]]:
    try:
        tokens = shlex.split(qs)
    except ValueError as exc:
        raise QueryError(f"unbalanced quotes: {exc}") from exc
    if not tokens:
        raise QueryError('empty query — try `symbols kind=method`')

    target = "symbols"
    rest = tokens
    if tokens[0] in ("symbols", "edges"):
        target, rest = tokens[0], tokens[1:]

    preds: list[tuple[str, str, str]] = []
    fields = _SYMBOL_FIELDS if target == "symbols" else _EDGE_FIELDS
    for tok in rest:
        op = next((o for o in _OPS if o in tok), None)
        if op is None:
            raise QueryError(f'"{tok}" has no operator (one of {" ".join(_OPS)})')
        field, value = tok.split(op, 1)
        if field not in fields:
            raise QueryError(f'unknown field "{field}" for {target} — one of: '
                             f'{", ".join(fields)}')
        preds.append((field, op, value))
    return target, preds


def _coerce(field: str, value: str):
    if field in _BOOL_FIELDS:
        return value.strip().lower() in ("1", "true", "yes", "y")
    if field == "tier":
        return _TIER_RANK.get(value.strip().lower(), value)
    if field in _INT_FIELDS:
        try:
            return int(value)
        except ValueError:
            return value
    if field in _FLOAT_FIELDS:
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _cmp(actual, op: str, expected) -> bool:
    if op == "~":
        return str(expected).lower() in str(actual).lower()
    if op == "=":
        return str(actual).lower() == str(expected).lower() if isinstance(expected, str) else actual == expected
    if op == "!=":
        return not _cmp(actual, "=", expected)
    try:
        a, b = float(actual), float(expected)
    except (TypeError, ValueError):
        return False
    return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def _match(row: dict, preds: list[tuple[str, str, str]]) -> bool:
    for field, op, raw in preds:
        if not _cmp(row.get(field, ""), op, _coerce(field, raw)):
            return False
    return True


def _edge_tier(graph: Graph, cs: dict) -> int:
    caller_container = graph._container_at(cs["caller_path"], cs["caller_line"])
    defs = graph.sym_by_name.get(cs["callee"].lower(), ())
    return max((_resolve_tier(cs, d, caller_container, graph.type_names, graph.implementors)
                for d in defs), default=TIER_WEAK)


def run(graph: Graph, qs: str, limit: int = 200) -> dict:
    target, preds = parse(qs)
    rows: list[dict] = []
    if target == "symbols":
        for s in graph.symbols:
            row = dict(s)
            row["callers"] = graph.caller_count(_short_name(row["name"]))
            row["pagerank"] = round(graph.pagerank_score(_short_name(row["name"])), 6)
            if _match(row, preds):
                rows.append(row)
    else:
        for cs in graph.call_sites:
            row = dict(cs)
            row["tier"] = _edge_tier(graph, cs)
            row["tier_name"] = TIER_NAMES.get(row["tier"], "weak")
            if _match(row, preds):
                rows.append(row)
    total = len(rows)
    return {"query": qs, "target": target, "rows": rows[:limit], "total": total,
            "shown": min(total, limit)}


def render(result: dict, terse: bool = False) -> str:
    target, rows = result["target"], result["rows"]
    if terse:
        lines = [f'query "{result["query"]}" — {result["shown"]} of {result["total"]} (terse)']
        if target == "symbols":
            lbl = disambiguate([r["path"] for r in rows])
            for r in rows:
                lines.append(f"{r['name']} `{r['kind']}` {lbl[r['path']]}:{r['line']} "
                             f"callers={r['callers']} pagerank={r['pagerank']}")
        else:
            lbl = disambiguate([r["caller_path"] for r in rows])
            for r in rows:
                lines.append(f"{r['caller'] or '(file scope)'} -> {r['callee']} "
                             f"{lbl[r['caller_path']]}:{r['line']}{_tier_mark(r['tier'], terse=True)}")
        if not rows:
            lines.append("(no matches)")
        return "\n".join(lines) + "\n"

    lines = [f'# query "{result["query"]}" — {result["shown"]} of {result["total"]} {target}', ""]
    if target == "symbols":
        lbl = disambiguate([r["path"] for r in rows])
        for r in rows:
            tag = " [test]" if r["is_test"] else ""
            lines.append(f"- **{r['name']}** `{r['kind']}`{tag} — {lbl[r['path']]}:{r['line']} "
                         f"(callers: {r['callers']}, pagerank: {r['pagerank']})")
    else:
        lbl = disambiguate([r["caller_path"] for r in rows])
        for r in rows:
            lines.append(f"- {r['caller'] or '(file scope)'} → {r['callee']} "
                         f"— {lbl[r['caller_path']]}:{r['line']}{_tier_mark(r['tier'], terse=False)}")
    if not rows:
        lines.append("_No matches._")
    if result["total"] > result["shown"]:
        lines.append(f"\n_…and {result['total'] - result['shown']} more — raise --limit._")
    return "\n".join(lines).rstrip() + "\n"

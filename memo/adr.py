"""Architecture Decision Records as queryable, symbol-linked records —
`codebase-memory-mcp`'s `manage_adr`, in memo's flat-JSON, no-database form.

memo's summaries are structural, from parsing — never *why* code is the way it is
(README: "It won't explain logic"). An ADR is the other half of that answer, and the
only part worth memo owning is making it findable from a symbol instead of from a
`docs/adr/` directory an agent has to know to look in. Stored in `.memo/adrs.json`,
linked to symbols/files by the same name-matching `memo map`/`callers` already use, so
linking an ADR to `HandleException` also surfaces it for `SaveExceptionHandler` in the
same file family without demanding an exact match.
"""

from __future__ import annotations

import json
from pathlib import Path

from .mapper import _collapse, _matches

ADR_FILE = "adrs.json"


def _load(cache_root: Path) -> dict:
    f = cache_root / ADR_FILE
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data.setdefault("next_id", 1)
            data.setdefault("records", [])
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"next_id": 1, "records": []}


def _save(cache_root: Path, data: dict) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / ADR_FILE).write_text(
        json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def add(cache_root: Path, title: str, *, status: str = "accepted", date: str = "",
        context: str = "", decision: str = "", consequences: str = "",
        affects: list[str] | None = None) -> dict:
    data = _load(cache_root)
    rec = {
        "id": data["next_id"], "title": title, "status": status, "date": date,
        "context": context, "decision": decision, "consequences": consequences,
        "affects": affects or [],
    }
    data["records"].append(rec)
    data["next_id"] += 1
    _save(cache_root, data)
    return rec


def list_all(cache_root: Path) -> list[dict]:
    return _load(cache_root)["records"]


def get(cache_root: Path, adr_id: int) -> dict | None:
    return next((r for r in list_all(cache_root) if r["id"] == adr_id), None)


def update_status(cache_root: Path, adr_id: int, status: str) -> bool:
    data = _load(cache_root)
    for r in data["records"]:
        if r["id"] == adr_id:
            r["status"] = status
            _save(cache_root, data)
            return True
    return False


def delete(cache_root: Path, adr_id: int) -> bool:
    data = _load(cache_root)
    before = len(data["records"])
    data["records"] = [r for r in data["records"] if r["id"] != adr_id]
    if len(data["records"]) == before:
        return False
    _save(cache_root, data)
    return True


def for_symbol(cache_root: Path, query: str) -> list[dict]:
    """ADRs whose `affects` list names `query`, either side matching at a token
    boundary — same identifier-aware matching as `memo map`, so an ADR tagged with
    `CaseProcessor` still surfaces for a query of `HandleCaseProcessor`."""
    qc = _collapse(query)
    out = []
    for r in list_all(cache_root):
        for pat in r.get("affects", []):
            pc = _collapse(pat)
            if _matches(pat, qc) or _matches(query, pc) or qc == pc:
                out.append(r)
                break
    return out


def render_list(records: list[dict], terse: bool = False) -> str:
    if not records:
        return "_No ADRs recorded yet. Add one with `memo adr add \"<title>\" --affects Sym1,Sym2`._\n"
    if terse:
        lines = [f"adrs ({len(records)})"]
        for r in records:
            lines.append(f"ADR-{r['id']} [{r['status']}] {r['title']}")
        return "\n".join(lines) + "\n"
    lines = [f"# ADRs ({len(records)})", ""]
    for r in records:
        lines.append(f"- **ADR-{r['id']}** [{r['status']}] {r['title']}"
                     + (f"  ({r['date']})" if r.get("date") else ""))
        if r.get("affects"):
            lines.append(f"  affects: {', '.join(r['affects'])}")
    return "\n".join(lines).rstrip() + "\n"


def render_one(r: dict, terse: bool = False) -> str:
    if terse:
        lines = [f"ADR-{r['id']} [{r['status']}] {r['title']}"]
        if r.get("context"):
            lines.append(f"context: {r['context']}")
        if r.get("decision"):
            lines.append(f"decision: {r['decision']}")
        if r.get("consequences"):
            lines.append(f"consequences: {r['consequences']}")
        if r.get("affects"):
            lines.append(f"affects: {', '.join(r['affects'])}")
        return "\n".join(lines) + "\n"
    lines = [f"# ADR-{r['id']}: {r['title']}", "", f"Status: {r['status']}"]
    if r.get("date"):
        lines.append(f"Date: {r['date']}")
    lines.append("")
    for label, field in (("Context", "context"), ("Decision", "decision"),
                         ("Consequences", "consequences")):
        if r.get(field):
            lines += [f"## {label}", "", r[field], ""]
    if r.get("affects"):
        lines += ["## Affects", "", ", ".join(r["affects"])]
    return "\n".join(lines).rstrip() + "\n"


def render_for(query: str, records: list[dict], terse: bool = False) -> str:
    if not records:
        return f'_No ADR affects "{query}"._\n'
    if terse:
        lines = [f'adrs for "{query}" ({len(records)})']
        for r in records:
            lines.append(f"ADR-{r['id']} [{r['status']}] {r['title']}")
        return "\n".join(lines) + "\n"
    lines = [f'# ADRs affecting "{query}" — {len(records)}', ""]
    for r in records:
        lines.append(f"## ADR-{r['id']}: {r['title']}  _{r['status']}_")
        if r.get("decision"):
            lines.append(r["decision"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"

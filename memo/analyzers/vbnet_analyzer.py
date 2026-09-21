"""VB.NET analyzer using line + regex heuristics."""

from __future__ import annotations

import re

from .base import (
    FileSummary, Symbol, dedupe_keep_order, first_sentence, line_of, line_of_group,
    scan_comment_markers, vb_block_end,
)

_IMPORTS = re.compile(r"^\s*Imports\s+([A-Za-z_][\w.]*)", re.M | re.I)
_NAMESPACE = re.compile(r"^\s*Namespace\s+([A-Za-z_][\w.]*)", re.M | re.I)

_TYPE = re.compile(
    r"^\s*(?:<[^>]*>\s*)*"  # attributes
    r"(?:(?:Public|Private|Friend|Protected|Partial|MustInherit|NotInheritable|Shadows|\s)*)"
    r"\b(?P<kind>Class|Module|Structure|Interface|Enum)\s+"
    r"(?P<name>[A-Za-z_]\w*)",
    re.M | re.I,
)

_METHOD = re.compile(
    r"^\s*(?:<[^>]*>\s*)*"
    r"(?P<mods>(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|MustOverride|Async|Shadows|\s)+)"
    r"\b(?P<kind>Function|Sub)\s+"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<params>[^)]*)\)"
    r"(?P<ret>[ \t]+As[ \t]+[A-Za-z_][\w<>,.\(\)\[\] \t]*)?",  # single line only
    re.M | re.I,
)


_MODS = (r"(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|"
         r"MustOverride|Shadows|ReadOnly|WriteOnly|Default|Overloads|NotOverridable)")

# `Public Property Name As String` / `Public ReadOnly Property Total As Decimal`
_PROPERTY = re.compile(
    r"^[ \t]*(?:<[^>]*>[ \t]*)*"
    rf"(?P<mods>(?:{_MODS}[ \t]+)+)"
    r"Property[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"(?P<rest>[^\r\n]*)",
    re.M | re.I,
)

# `Public Event OrderPlaced As EventHandler` / `Public Event Progress(ByVal pct As Integer)`
_EVENT = re.compile(
    r"^[ \t]*(?:<[^>]*>[ \t]*)*"
    rf"(?P<mods>(?:{_MODS}[ \t]+)+)"
    r"Event[ \t]+(?P<name>[A-Za-z_]\w*)"
    r"(?P<rest>[^\r\n]*)",
    re.M | re.I,
)

_GET_SET = re.compile(r"^[ \t]*(?:<[^>]*>[ \t]*)*(?:Get|Set)\b", re.I)


def _property_end(lines: list[str], start_line: int) -> int:
    """End line of a VB property, or 0 for an auto-property.

    An auto-property (`Public Property X As String`) has no `End Property` at all, so
    searching for one would run on to the *next* property's terminator and report a
    wildly overlong extent. A block property is identified by a Get/Set on one of the
    following lines.
    """
    for idx in range(start_line, min(start_line + 4, len(lines))):
        stripped = lines[idx].strip()
        if not stripped or stripped.startswith("'"):
            continue
        if _GET_SET.match(lines[idx]):
            return vb_block_end(lines, start_line, "Property")
        break
    return 0


def _preceding_doc(text: str, index: int) -> str:
    """Read a ''' <summary> XML doc immediately preceding position `index`."""
    lines = text[:index].splitlines()
    doc_lines: list[str] = []
    for line in reversed(lines):
        s = line.strip()
        if s.startswith("'''"):
            doc_lines.append(s.lstrip("'").strip())
        elif s.startswith("<") or s == "":
            continue
        else:
            break
    if not doc_lines:
        return ""
    doc = " ".join(reversed(doc_lines))
    m = re.search(r"<summary>(.*?)</summary>", doc, re.S | re.I)
    body = m.group(1) if m else doc
    return first_sentence(re.sub(r"<[^>]+>", " ", body))


def analyze(text: str) -> FileSummary:
    summary = FileSummary(language="vbnet")
    summary.dependencies = dedupe_keep_order(sorted(_IMPORTS.findall(text)))
    # VB has no braces: extents come from matching `End <kind>` lines.
    _lines = text.splitlines()

    ns_match = _NAMESPACE.search(text)
    namespace = ns_match.group(1) if ns_match else None

    for m in _TYPE.finditer(text):
        kind = m.group("kind").capitalize()
        name = m.group("name")
        summary.symbols.append(
            Symbol(kind.lower(), name, f"{kind} {name}",
                   _preceding_doc(text, m.start()), line_of_group(text, m, "name"),
                   vb_block_end(_lines, line_of_group(text, m, "name"), kind))
        )

    for m in _METHOD.finditer(text):
        name = m.group("name")
        mods = " ".join(m.group("mods").split())
        kind = m.group("kind")
        params = " ".join(m.group("params").split())
        ret = (m.group("ret") or "").strip()
        sig = f"{mods} {kind} {name}({params}){(' ' + ret) if ret else ''}".strip()
        summary.symbols.append(
            Symbol("method", name, first_sentence(sig, 160),
                   _preceding_doc(text, m.start()), line_of_group(text, m, "name"),
                   vb_block_end(_lines, line_of_group(text, m, "name"), m.group("kind")))
        )

    method_names = {s.name for s in summary.symbols if s.kind == "method"}

    for m in _EVENT.finditer(text):
        name = m.group("name")
        rest = (m.group("rest") or "").strip()
        summary.symbols.append(
            Symbol("event", name, first_sentence(f"Event {name}{rest}", 160),
                   _preceding_doc(text, m.start()), line_of_group(text, m, "name"))
        )
    event_names = {s.name for s in summary.symbols if s.kind == "event"}

    seen_props: set[str] = set()
    for m in _PROPERTY.finditer(text):
        name = m.group("name")
        if name in method_names or name in event_names or name in seen_props:
            continue
        seen_props.add(name)
        line = line_of_group(text, m, "name")
        rest = (m.group("rest") or "").strip()
        mods = " ".join(m.group("mods").split())
        summary.symbols.append(
            Symbol("property", name,
                   first_sentence(f"{mods} Property {name} {rest}".strip(), 160),
                   _preceding_doc(text, m.start()), line,
                   _property_end(_lines, line))
        )

    if not summary.purpose:
        primary = next((s for s in summary.symbols if s.doc), None)
        if primary:
            summary.purpose = primary.doc
        else:
            type_name = next((s.name for s in summary.symbols if s.kind != "method"), None)
            summary.purpose = (
                f"VB.NET file defining {type_name or 'members'}"
                + (f" in namespace {namespace}" if namespace else "")
                + "."
            )
            summary.purpose_inferred = True

    summary.gotchas = _gotchas(text)
    return summary


def _gotchas(text: str) -> list[str]:
    out: list[str] = []
    if re.search(r"On\s+Error\s+Resume\s+Next", text, re.I):
        out.append("'On Error Resume Next' — silently ignores runtime errors.")
    if re.search(r"On\s+Error\s+GoTo", text, re.I):
        out.append("Legacy 'On Error GoTo' error handling in use.")
    if re.search(r"^\s*GoTo\s+\w", text, re.M | re.I):
        out.append("Uses GoTo — control flow may be hard to follow.")
    if re.search(r"Option\s+Strict\s+Off", text, re.I):
        out.append("'Option Strict Off' — implicit narrowing conversions allowed (runtime risk).")
    if re.search(r"\bThread\.Sleep\s*\(", text, re.I):
        out.append("Thread.Sleep() — blocking delay (argument is milliseconds).")
    if re.search(r"\bDateTime\.Now\b", text, re.I):
        out.append("Uses DateTime.Now (local time) — consider DateTime.UtcNow.")
    if re.search(r"\.(Result|Wait)\b", text):
        out.append("Blocks on a Task (.Result/.Wait) — deadlock risk in async contexts.")
    out.extend(scan_comment_markers(text))
    return dedupe_keep_order(out)[:12]

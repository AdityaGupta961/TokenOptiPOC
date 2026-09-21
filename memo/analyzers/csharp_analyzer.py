"""C# analyzer (ASP.NET Core / general C#) using line + regex heuristics.

Not a full parser — it extracts the things that matter for understanding a file
at a glance: namespace, types, public/protected members (methods *and*
properties), `using` dependencies, and well-known gotchas. XML doc `<summary>`
comments become one-line descriptions when present.

Method detection uses an access-modifier anchor plus balanced-paren matching so
it copes with modern C#: tuple return types (`Task<(A, B)>`), generics,
multi-line signatures, and expression-bodied members (`=>`).
"""

from __future__ import annotations

import re

from .base import (
    FileSummary, Symbol, block_extent_if_braced, brace_extent, dedupe_keep_order,
    first_sentence, line_of, line_of_group, scan_comment_markers,
)

_USING = re.compile(r"^\s*using\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;", re.M)
_USING_ALIAS = re.compile(r"^\s*using\s+\w+\s*=\s*([A-Za-z_][\w.]*)\s*;", re.M)
_NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)", re.M)

_TYPE = re.compile(
    r"^[ \t]*(?:\[[^\]]*\]\s*)*"  # attributes
    r"(?P<mods>(?:public|internal|private|protected|abstract|sealed|static|partial|"
    r"readonly|ref|file|\s)*)"
    # `record class` / `record struct` must precede bare `record`: otherwise the
    # alternation matches `record` and then takes `class`/`struct` as the type NAME.
    r"\b(?P<kind>record[ \t]+class|record[ \t]+struct|class|interface|struct|enum|record)\s+"
    r"(?P<name>[A-Za-z_]\w*)"
    # Bounded to the declaration's own line. `[^\{]*` crossed newlines, so a positional
    # record ending in `;` ran on to the NEXT type's opening brace and swallowed that
    # type's declaration entirely — it was never reported at all.
    r"(?P<rest>[^\{;\r\n]*)",
    re.M,
)

# `public event EventHandler Changed;` / `public event EventHandler<int> Progressed;`
# Part of a type's public surface, and previously invisible to memo.
_EVENT = re.compile(
    r"(?m)^[ \t]*(?:\[[^\]]*\]\s*)*"
    r"(?:public|protected|internal|private)"
    r"(?:\s+(?:static|virtual|override|abstract|sealed|new))*"
    r"\s+event\s+(?P<type>[A-Za-z_][\w<>,.\[\]\?]*)\s+(?P<name>[A-Za-z_]\w*)"
)

# Anchor: a member declaration that starts with an access modifier at line start.
# `decl` runs from the return type up to the closing paren of the parameter list
# (balanced-matched afterwards, so nested/tuple/multi-line params are fine).
_METHOD = re.compile(
    r"(?m)^[ \t]*(?:\[[^\]]*\]\s*)*"
    r"(?P<mods>(?:public|protected|internal|private)"
    r"(?:\s+(?:static|virtual|override|async|sealed|abstract|extern|new|unsafe|partial))*)"
    r"\s+(?P<decl>[A-Za-z_][^;{]*?\))"
    r"\s*(?:where[^{;]*)?\s*(?:=>|\{|;)"  # body brace may sit on its own line (Allman style)
)

# Auto-properties and expression-bodied properties.
_PROPERTY = re.compile(
    r"(?m)^[ \t]*(?:\[[^\]]*\]\s*)*"
    r"(?:public|protected|internal|private)(?:\s+(?:static|virtual|override|new|required|readonly))*"
    r"\s+(?P<type>[A-Za-z_][\w<>,.\[\]\?]*)\s+(?P<name>[A-Za-z_]\w*)\s*"
    r"(?:\{\s*(?:get|set|init)\b|=>)"
)

_CONTROL_WORDS = {
    "if", "for", "foreach", "while", "switch", "using", "lock", "catch",
    "return", "get", "set", "fixed", "unsafe", "do", "else",
}


def _match_open_paren(s: str) -> int:
    """Given a string ending in ')', return the index of its matching '('."""
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        c = s[i]
        if c == ")":
            depth += 1
        elif c == "(":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _preceding_doc(text: str, index: int) -> str:
    """Read an XML-doc <summary> immediately preceding position `index`."""
    head = text[:index]
    lines = head.splitlines()
    doc_lines: list[str] = []
    for line in reversed(lines):
        s = line.strip()
        if s.startswith("///"):
            doc_lines.append(s.lstrip("/").strip())
        elif s.startswith("[") or s == "":
            continue  # skip attributes / blank lines between doc and member
        else:
            break
    if not doc_lines:
        return ""
    doc = " ".join(reversed(doc_lines))
    m = re.search(r"<summary>(.*?)</summary>", doc, re.S | re.I)
    if m:
        return first_sentence(re.sub(r"<[^>]+>", " ", m.group(1)))
    return first_sentence(re.sub(r"<[^>]+>", " ", doc))


def _extract_methods(text: str, type_names: set[str]) -> list[Symbol]:
    out: list[Symbol] = []
    for m in _METHOD.finditer(text):
        decl = m.group("decl")
        op = _match_open_paren(decl)
        if op <= 0:
            continue
        pre = decl[:op]
        # Strip a trailing generic argument list so `Get<T>(` yields name "Get".
        pre_no_generic = re.sub(r"<[^<>]*>\s*$", "", pre)
        nm = re.search(r"([A-Za-z_]\w*)\s*$", pre_no_generic)
        if not nm:
            continue
        name = nm.group(1)
        before_name = pre_no_generic[: nm.start()]
        # A top-level '=' before the name means this is a field initializer, not a method.
        if "=" in before_name.replace("=>", ""):
            continue
        if name in _CONTROL_WORDS:
            continue
        params = " ".join(decl[op + 1 : -1].split())
        mods = " ".join(m.group("mods").split())
        kind = "constructor" if name in type_names else "method"
        sig = first_sentence(f"{mods} {' '.join(decl.split())}", 220)
        # Anchor the line on the method NAME inside `decl`: the pattern's attribute
        # prefix can span its own lines, so m.start() may sit above the declaration.
        name_at = m.start("decl") + nm.start(1)
        start_line = line_of(text, name_at)
        # `m.end()` sits just past the `{`, `=>` or `;` that follows the signature.
        # Only a brace introduces a block to match; `=>`/`;` bodies end on their line.
        end_line = brace_extent(text, m.end() - 1) if text[m.end() - 1] == "{" else 0
        out.append(Symbol(kind, name, sig, _preceding_doc(text, m.start()),
                          start_line, end_line))
    return out


def analyze(text: str) -> FileSummary:
    summary = FileSummary(language="csharp")

    deps = _USING.findall(text) + _USING_ALIAS.findall(text)
    summary.dependencies = dedupe_keep_order(sorted(deps))

    ns_match = _NAMESPACE.search(text)
    namespace = ns_match.group(1) if ns_match else None

    type_names: set[str] = set()
    for m in _TYPE.finditer(text):
        name = m.group("name")
        # "record class"/"record struct" collapse to "record": the distinction is in
        # the signature, and a stable kind keeps downstream ranking simple.
        kind = " ".join(m.group("kind").split())
        if kind.startswith("record"):
            kind = "record"
        rest = (m.group("rest") or "").strip().rstrip("{").strip()
        sig = f"{' '.join(m.group('kind').split())} {name}"
        # Keep the generic parameter list and any base clause: `Repo<T> : IRepo<T>`
        # tells an architect far more than a bare `Repo`. `<` and `(` bind directly to
        # the name; `:` and `where` are separate clauses.
        if rest.startswith(("<", "(")):
            sig += first_sentence(rest, 100)
        elif rest.startswith(":") or rest.startswith("where"):
            sig += " " + first_sentence(rest, 100)
        # Only a `{` opens a block. A positional record ends in `;` and has no body, so
        # brace-matching from here would latch onto the NEXT type's brace and report an
        # extent spanning an unrelated declaration.
        summary.symbols.append(Symbol(kind, name, sig, _preceding_doc(text, m.start()),
                                      line_of_group(text, m, "name"),
                                      block_extent_if_braced(text, m.end())))
        type_names.add(name)

    # Events
    for m in _EVENT.finditer(text):
        summary.symbols.append(
            Symbol("event", m.group("name"),
                   f"event {m.group('type')} {m.group('name')}",
                   _preceding_doc(text, m.start()), line_of_group(text, m, "name"))
        )
    event_names = {s.name for s in summary.symbols if s.kind == "event"}

    method_syms = _extract_methods(text, type_names)
    summary.symbols.extend(method_syms)
    method_names = {s.name for s in method_syms}

    # Properties (skip names already captured as methods/types).
    seen_props: set[str] = set()
    for m in _PROPERTY.finditer(text):
        name = m.group("name")
        if name in method_names or name in type_names or name in seen_props:
            continue
        if name in event_names:
            continue  # `event EventHandler X;` already captured, and isn't a property
        if name in _CONTROL_WORDS:
            continue
        seen_props.add(name)
        summary.symbols.append(
            Symbol("property", name, f"{m.group('type')} {name}",
                   _preceding_doc(text, m.start()), line_of_group(text, m, "name"))
        )

    if not summary.purpose:
        primary = next((s for s in summary.symbols if s.doc), None)
        if primary:
            summary.purpose = primary.doc
        else:
            type_name = next((s.name for s in summary.symbols if s.kind in {"class", "interface", "struct", "enum", "record"}), None)
            summary.purpose = (
                f"C# file defining {type_name or 'members'}"
                + (f" in namespace {namespace}" if namespace else "")
                + "."
            )
            summary.purpose_inferred = True

    summary.gotchas = _gotchas(text)
    return summary


def _gotchas(text: str) -> list[str]:
    out: list[str] = []
    if re.search(r"\[\s*Obsolete", text):
        out.append("Has [Obsolete] members — deprecated API in use.")
    if re.search(r"\basync\s+void\b", text):
        out.append("'async void' method(s) — exceptions can't be awaited/caught (avoid outside event handlers).")
    if re.search(r"\.(Result|GetAwaiter\(\)\.GetResult)\b", text) or re.search(r"\.Wait\(\)", text):
        out.append("Blocks on a Task (.Result/.Wait()/GetResult) — deadlock risk in async contexts.")
    if re.search(r"\bThread\.Sleep\s*\(", text):
        out.append("Thread.Sleep() — blocking delay (argument is milliseconds).")
    if re.search(r"\bDateTime\.Now\b", text):
        out.append("Uses DateTime.Now (local time) — consider DateTime.UtcNow for storage/comparison.")
    if re.search(r"\bGC\.Collect\s*\(", text):
        out.append("Forces GC.Collect() — usually a code smell.")
    if re.search(r"catch\s*(\([^)]*\))?\s*\{\s*\}", text):
        out.append("Empty catch block — swallows exceptions silently.")
    if re.search(r"\bConfigureAwait\s*\(\s*false\s*\)", text):
        out.append("Uses ConfigureAwait(false) — context-free continuations (intentional in libraries).")
    out.extend(scan_comment_markers(text))
    return dedupe_keep_order(out)[:12]

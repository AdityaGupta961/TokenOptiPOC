"""JavaScript / TypeScript / JSX / TSX analyzer (React-aware) via regex heuristics."""

from __future__ import annotations

import re

from .base import (
    FileSummary, Symbol, block_extent_if_braced, brace_extent, dedupe_keep_order,
    first_sentence, line_of, line_of_group, scan_comment_markers,
)

_IMPORT_FROM = re.compile(r"""import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]""")
_REQUIRE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_EXPORT_DEFAULT = re.compile(r"export\s+default\s+(?:function\s+)?([A-Za-z_$][\w$]*)?")

_CLASS = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([\w$.]+))?", re.M)
_FUNC_DECL = re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)", re.M)
# const Foo = (...) => / const Foo = function / const Foo = async (...) =>
_CONST_FN = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::\s*[^=]+)?=\s*"
    r"(?:async\s+)?"
    r"(?:<[^<>()]*>\s*)?"  # TS generic arrow: `const f = <T,>(x: T) => ...`
    r"(?:function\b[^(]*|\(([^)]*)\)\s*(?::[^=]+)?=>|([A-Za-z_$][\w$]*)\s*=>)",
    re.M,
)
_INTERFACE = re.compile(r"^\s*(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)", re.M)

# Legacy function expressions: `Foo.prototype.bar = function(...)`, `x = function`,
# `key: function(...)`. Common in older/vendor JS and prototype-based code.
_FUNC_EXPR = re.compile(
    r"(?:^|[\n;,{])[ \t]*"
    r"(?:(?:var|let|const)\s+)?"
    r"(?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*[:=]\s*"
    r"(?:async\s+)?function\b\s*[A-Za-z_$]*\s*\((?P<params>[^)]*)\)"
)

# ES6 class-method shorthand: `name(params) {` (params may span lines).
_CLASS_METHOD = re.compile(
    r"(?:^|\n)[ \t]*"
    r"(?:(?:public|private|protected|static|async|readonly|abstract|override|get|set)\s+)*"
    r"(?:\*\s*)?"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*"
    r"\((?P<params>[^)]*)\)\s*"
    r"(?::[^={\n]+)?"  # optional return-type annotation
    r"\{"
)
# Class-field arrow methods: `name = (params) => ` (common in React classes).
_CLASS_FIELD_FN = re.compile(
    r"(?:^|\n)[ \t]*"
    r"(?:(?:public|private|protected|static|readonly)\s+)*"
    r"(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=\n]+)?=\s*(?:async\s+)?"
    r"\((?P<params>[^)]*)\)\s*(?::[^=]+)?=>"
)
# Words that look like a method call but are control flow / keywords.
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "function", "return", "else", "do",
    "with", "constructor", "super", "await", "typeof", "new", "in", "of", "case",
    "default", "throw", "break", "continue", "this", "yield", "void", "delete",
}


def _class_body(text: str, from_index: int) -> tuple[str, int]:
    """Return (body, body_start_offset) for a class whose header ends at `from_index`.

    body_start_offset is the absolute index of the first char after the opening
    brace, so callers can map body-relative match positions back to line numbers.
    """
    open_idx = text.find("{", from_index)
    if open_idx == -1:
        return "", -1
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i], open_idx + 1
    return text[open_idx + 1 :], open_idx + 1  # unbalanced — take the rest


def _class_methods(class_name: str, body: str, base: int, text: str) -> list[Symbol]:
    out: list[Symbol] = []
    seen: set[str] = set()
    for rx in (_CLASS_METHOD, _CLASS_FIELD_FN):
        for m in rx.finditer(body):
            nm = m.group("name")
            if nm in _JS_KEYWORDS or nm in seen:
                continue
            seen.add(nm)
            params = " ".join((m.group("params") or "").split())
            line = line_of(text, base + m.start("name")) if base >= 0 else 0
            end = 0
            if base >= 0:
                # _CLASS_METHOD ends on its `{`; _CLASS_FIELD_FN on `=>`.
                end = (brace_extent(text, base + m.end() - 1)
                       if m.group(0).rstrip().endswith("{")
                       else block_extent_if_braced(text, base + m.end()))
            out.append(Symbol("method", f"{class_name}.{nm}", f"{nm}({params})", "",
                              line, end))
    return out


def _preceding_jsdoc(text: str, index: int) -> str:
    """Read a /** ... */ JSDoc block immediately preceding `index`."""
    head = text[:index].rstrip()
    if not head.endswith("*/"):
        return ""
    start = head.rfind("/**")
    if start == -1:
        return ""
    block = head[start + 3 : -2]
    lines = [re.sub(r"^\s*\*?\s?", "", ln).strip() for ln in block.splitlines()]
    desc = " ".join(ln for ln in lines if ln and not ln.startswith("@"))
    return first_sentence(desc)


# A body that *starts* with JSX is an implicit-return arrow: `() => <span />`. Anchored
# with match() rather than search() so it can't be confused with a generic type
# argument (`Array<string>`) appearing later in the body.
_IMPLICIT_JSX = re.compile(r"\s*\(?\s*<[A-Za-z>]")
_EXPLICIT_JSX = re.compile(r"return\s*\(?\s*<|use[A-Z]\w*\(")


def _looks_like_component(name: str, body_slice: str) -> bool:
    """React heuristic: PascalCase name that returns JSX or uses hooks.

    Handles both `=> { ... return <div/> }` and the implicit `=> <div/>` form. The
    latter has no `return` keyword at all and was previously reported as a plain
    function, which is wrong for one of the most common ways to write a component.
    """
    if not name or not name[0].isupper():
        return False
    return bool(_IMPLICIT_JSX.match(body_slice) or _EXPLICIT_JSX.search(body_slice))


def analyze(text: str) -> FileSummary:
    ts = False  # language label set by dispatcher; kept generic here
    summary = FileSummary(language="javascript")

    deps = _IMPORT_FROM.findall(text) + _REQUIRE.findall(text)
    summary.dependencies = dedupe_keep_order(sorted(deps))

    for m in _CLASS.finditer(text):
        name, base = m.group(1), m.group(2)
        sig = f"class {name}" + (f" extends {base}" if base else "")
        kind = "component" if base and "Component" in base else "class"
        summary.symbols.append(Symbol(kind, name, sig, _preceding_jsdoc(text, m.start()),
                                      line_of_group(text, m, 1),
                                      block_extent_if_braced(text, m.end())))
        body, body_start = _class_body(text, m.end())
        summary.symbols.extend(_class_methods(name, body, body_start, text))

    for m in _INTERFACE.finditer(text):
        summary.symbols.append(Symbol("type", m.group(1), m.group(0).strip(), "",
                                      line_of_group(text, m, 1),
                                      block_extent_if_braced(text, m.end())))

    # `start` is the match start (used to find a preceding JSDoc block, which really
    # does sit above the declaration); `name_at` is where the symbol is named, and is
    # what the reported line must follow.
    def add_fn(name: str, params: str, start: int, end: int, name_at: int):
        # Window sized to reach a component's first return. The previous 400 chars was
        # under one screenful: a component that declares a few consts before returning
        # JSX fell outside it and was misreported as a plain function.
        body_slice = text[end : end + 2500]
        kind = "component" if _looks_like_component(name, body_slice) else "function"
        sig = f"{name}({' '.join((params or '').split())})"
        summary.symbols.append(Symbol(kind, name, sig, _preceding_jsdoc(text, start),
                                      line_of(text, name_at),
                                      block_extent_if_braced(text, end)))

    for m in _FUNC_DECL.finditer(text):
        add_fn(m.group(1), m.group(2), m.start(), m.end(), m.start(1))
    for m in _CONST_FN.finditer(text):
        params = m.group(2) or (m.group(3) or "")
        add_fn(m.group(1), params, m.start(), m.end(), m.start(1))
    for m in _FUNC_EXPR.finditer(text):
        name = m.group("name")
        short = name.split(".")[-1]  # last segment for keyword check
        if short in _JS_KEYWORDS:
            continue
        display = name.replace(".prototype.", "#")  # Foo#bar reads clearly
        add_fn(display, m.group("params"), m.start(), m.end(), m.start("name"))

    # De-dupe symbols by name (const-fn + export can double-match).
    seen: set[str] = set()
    summary.symbols = [
        s for s in summary.symbols if not (s.name in seen or seen.add(s.name))
    ]

    dflt = _EXPORT_DEFAULT.search(text)
    if not summary.purpose:
        comp = next((s for s in summary.symbols if s.kind == "component"), None)
        doc = next((s.doc for s in summary.symbols if s.doc), "")
        if doc:
            summary.purpose = doc
        elif comp:
            summary.purpose = f"React component module (exports {comp.name})."
            summary.purpose_inferred = True
        elif dflt and dflt.group(1):
            summary.purpose = f"Module with default export '{dflt.group(1)}'."
            summary.purpose_inferred = True
        else:
            summary.purpose = "JavaScript/TypeScript module."
            summary.purpose_inferred = True

    summary.gotchas = _gotchas(text)
    return summary


def _gotchas(text: str) -> list[str]:
    out: list[str] = []
    if re.search(r"dangerouslySetInnerHTML", text):
        out.append("Uses dangerouslySetInnerHTML — XSS risk if input isn't sanitized.")
    if re.search(r"\beval\s*\(", text):
        out.append("Uses eval() — review for injection risk.")
    if re.search(r"@ts-(ignore|nocheck)", text):
        out.append("Suppresses TypeScript checks (@ts-ignore/@ts-nocheck).")
    if re.search(r":\s*any\b", text):
        out.append("Uses the 'any' type — weakens TypeScript safety.")
    if re.search(r"\buseEffect\s*\(", text) and not re.search(r"\}\s*,\s*\[", text):
        out.append("useEffect without an obvious dependency array — check for missing deps / re-run loops.")
    if re.search(r"==[^=]", text) and not re.search(r"===", text):
        out.append("Uses loose equality (==) — prefer === to avoid coercion surprises.")
    if re.search(r"\bconsole\.(log|debug)\s*\(", text):
        out.append("Leftover console.log/debug statements.")
    if re.search(r"\b(localStorage|sessionStorage)\b", text):
        out.append("Reads/writes Web Storage — values are strings and unencrypted.")
    out.extend(scan_comment_markers(text))
    return dedupe_keep_order(out)[:12]

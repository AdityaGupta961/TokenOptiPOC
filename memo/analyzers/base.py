"""Shared data types and helpers for language analyzers.

Every analyzer takes raw file text and returns a `FileSummary`. The summarizer
then renders that into compact markdown for the cache. Analyzers must never
invent information: if something can't be inferred from the source, it is left
out (empty lists / a plain "inferred from structure" purpose).
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field


@dataclass
class Symbol:
    kind: str  # e.g. "class", "function", "method", "component", "interface"
    name: str
    signature: str = ""  # short signature or qualifier line
    doc: str = ""  # one-line description, only if present in source
    line: int = 0  # 1-based line where the symbol is declared (0 = unknown)
    # 1-based last line of the symbol's body (0 = unknown, fall back to a heuristic).
    # Without this, a symbol's extent had to be guessed as "up to the next symbol's
    # line", which collapses any container to a single line: a class followed by its
    # own methods appeared to end before its first method. `peek` then showed a
    # truncated class body, and callers were attributed to the wrong enclosing symbol.
    end_line: int = 0


@dataclass
class FileSummary:
    language: str
    purpose: str = ""
    purpose_inferred: bool = False  # True when purpose was derived, not doc-stated
    symbols: list[Symbol] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    gotchas: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "purpose": self.purpose,
            "purpose_inferred": self.purpose_inferred,
            "symbols": [vars(s) for s in self.symbols],
            "dependencies": self.dependencies,
            "gotchas": self.gotchas,
        }


# --- text helpers shared across the heuristic (non-Python) analyzers --------

def line_of(text: str, index: int) -> int:
    """1-based line number of character offset `index` in `text`.

    Fine for a handful of lookups. For many lookups in one file use `line_starts()` +
    `line_at()` instead: this counts newlines from the start of the file every time, so
    repeated use is quadratic in file size (measured: 821 ms to resolve 3,019 call sites
    in one 842 KB file, versus 8 ms precomputed).
    """
    return text.count("\n", 0, index) + 1


def line_starts(text: str) -> list[int]:
    """Offsets of the first character of each line, for repeated line lookups."""
    starts = [0]
    j = text.find("\n")
    while j != -1:
        starts.append(j + 1)
        j = text.find("\n", j + 1)
    return starts


def line_at(starts: list[int], index: int) -> int:
    """1-based line number of `index`, given `starts` from `line_starts()`."""
    return bisect_right(starts, index)


def line_of_group(text: str, match, group) -> int:
    """1-based line of a match's *name* group, falling back to the match start.

    Prefer this over `line_of(text, m.start())` for declaration patterns. Several of
    them begin with `^\\s*` or an attribute-list prefix, and `\\s` matches newlines —
    so when a declaration is preceded by a blank line or an attribute on its own line,
    the match starts a line (or more) above the declaration. That produced reported
    line numbers one too small.

    This matters more here than in a typical parser: memo instructs agents to cite its
    `file:line` directly and *not* re-read the file to confirm, so a stale coordinate
    is silently believed. Anchoring on the name group keeps the line pointing at the
    text the symbol is actually named on, whatever the pattern consumed before it.
    """
    try:
        i = match.start(group)
    except (IndexError, re.error):
        i = -1
    return line_of(text, i if i >= 0 else match.start())


def first_sentence(text: str, max_len: int = 220) -> str:
    """Collapse whitespace and return roughly the first sentence."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    m = re.search(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    return (out[: max_len - 1] + "…") if len(out) > max_len else out


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _skip_quoted(text: str, i: int) -> int:
    """Advance past the string/char literal starting at `i`. Returns the index after it."""
    quote = text[i]
    n = len(text)
    # C# verbatim (@"..") and raw strings don't honour backslash escapes; a doubled
    # quote is the escape. Treating \ as literal here is the safe reading for both.
    verbatim = i > 0 and text[i - 1] == "@"
    i += 1
    while i < n:
        c = text[i]
        if c == "\\" and not verbatim:
            i += 2
            continue
        if c == quote:
            if verbatim and i + 1 < n and text[i + 1] == quote:
                i += 2  # "" escape inside a verbatim string
                continue
            return i + 1
        if c == "\n" and not verbatim:
            return i  # unterminated literal; don't run past the line
        i += 1
    return n


def brace_close_index(text: str, from_index: int) -> int:
    """Index of the `}` closing the block that opens at/after `from_index`, or -1.

    Comments and string literals are skipped, because braces inside them (`"{0}"`,
    `// }`) would otherwise unbalance the count and hand back a wildly wrong extent —
    worse than admitting we don't know.
    """
    i = text.find("{", from_index)
    if i == -1:
        return -1
    n = len(text)
    depth = 0
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i)
                i = n if j == -1 else j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue
        if c in ('"', "'", "`"):  # ` = JS/TS template literal
            i = _skip_quoted(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def brace_extent(text: str, from_index: int) -> int:
    """1-based line of the `}` closing the block that opens at/after `from_index`.

    Returns 0 when no balanced block is found, so callers can fall back to the
    next-symbol heuristic.
    """
    i = brace_close_index(text, from_index)
    return line_of(text, i) if i >= 0 else 0


def block_extent_if_braced(text: str, index: int) -> int:
    """`brace_extent` only when a `{` block actually starts at/after `index`.

    Distinguishes a braced body from an expression body: `() => { ... }` has a block,
    `() => <div/>` does not. Blindly calling brace_extent on the latter would latch
    onto some unrelated later block and report a nonsense extent.
    """
    n = len(text)
    j = index
    while j < n and text[j] in " \t\r\n":
        j += 1
    return brace_extent(text, j) if j < n and text[j] == "{" else 0


# VB.NET block terminators, by the keyword that opened the block.
_VB_END = re.compile(
    r"^\s*End\s+(?P<kind>Sub|Function|Class|Module|Structure|Interface|Enum|Property)\b",
    re.I)
_VB_OPEN = re.compile(
    r"^\s*(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|"
    r"MustOverride|Async|Shadows|Partial|MustInherit|NotInheritable|Default|Iterator)\s+)*"
    r"(?P<kind>Sub|Function|Class|Module|Structure|Interface|Enum|Property)\b",
    re.I)


def vb_block_end(lines: list[str], start_line: int, kind: str) -> int:
    """1-based line of the `End <kind>` closing a VB block that starts at `start_line`.

    VB has no braces, so extents come from matching End statements. Nesting is tracked
    (a Class contains Subs) so an inner `End Sub` doesn't close the outer Class.
    Returns 0 if unbalanced.
    """
    want = kind.lower()
    depth = 0
    for idx in range(start_line - 1, len(lines)):
        line = lines[idx]
        # A single-line `Declare Sub`/abstract `MustOverride Sub` has no End at all.
        om = _VB_OPEN.match(line)
        if om and idx >= start_line - 1:
            if re.match(r"^\s*(?:.*\b)?(?:Declare|MustOverride)\b", line, re.I):
                pass  # no body to close
            else:
                depth += 1
        em = _VB_END.match(line)
        if em:
            depth -= 1
            if depth <= 0:
                return idx + 1 if em.group("kind").lower() == want or depth == 0 else 0
    return 0


# --- call-argument and parameter counting ----------------------------------
#
# Used to score name-based call edges: a site passing three arguments probably isn't
# calling the 1-parameter method of that name. Everything here is allowed to answer
# "unknown" (-1) and must do so rather than guess, because a wrong count is read as
# positive evidence downstream while an unknown one is read as no evidence.

UNKNOWN_ARITY = -1
VARIADIC_MAX = 99  # `params` / `ParamArray` / `...rest` / `*args`

PF_EXTENSION = 1  # C# extension method: first parameter is `this X`
PF_UNPARSED = 2  # signature had no readable parameter list

# Comment syntax per language family. VB's `'` is handled here rather than by
# `_skip_quoted`, which would read it as a char literal and swallow to the next quote.
_LINE_COMMENTS = {
    "python": ("#",), "vbnet": ("'", "REM "), "csharp": ("//",),
    "javascript": ("//",), "typescript": ("//",), "markup": ("//",),
}
_BLOCK_COMMENTS = {"csharp", "javascript", "typescript", "markup"}


def scan_arg_counts(text: str, language: str = "", max_span: int = 4000) -> dict[int, int]:
    """Map each `(` offset to the number of arguments in its group; one pass.

    Returns `{open_paren_offset: argc}`, where `argc` is UNKNOWN_ARITY when the group
    never closed, ran past `max_span`, or held a spread/splat (`f(*a)`, `f(...xs)`) whose
    real count can't be known. Groups absent from the mapping are unknown by omission.

    One left-to-right pass, not a balanced scan per call site. That is not a
    micro-optimisation: this file's history records what re-walking costs on real input —
    `line_of` to `line_at` was 821 ms versus 8 ms for 3,019 call sites in one 842 KB file,
    and `_enclosing` runs 174,674 times on a 1,433-file repo. Argument counting has the
    same shape, so it is precomputed per file exactly once.

    Commas are attributed to the innermost open group, so `f(a, x => { g(1,2); })` counts
    2 for `f` and not 3, and `f(new Dictionary<string,int>())` counts 1. Only parens
    produce entries; `[`, `{` and `<` are tracked solely to keep their commas out.

    `<` counts as a generic bracket only directly after an identifier, so the spaced
    relational in `while (i < n)` is left alone while `List<int>` is not. When a group's
    angles don't balance by the time it closes, its count is reported unknown rather than
    guessed — `f(a<b, c)` is genuinely ambiguous without a type table.

    Deliberately *not* used to decide whether an edge exists. Discovery still runs over
    raw text, so `$"{Fmt(x)}"` keeps its edge for `Fmt`; skipping strings here costs only
    the count. Calls inside an interpolated hole are therefore unknown rather than
    counted — recursing into `{...}` would recover them, and is worth doing only if those
    sites turn out to matter.
    """
    line_marks = _LINE_COMMENTS.get(language, ("//",))
    block = (not language) or language in _BLOCK_COMMENTS
    out: dict[int, int] = {}
    # Each frame: [open_offset_or_None, commas, saw_content, ok, angle_depth]. `ok` goes
    # False once the group is known to be uncountable; it still has to be popped by its
    # closer.
    stack: list[list] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]

        if c in ('"', "'", "`"):
            # VB comments start with a quote character; treat them as comment, not string.
            if c == "'" and "'" in line_marks:
                j = text.find("\n", i)
                i = n if j == -1 else j
                continue
            if stack:
                stack[-1][2] = True
            i = _skip_quoted(text, i)
            continue

        if block and c == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                j = text.find("\n", i)
                i = n if j == -1 else j
                continue
            if nxt == "*":
                j = text.find("*/", i + 2)
                i = n if j == -1 else j + 2
                continue

        if any(c == m[0] and text.startswith(m, i) for m in line_marks if m != "'"):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue

        if c in "([{":
            stack.append([i if c == "(" else None, 0, False, True, 0])
        elif c in ")]}":
            if stack:
                start, commas, content, ok, angle = stack.pop()
                if start is not None:
                    span_ok = ok and angle == 0 and (i - start) <= max_span
                    out[start] = (commas + 1 if content else 0) if span_ok else UNKNOWN_ARITY
        elif c == "<" and stack and i and (text[i - 1].isalnum() or text[i - 1] in "_$>"):
            stack[-1][4] += 1
            stack[-1][2] = True
        elif c == ">" and stack and stack[-1][4] and text[i - 1] not in "=-":
            stack[-1][4] -= 1
        elif c == ",":
            if stack:
                if not stack[-1][4]:  # a comma inside `<...>` separates type arguments
                    stack[-1][1] += 1
                stack[-1][2] = True
        elif c == "*" or (c == "." and text.startswith("...", i)):
            # A splat/spread argument hides the real count. Mark the enclosing group
            # uncountable rather than reporting a number that is wrong.
            if stack and stack[-1][0] is not None:
                stack[-1][3] = False
                stack[-1][2] = True
            i += 3 if c == "." else 1
            continue
        elif not c.isspace():
            if stack:
                stack[-1][2] = True
        i += 1
    return out


def split_params(params_text: str) -> list[str]:
    """Split a parameter list on its top-level commas.

    Depth-aware over `()`, `[]`, `{}` and `<>`, and string-aware, so
    `int x, string y = "a,b", params object[] rest` yields three entries and
    `Dictionary<string,int> m, Func<int,int> f` yields two.

    Angle brackets are the awkward case: they nest like brackets in `Func<int,int>` but
    appear unpaired in a relational default. When the angle depth doesn't return to zero
    the split is redone ignoring `<`/`>` entirely, which is right for the unpaired case
    and no worse than a plain split for anything else.
    """
    for track_angles in (True, False):
        parts: list[str] = []
        buf: list[str] = []
        depth = angle = 0
        i, n = 0, len(params_text)
        while i < n:
            c = params_text[i]
            if c in ('"', "'", "`"):
                j = _skip_quoted(params_text, i)
                buf.append(params_text[i:j])
                i = j
                continue
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
            elif track_angles and c == "<":
                angle += 1
            elif track_angles and c == ">":
                angle -= 1
            elif c == "," and depth == 0 and angle == 0:
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
            buf.append(c)
            i += 1
        if not track_angles or angle == 0:
            parts.append("".join(buf))
            return [p.strip() for p in parts if p.strip()]
    return []


def _param_list(signature: str) -> str | None:
    """The text inside a signature's parameter list, or None if there isn't a readable one.

    Takes the first balanced `(...)` group that is *not* inside angle brackets. Scanning
    for the first `(` outright would pick the tuple in
    `Task<(int,string)> B(Dictionary<string,int> m)`; taking everything up to the last `)`
    would swallow the constraint in `void Foo(int x) where T : new()`.
    """
    angle = depth = 0
    start = -1
    i, n = 0, len(signature)
    while i < n:
        c = signature[i]
        if c in ('"', "'", "`"):
            i = _skip_quoted(signature, i)
            continue
        if c == "<":
            angle += 1
        elif c == ">":
            angle = max(0, angle - 1)
        elif c == "(" and angle == 0:
            if depth == 0:
                start = i
            depth += 1
        elif c == ")" and angle == 0 and depth:
            depth -= 1
            if depth == 0:
                return signature[start + 1:i]
        i += 1
    return None


_VB_OPTIONAL = re.compile(r"^\s*Optional\b", re.I)
_VB_PARAMARRAY = re.compile(r"^\s*ParamArray\b", re.I)
_VB_BYREFVAL = re.compile(r"^\s*(?:ByVal|ByRef)\s+", re.I)
_TS_OPTIONAL = re.compile(r"[\w\]]\s*\?\s*:")


def param_arity(signature: str, language: str, kind: str) -> tuple[int, int, int]:
    """`(pmin, pmax, pflags)` declared by `signature`, or unknown.

    Only meaningful for callables, so anything else returns unknown rather than reading a
    class's base list as parameters. Python is deliberately excluded: its analyzer builds
    signatures from `fn.args.args` alone, dropping defaults, keyword-only and
    positional-only parameters, so `def build(entries, cache_root=None)` renders as
    `def build(entries, cache_root)` — which would mark memo's own `build(entries)` call a
    contradiction. Python arity needs an AST-derived count, not this string.
    """
    if kind not in ("method", "function", "constructor", "component"):
        return UNKNOWN_ARITY, UNKNOWN_ARITY, PF_UNPARSED
    if language == "python":
        return UNKNOWN_ARITY, UNKNOWN_ARITY, PF_UNPARSED

    inner = _param_list(signature or "")
    if inner is None:
        return UNKNOWN_ARITY, UNKNOWN_ARITY, PF_UNPARSED
    params = split_params(inner)
    if not params:
        return 0, 0, 0

    flags = pmin = 0
    variadic = False
    for idx, raw in enumerate(params):
        p = _VB_BYREFVAL.sub("", raw).strip()
        low = p.lower()
        if idx == 0 and (low.startswith("this ") or low.startswith("this\t")):
            flags |= PF_EXTENSION
        if (language == "vbnet" and _VB_PARAMARRAY.match(p)) or \
                low.startswith("params ") or p.startswith("..."):
            variadic = True
            continue
        optional = "=" in p
        if language == "vbnet":
            optional = optional or bool(_VB_OPTIONAL.match(p))
        elif language in ("typescript", "javascript"):
            optional = optional or bool(_TS_OPTIONAL.search(p))
        if not optional:
            pmin += 1
    pmax = VARIADIC_MAX if variadic else len(params)
    return pmin, pmax, flags


def arity_fits(argc: int, pmin: int, pmax: int, pflags: int = 0) -> bool | None:
    """Whether `argc` arguments can reach a `[pmin, pmax]` parameter list.

    None means "no opinion" — either side unknown. Callers must treat None as absence of
    evidence, never as a mismatch.

    An extension method accepts one fewer argument than it declares, because the receiver
    fills the leading `this` parameter: `public static void Ext(this Svc s, int q)` is
    called as `svc.Ext(1)`.
    """
    if argc < 0 or pmin < 0 or pmax < 0:
        return None
    if pmin <= argc <= pmax:
        return True
    if pflags & PF_EXTENSION and pmin - 1 <= argc <= max(pmax - 1, 0):
        return True
    return False


# --- declared types --------------------------------------------------------
#
# The one thing that separates same-named methods on different types, which arity and
# receiver names cannot: `_processor.HandleException(m)` is unresolvable until you know
# `_processor` was declared `ICaseProcessor`. C# and VB write that down in source, so it
# is recoverable without type inference.

# `[modifiers] Type name` followed by end-of-declaration. The lookahead deliberately
# excludes `(`: with it, `IActionResult Create(OrderDto dto)` would map the *method*
# `Create` to its return type.
_CS_DECL = re.compile(
    r"\b([A-Z][\w]*(?:<[^<>;()\n]*>)?(?:\[\])?)\s+([_a-zA-Z][\w]*)\s*(?=[;=,){])")
# `x = new Foo(` — covers `var x = new Foo()`, where the declaration carries no type name.
_CS_NEW = re.compile(r"([_a-zA-Z]\w*)\s*=\s*new\s+([A-Z][\w]*)")
_VB_AS = re.compile(r"\b([_a-zA-Z]\w*)\s+As\s+([A-Za-z_][\w.]*)", re.I)
_VB_NEW = re.compile(r"\b([_a-zA-Z]\w*)\s*=\s*New\s+([A-Za-z_][\w.]*)", re.I)
# TS annotations: `private svc: OrderService`, `(a: Foo, b: Bar)`, `const x: Foo`.
_TS_ANNOT = re.compile(r"([_a-zA-Z$][\w$]*)\s*:\s*([A-Z][\w]*)")
_TS_NEW = re.compile(r"([_a-zA-Z$][\w$]*)\s*=\s*new\s+([A-Z][\w]*)")

_DECL_PATTERNS = {
    "csharp": ((_CS_DECL, 2, 1), (_CS_NEW, 1, 2)),
    "vbnet": ((_VB_AS, 1, 2), (_VB_NEW, 1, 2)),
    "typescript": ((_TS_ANNOT, 1, 2), (_TS_NEW, 1, 2)),
    "javascript": ((_TS_NEW, 1, 2),),
}


def declared_types(text: str, language: str) -> dict[str, str]:
    """Map identifiers to the type name they were declared with, per file.

    Covers fields, properties, parameters, explicitly typed locals and `= new T(`. Only
    the bare type name is kept: `List<Order>` collapses to `List`, since the container of
    a method is a plain name.

    Scoped per file rather than per block, which is what makes it cheap. The cost is that
    two methods can declare the same name as different types — so **a name with
    conflicting types is dropped entirely**. Unknown loses a resolution; wrong sends
    `calls` and `trace` to another class, and memo's callers are told not to re-check.

    File scope is also why this stays in the content-hash shard: it reads only this file.
    A field declared in another part of a `partial class` is therefore invisible, which
    costs recall on a pattern that is rare outside generated code.
    """
    patterns = _DECL_PATTERNS.get(language)
    if not patterns:
        return {}
    out: dict[str, str] = {}
    conflicted: set[str] = set()
    for rx, name_grp, type_grp in patterns:
        for m in rx.finditer(text):
            name = m.group(name_grp)
            typ = m.group(type_grp).split("<")[0].rsplit(".", 1)[-1].replace("[]", "")
            if not typ or not name:
                continue
            prev = out.get(name)
            if prev is not None and prev != typ:
                conflicted.add(name)
            else:
                out[name] = typ
    for name in conflicted:
        out.pop(name, None)
    return out


_BASE_LIST = re.compile(r":\s*(.+)$")


def base_types(signature: str, language: str) -> list[str]:
    """The types a class signature inherits from or implements.

    C# and TypeScript put them in the declaration — `class Svc : BaseSvc, ICaseProcessor`
    — which the analyzers already keep verbatim in `signature`. That is real source
    information, and it is what lets a receiver declared as an interface resolve to the
    class that implements it, rather than to the interface's own declaration.
    """
    if language in ("csharp", "typescript", "javascript"):
        m = _BASE_LIST.search(signature or "")
        if not m:
            return []
        parts = split_params(m.group(1).replace(" where ", ";"))
        return [p.split("<")[0].split()[0].rsplit(".", 1)[-1]
                for p in parts if p and p[0].isalpha()]
    return []


# Common markers that legitimately signal a "gotcha" when found in comments.
COMMENT_MARKERS = re.compile(r"\b(TODO|FIXME|HACK|XXX|BUG|DEPRECATED|WORKAROUND)\b", re.I)


def scan_comment_markers(text: str) -> list[str]:
    """Collect distinct TODO/FIXME/etc. markers with a short excerpt each."""
    out: list[str] = []
    for line in text.splitlines():
        m = COMMENT_MARKERS.search(line)
        if m:
            excerpt = first_sentence(line.strip().lstrip("/*#'<!->").strip(), 120)
            out.append(f"{m.group(1).upper()} comment: {excerpt}")
    return dedupe_keep_order(out)[:8]

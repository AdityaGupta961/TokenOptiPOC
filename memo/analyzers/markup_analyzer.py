"""Analyzer for classic ASP.NET markup: .aspx/.ascx/.ashx/.asmx/.master + Razor.

Focuses on the page directive (code-behind wiring), registered/imported
namespaces, notable server controls, and inline server-code gotchas.
"""

from __future__ import annotations

import re

from .base import (
    FileSummary, Symbol, brace_close_index, dedupe_keep_order, first_sentence, line_of,
    scan_comment_markers,
)

# Razor / Blazor server-code blocks: `@code { ... }` (and the older `@functions`).
# Everything a .razor component actually *does* lives in here, so a page whose code
# block wasn't parsed produced a summary with no symbols at all.
_RAZOR_CODE = re.compile(r"@(?:code|functions)\s*\{", re.I)

# MVC-style Razor views carry no page directive and often no code block at all, so they
# previously yielded zero symbols and the purpose "ASP.NET markup fragment". What they DO
# declare is their model, their injected services, their named sections, and — most
# usefully for routing — the partials and actions they pull in, which are real edges
# between views that nothing else in memo could see.
_RAZOR_MODEL = re.compile(r"^[ \t]*@model[ \t]+(?P<type>[\w<>,.\[\]\?]+)", re.M)
_RAZOR_INHERITS = re.compile(r"^[ \t]*@inherits[ \t]+(?P<type>[\w<>,.\[\]\?]+)", re.M)
_RAZOR_USING = re.compile(r"^[ \t]*@using[ \t]+(?P<ns>[\w.]+)", re.M)
_RAZOR_INJECT = re.compile(
    r"^[ \t]*@inject[ \t]+(?P<type>[\w<>,.\[\]\?]+)[ \t]+(?P<name>\w+)", re.M)
_RAZOR_SECTION = re.compile(r"@section[ \t]+(?P<name>\w+)", re.I)
# `@Html.Partial("_Foo")`, `RenderPartial`, `PartialAsync`, `@await Html.RenderPartialAsync`
_RAZOR_PARTIAL = re.compile(
    r"""(?:Partial|RenderPartial|PartialAsync|RenderPartialAsync)\s*\(\s*["']([^"']+)["']""")
# `@Html.Action("Act","Ctrl")` / `@Url.Action("Act")` — controller-action references
_RAZOR_ACTION = re.compile(
    r"""(?:Html|Url)\.(?:Action|ActionLink|RenderAction)\s*\(\s*["']([^"']+)["']"""
    r"""(?:\s*,\s*["']([^"']+)["'])?""")

_DIRECTIVE = re.compile(r"<%@\s*(?P<kind>\w+)\s+(?P<attrs>[^%]*?)%>", re.S)
_ATTR = re.compile(r"""(\w+)\s*=\s*["']([^"']*)["']""")
_REGISTER = re.compile(r"""<%@\s*Register\b[^%]*?(?:TagPrefix|Namespace|Assembly)\s*=\s*["']([^"']+)["']""", re.I)
# Any opening tag carrying runat="server"; ID may appear before or after runat.
_SERVER_TAG = re.compile(r"""<([A-Za-z][\w:]*)\b([^>]*?\brunat\s*=\s*["']server["'][^>]*?)/?>""", re.I | re.S)
_ID_ATTR = re.compile(r"""\bID\s*=\s*["']([^"']+)["']""", re.I)
_INLINE_SCRIPT = re.compile(r"""<script[^>]*runat\s*=\s*["']server["'][^>]*>(.*?)</script>""", re.I | re.S)


def _parse_attrs(attr_str: str) -> dict:
    return {k.lower(): v for k, v in _ATTR.findall(attr_str)}


def _razor_code_symbols(text: str) -> list[Symbol]:
    """Symbols declared inside `@code { ... }` blocks, with line numbers rebased.

    Delegates to the real C# analyzer rather than re-deriving a regex here: the block
    contains ordinary C# (methods, properties, lifecycle overrides), and duplicating
    that parsing would mean two places to keep correct.
    """
    from .csharp_analyzer import analyze as analyze_csharp

    out: list[Symbol] = []
    for m in _RAZOR_CODE.finditer(text):
        open_idx = text.find("{", m.start())
        close_idx = brace_close_index(text, m.start())
        if open_idx == -1 or close_idx <= open_idx:
            continue
        body = text[open_idx + 1: close_idx]
        # The body's line 1 is the remainder of the line holding `{`, so the offset is
        # that line number minus one.
        offset = line_of(text, open_idx) - 1
        for s in analyze_csharp(body).symbols:
            s.line += offset
            if s.end_line:
                s.end_line += offset
            out.append(s)
    return out


def _razor_symbols(text: str) -> tuple[list[Symbol], list[str], str]:
    """Razor view surface: (symbols, extra dependencies, model type or "").

    Covers the MVC view shape — `@model`/`@inject`/`@section`/partials/actions — which
    is distinct from the Blazor `@code` shape handled by `_razor_code_symbols`.
    """
    syms: list[Symbol] = []
    deps: list[str] = []

    model = ""
    m = _RAZOR_MODEL.search(text)
    if m:
        model = m.group("type")
        deps.append(model)
        syms.append(Symbol("model", model, f"@model {model}", "",
                           line_of(text, m.start("type"))))

    m = _RAZOR_INHERITS.search(text)
    if m:
        deps.append(m.group("type"))

    deps.extend(_RAZOR_USING.findall(text))

    for m in _RAZOR_INJECT.finditer(text):
        deps.append(m.group("type"))
        syms.append(Symbol("injected", m.group("name"),
                           f"@inject {m.group('type')} {m.group('name')}", "",
                           line_of(text, m.start("name"))))

    seen: set[str] = set()
    for m in _RAZOR_SECTION.finditer(text):
        name = m.group("name")
        if name in seen:
            continue
        seen.add(name)
        syms.append(Symbol("section", name, f"@section {name}", "",
                           line_of(text, m.start("name"))))

    # Partials and actions become dependencies rather than symbols: this view *uses*
    # them, it does not define them. That puts them where `map`/`find` can route on them.
    deps.extend(_RAZOR_PARTIAL.findall(text))
    for act, ctrl in _RAZOR_ACTION.findall(text):
        deps.append(f"{ctrl}/{act}" if ctrl else act)

    return syms, deps, model


# Classic-ASP server-side include: `<!-- #include file="x.asp" -->` or `virtual="/x.asp"`.
# This is *the* composition mechanism in classic ASP — there are no imports and no module
# system, so a page's real behaviour is assembled from its include chain. Without this,
# tracing "does this page run SecurityCheck.asp?" means opening every file by hand, which
# is exactly what happened on a real investigation.
_SSI_INCLUDE = re.compile(
    r"<!--+\s*#include\s+(?:file|virtual)\s*=\s*[\"']([^\"']+)[\"']", re.I)


def _ssi_includes(text: str) -> list[str]:
    """Included file paths, normalised to forward slashes."""
    return [p.replace("\\", "/").strip() for p in _SSI_INCLUDE.findall(text)]


def analyze(text: str) -> FileSummary:
    summary = FileSummary(language="markup")

    directives = list(_DIRECTIVE.finditer(text))
    page_attrs: dict = {}
    directive_kind = None
    for d in directives:
        kind = d.group("kind")
        attrs = _parse_attrs(d.group("attrs"))
        if kind.lower() in {"page", "control", "master", "webhandler", "webservice"}:
            directive_kind = kind
            page_attrs = attrs

    razor_syms, razor_deps, razor_model = _razor_symbols(text)

    # Purpose derived from the directive.
    if directive_kind:
        code_behind = page_attrs.get("codebehind") or page_attrs.get("codefile")
        inherits = page_attrs.get("inherits")
        lang = page_attrs.get("language")
        parts = [f"ASP.NET {directive_kind}"]
        if lang:
            parts.append(f"({lang})")
        if inherits:
            parts.append(f"bound to code-behind class '{inherits}'")
        elif code_behind:
            parts.append(f"with code-behind '{code_behind}'")
        summary.purpose = first_sentence(" ".join(parts) + ".")
    elif razor_model or razor_syms:
        # An MVC Razor view has no page directive; naming its model is far more use than
        # calling it an anonymous "markup fragment".
        summary.purpose = (f"Razor view bound to model '{razor_model}'."
                           if razor_model else "Razor view (no model declared).")
    else:
        summary.purpose = "ASP.NET markup fragment (no page directive found)."
    summary.purpose_inferred = True

    # Dependencies: registered tag prefixes / imported namespaces / assemblies, plus
    # classic-ASP server-side includes.
    deps: list[str] = _REGISTER.findall(text) + razor_deps + _ssi_includes(text)
    for d in directives:
        if d.group("kind").lower() == "import":
            attrs = _parse_attrs(d.group("attrs"))
            if attrs.get("namespace"):
                deps.append(attrs["namespace"])
    summary.dependencies = dedupe_keep_order(sorted(deps))

    # Symbols: server controls that have an ID, plus inline server functions.
    unnamed = 0
    for m in _SERVER_TAG.finditer(text):
        tag = m.group(1)
        id_match = _ID_ATTR.search(m.group(2))
        if id_match:
            cid = id_match.group(1)
            # Anchor on the ID attribute, not the opening `<asp:Label`. Server tags in
            # real .aspx routinely span several lines, so the tag start often holds no
            # trace of the control's name — an agent told to trust that coordinate lands
            # on a line that doesn't mention what it was looking for.
            summary.symbols.append(Symbol(
                "control", cid, f"<{tag}> id={cid}", "",
                line_of(text, m.start(2) + id_match.start(1)),
            ))
        else:
            unnamed += 1

    for m in _INLINE_SCRIPT.finditer(text):
        # Offsets from fn are relative to the captured *body*, group 1 — not to the
        # match, which begins at the `<script runat="server">` tag. Adding m.start()
        # instead of m.start(1) put every inline handler a whole tag too early (a
        # <script> near the top of a page reported line 1).
        body_at = m.start(1)
        for fn in re.finditer(
                r"\b(?:Sub|Function|void|public|private|protected)\s+([A-Za-z_]\w*)\s*\(",
                m.group(1)):
            summary.symbols.append(
                Symbol("method", fn.group(1), "inline server-side handler", "",
                       line_of(text, body_at + fn.start(1)))
            )

    summary.symbols.extend(razor_syms)
    summary.symbols.extend(_razor_code_symbols(text))

    summary.gotchas = _gotchas(text)
    if unnamed:
        summary.gotchas.append(f'{unnamed} runat="server" element(s) without an ID.')
    return summary


def _gotchas(text: str) -> list[str]:
    out: list[str] = []
    if re.search(r"<%(?![@=])", text):
        out.append("Contains inline server code blocks (<% %>) — logic mixed into markup.")
    if re.search(r"<%=", text):
        out.append("Uses <%= %> render expressions — verify output is HTML-encoded (XSS).")
    if re.search(r"Response\.Write\s*\(", text, re.I):
        out.append("Direct Response.Write in markup — encoding is your responsibility.")
    if re.search(r"EnableViewState\s*=\s*[\"']?false", text, re.I):
        out.append("ViewState disabled somewhere — server-control state may not persist across postbacks.")
    if re.search(r"ValidateRequest\s*=\s*[\"']?false", text, re.I):
        out.append("ValidateRequest=false — request validation disabled (raises injection risk).")
    if re.search(r"AutoEventWireup\s*=\s*[\"']?false", text, re.I):
        out.append("AutoEventWireup=false — page lifecycle events must be wired manually.")
    out.extend(scan_comment_markers(text))
    return dedupe_keep_order(out)[:12]

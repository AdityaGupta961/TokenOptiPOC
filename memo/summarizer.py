"""Dispatch a file to the right language analyzer and render a compact summary.

The rendered markdown is what you paste into an AI assistant. It is deliberately
terse: purpose, key symbols (with one-line docs when the source had them),
dependencies that matter, and inferable gotchas. Nothing is invented — if the
analyzer found nothing for a section, that section is omitted.
"""

from __future__ import annotations

from pathlib import Path

from .analyzers import (
    csharp_analyzer,
    generic_analyzer,
    js_ts_analyzer,
    markup_analyzer,
    python_analyzer,
    vbnet_analyzer,
)
from .analyzers.base import FileSummary

_DISPATCH = {
    "python": python_analyzer.analyze,
    "csharp": csharp_analyzer.analyze,
    "vbnet": vbnet_analyzer.analyze,
    "javascript": js_ts_analyzer.analyze,
    "typescript": js_ts_analyzer.analyze,
    "markup": markup_analyzer.analyze,
    "generic": generic_analyzer.analyze,
}

_KIND_ORDER = {
    "class": 0, "component": 0, "module": 0, "interface": 1, "struct": 1,
    "record": 1, "enum": 1, "type": 2, "function": 3, "method": 4, "control": 5,
}


def summarize(text: str, language: str) -> FileSummary:
    analyze = _DISPATCH.get(language, generic_analyzer.analyze)
    fs = analyze(text)
    fs.language = language  # keep the caller's precise label (e.g. typescript)
    return fs


def render_markdown(fs: FileSummary, path: Path, raw_tokens: int, summary_tokens: int) -> str:
    lines: list[str] = []
    lines.append(f"# Summary: {path.name}")
    lines.append("")
    lines.append(f"- **Language:** {fs.language}")
    lines.append(f"- **Source tokens:** ~{raw_tokens} → **summary tokens:** ~{summary_tokens}")
    lines.append("")

    purpose_label = "Purpose (inferred)" if fs.purpose_inferred else "Purpose"
    lines.append(f"## {purpose_label}")
    lines.append(fs.purpose or "_Not determinable from source._")
    lines.append("")

    if fs.symbols:
        symbols = sorted(fs.symbols, key=lambda s: _KIND_ORDER.get(s.kind, 9))
        lines.append("## Key symbols")
        cap = 60
        hidden = max(0, len(symbols) - cap)
        for s in symbols[:cap]:
            loc = f" L{s.line}" if getattr(s, "line", 0) else ""
            head = f"- **{s.name}** (`{s.kind}`{loc})"
            if s.doc:
                head += f" — {s.doc}"
            lines.append(head)
            if s.signature and s.signature not in (s.name, s.doc):
                lines.append(f"  - `{s.signature}`")
        if hidden:
            lines.append(f"- _…and {hidden} more symbol(s)._")
        lines.append("")

    if fs.dependencies:
        shown = fs.dependencies[:30]
        lines.append("## Dependencies")
        lines.append(", ".join(f"`{d}`" for d in shown))
        if len(fs.dependencies) > len(shown):
            lines.append(f"\n_…and {len(fs.dependencies) - len(shown)} more._")
        lines.append("")

    if fs.gotchas:
        lines.append("## Gotchas / notes")
        for g in fs.gotchas:
            lines.append(f"- {g}")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Structural summary from local static analysis (no LLM). "
        "Re-read the file for exact implementation details._"
    )
    return "\n".join(lines).rstrip() + "\n"

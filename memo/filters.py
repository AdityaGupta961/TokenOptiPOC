"""Per-tool stdout compression for `memo run`.

Layer 1 of memo: memo's existing graph/cache layers avoid re-reading *files* an agent
has already seen; this module targets a different token source entirely — the *output
of commands* an agent runs (test suites, linters, git). A pytest run failing 2 of 400
tests still prints 398 PASSED lines; that's the noise this compresses.

Each recognized tool gets a small, deliberately narrow compressor that keeps failures
and summaries, drops pass/progress noise. Everything else falls back to a generic line
cap. A compressor here is allowed to be conservative — `runner.py`'s never_worse guard
is what actually decides whether the compressed result ships, so understating what can
be safely dropped costs nothing; overstating it would.
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_MAX_LINES = 200

_PYTHON_LAUNCHER_RE = re.compile(r"^python(\d+(\.\d+)?)?$")


def normalize_tool_name(argv: list[str]) -> str:
    """Map a command's argv to a compressor key. Unrecognized commands -> "generic"."""
    if not argv:
        return "generic"
    base = Path(argv[0]).name.lower()
    # Strip a Windows executable extension (e.g. "pytest.exe") before matching.
    if base.endswith((".exe", ".cmd", ".bat", ".ps1")):
        base = base.rsplit(".", 1)[0]
    rest = [a.lower() for a in argv[1:]]

    # `python -m pytest`, `python3.12 -m mypy`, etc. — the real tool is the module
    # name, not "python3", which is by far the most common way pytest/mypy actually
    # get invoked (including from this repo's own README/tests). Recurse on the
    # module name as if it were argv[0].
    if _PYTHON_LAUNCHER_RE.match(base) and "-m" in argv[1:]:
        idx = argv.index("-m")
        if idx + 1 < len(argv):
            return normalize_tool_name([argv[idx + 1], *argv[idx + 2:]])

    if base in ("pytest", "py.test"):
        return "pytest"
    if base == "jest":
        return "npm_test"
    if base in ("npm", "yarn", "pnpm") and "test" in rest:
        return "npm_test"
    if base == "git":
        if rest[:1] == ["diff"]:
            return "git_diff"
        if rest[:1] == ["log"]:
            return "git_log"
        if rest[:1] == ["status"]:
            return "git_status"
        return "generic"
    if base == "eslint":
        return "eslint"
    if base == "mypy":
        return "mypy"
    if base == "cargo":
        if rest[:1] == ["test"]:
            return "cargo_test"
        if rest[:1] == ["build"]:
            return "cargo_build"
        return "generic"
    return "generic"


def _load_max_lines(cache_root: Path, tool: str) -> int:
    """Read a per-tool line cap from `.memo/filters.toml`, if present and parseable.

    Optional by design: memo supports Python 3.9+, and `tomllib` is 3.11+ stdlib, so a
    missing file, an unavailable parser, or bad syntax all silently fall back to the
    hardcoded default rather than forcing a new hard dependency onto every install.
    """
    cfg = cache_root / "filters.toml"
    if not cfg.is_file():
        return DEFAULT_MAX_LINES
    try:
        import tomllib
    except ImportError:
        return DEFAULT_MAX_LINES
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_MAX_LINES
    section = data.get(tool, {})
    max_lines = section.get("max_lines") if isinstance(section, dict) else None
    return max_lines if isinstance(max_lines, int) and max_lines > 0 else DEFAULT_MAX_LINES


def compress(tool: str, text: str, cache_root: Path) -> str:
    """Compress `text` (a command's stdout) using the compressor registered for `tool`."""
    if not text:
        return text
    max_lines = _load_max_lines(cache_root, tool)
    fn = _COMPRESSORS.get(tool, _compress_generic)
    return fn(text, max_lines)


def _compress_generic(text: str, max_lines: int) -> str:
    """Fallback for any tool without a dedicated compressor: a hard line cap.

    Deliberately not "smart" — it doesn't know what's noise for an arbitrary tool. A
    line cap still saves real tokens on verbose output, and the never_worse guard in
    runner.py means it can never make things worse than doing nothing.
    """
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[:max_lines]
    dropped = len(lines) - max_lines
    kept.append(f"... [{dropped} more line(s) truncated]")
    return "\n".join(kept)


_SECTION_RE = re.compile(r"^=+\s*(.+?)\s*=+$")
_DURATION_RE = re.compile(r"\bin\s+[\d.]+s\b")


def _compress_pytest(text: str, max_lines: int) -> str:
    """Keep pytest's FAILURES/ERRORS/short-summary sections and the final result
    banner verbatim; drop the per-test PASSED/progress-percentage noise in between.

    On an all-green run there's no FAILURES section at all — in that case the single
    final summary line ("N passed in X.XXs") carries everything worth knowing.
    """
    lines = text.splitlines()
    kept: list[str] = []
    in_verbatim_block = False
    final_summary: str | None = None

    for ln in lines:
        m = _SECTION_RE.match(ln.strip())
        if m:
            title = m.group(1).lower()
            if "failures" in title or "errors" in title or "short test summary" in title:
                in_verbatim_block = True
                kept.append(ln)
                continue
            if _DURATION_RE.search(title):
                in_verbatim_block = False
                kept.append(ln)
                final_summary = ln
                continue
            if "test session starts" in title:
                in_verbatim_block = False
                continue  # platform/rootdir header noise
        if in_verbatim_block:
            kept.append(ln)
            continue
        if ln.strip().startswith("collected "):
            kept.append(ln)

    if final_summary is not None and "failed" not in final_summary.lower() \
            and "error" not in final_summary.lower():
        return final_summary  # all green — nothing else is worth keeping

    if not kept:
        return _compress_generic(text, max_lines)
    return _compress_generic("\n".join(kept), max_lines)


_PASS_LINE_RE = re.compile(r"^\s*(PASS\b|[✓√])")


def _compress_pass_fail_lines(text: str, max_lines: int) -> str:
    """Drop lines that only announce a pass (jest/npm-test style: "PASS foo.test.js",
    "✓ does the thing") and collapse blank-line runs; keep everything else,
    including failures, which these tools already print with their own detail."""
    lines = text.splitlines()
    kept: list[str] = []
    prev_blank = False
    for ln in lines:
        if _PASS_LINE_RE.match(ln):
            continue
        is_blank = not ln.strip()
        if is_blank and prev_blank:
            continue
        kept.append(ln)
        prev_blank = is_blank
    if not kept:
        return _compress_generic(text, max_lines)
    return _compress_generic("\n".join(kept), max_lines)


_OK_TEST_RE = re.compile(r"^test .+ \.\.\. ok\s*$")


def _compress_cargo_test(text: str, max_lines: int) -> str:
    """Drop cargo's "test foo::bar ... ok" lines; keep failures and the result line."""
    lines = text.splitlines()
    kept = [ln for ln in lines if not _OK_TEST_RE.match(ln.strip())]
    if not kept:
        return _compress_generic(text, max_lines)
    return _compress_generic("\n".join(kept), max_lines)


_COMPRESSORS = {
    "pytest": _compress_pytest,
    "npm_test": _compress_pass_fail_lines,
    "cargo_test": _compress_cargo_test,
    # git_diff / git_log / git_status / eslint / mypy / cargo_build: no dedicated
    # parser yet (v1 scope) — they still go through _compress_generic's line cap via
    # the default in `compress()`. Bespoke handling for these is a natural follow-up,
    # not required to ship value: the never_worse guard makes generic capping safe
    # even where it isn't especially smart.
}

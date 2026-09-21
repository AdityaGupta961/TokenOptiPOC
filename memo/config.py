"""Configuration: which files count as 'source code', and where to find things.

The extension allowlist is intentionally focused on the languages this tool cares
about (ASP.NET Core / C#, VB.NET, React JS/TS, classic ASPX) plus a few common
extras. It is configurable per-project via a `.memo/config.json` file, e.g.:

    { "extensions": [".cs", ".vb", ".ts", ".tsx", ".razor"] }

Any extension listed there fully replaces the defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Maps a file extension -> a language key understood by the summarizer dispatch.
DEFAULT_LANGUAGES: dict[str, str] = {
    # .NET
    ".cs": "csharp",
    ".vb": "vbnet",
    # Web / React
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    # Classic ASP.NET WebForms + Razor markup
    ".aspx": "markup",
    ".ascx": "markup",
    ".ashx": "markup",
    ".asmx": "markup",
    ".master": "markup",
    ".cshtml": "markup",
    ".vbhtml": "markup",
    # Classic ASP (VBScript/JScript server pages) — common in legacy ImageOne code
    ".asp": "markup",
    ".inc": "markup",
    # .NET configuration. Indexed because on a WebForms / classic-ASP estate the config IS
    # the behaviour, and leaving it out cost a real investigation: the root cause was
    # `customErrors mode="On" defaultRedirect="Error.asp"` in Web.config, so
    # `memo find "customErrors"` returned nothing and the answer was invisible to memo.
    # Environment variants (prod/uat/load.web.config) are where deployments actually
    # diverge. Bulk noise like packages.config is excluded by .memoignore, not here.
    ".config": "generic",
    # Python (this tool is written in Python; nice to dogfood)
    ".py": "python",
}

CACHE_DIRNAME = ".memo"
CACHE_SUBDIR = "cache"
INDEX_FILENAME = "index.json"
CONFIG_FILENAME = "config.json"
IGNORE_FILENAME = ".memoignore"


def find_cache_root(start: Path | None = None) -> Path:
    """Locate the project's .memo home.

    Walks up from `start` (default: CWD) looking for an existing `.memo` directory
    so `memo` can be run from any subdirectory of a project. If none is found,
    the cache lives in `<CWD>/.memo`.

    Overridable with the MEMO_HOME environment variable (points at the dir that
    should *contain* `.memo`).
    """
    env = os.environ.get("MEMO_HOME")
    if env:
        return Path(env).expanduser().resolve() / CACHE_DIRNAME

    start = (start or Path.cwd()).resolve()
    for parent in [start, *start.parents]:
        if (parent / CACHE_DIRNAME).is_dir():
            return parent / CACHE_DIRNAME
    return start / CACHE_DIRNAME


def load_extensions(cache_root: Path) -> dict[str, str]:
    """Return the extension->language map, honoring a per-project config override."""
    cfg = cache_root / CONFIG_FILENAME
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            exts = data.get("extensions")
            if isinstance(exts, list) and exts:
                out: dict[str, str] = {}
                for e in exts:
                    e = e.lower()
                    if not e.startswith("."):
                        e = "." + e
                    out[e] = DEFAULT_LANGUAGES.get(e, "generic")
                return out
        except (json.JSONDecodeError, OSError):
            pass  # fall back to defaults on any malformed config
    return dict(DEFAULT_LANGUAGES)


def language_for(path: Path, extensions: dict[str, str]) -> str | None:
    """Return the language key for a path, or None if the extension isn't allowed."""
    return extensions.get(path.suffix.lower())


BILLING_MODES = ("subscription", "payg")
DEFAULT_BILLING_MODE = "subscription"


def load_billing_mode(cache_root: Path) -> str:
    """Return the configured billing mode, honoring a per-project config override.

    Gates how aggressively `memo run` is allowed to compress tool output:
    "subscription" (flat-rate plans) sticks to lossless-only compression, "payg"
    (pay-per-token) allows the more aggressive fallback compressor. Defaults to
    the safer "subscription" behavior when unset or malformed.
    """
    cfg = cache_root / CONFIG_FILENAME
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            mode = data.get("billing_mode")
            if isinstance(mode, str) and mode.lower() in BILLING_MODES:
                return mode.lower()
        except (json.JSONDecodeError, OSError):
            pass  # fall back to the default on any malformed config
    return DEFAULT_BILLING_MODE

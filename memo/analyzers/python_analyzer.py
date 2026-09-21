"""Python analyzer using the standard-library `ast` module (accurate, not regex)."""

from __future__ import annotations

import ast

from .base import FileSummary, Symbol, first_sentence, line_of, scan_comment_markers


def _doc_first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node)
    return first_sentence(doc.split("\n\n")[0]) if doc else ""


def _args_sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    parts = [a.arg for a in fn.args.args]
    if fn.args.vararg:
        parts.append("*" + fn.args.vararg.arg)
    if fn.args.kwarg:
        parts.append("**" + fn.args.kwarg.arg)
    prefix = "async def " if isinstance(fn, ast.AsyncFunctionDef) else "def "
    sig = f"{prefix}{fn.name}({', '.join(parts)})"
    decs = _decorator_names(fn)
    # Decorators change how a symbol is *used* — @property is read as an attribute,
    # @staticmethod takes no self, @lru_cache memoizes — so they belong in the summary
    # rather than being silently dropped.
    return " ".join(f"@{d}" for d in decs) + (" " if decs else "") + sig


def _decorator_names(node) -> list[str]:
    """Decorator names as written, e.g. ['dataclass', 'functools.lru_cache']."""
    out: list[str] = []
    for d in getattr(node, "decorator_list", []) or []:
        target = d.func if isinstance(d, ast.Call) else d
        try:
            out.append(ast.unparse(target))
        except Exception:
            if isinstance(target, ast.Name):
                out.append(target.id)
    return out


# Decorators that make a function behave like an attribute rather than a call. Marking
# these "property" matters: the call graph matches `name(`, which never appears for
# them, and `peek`/`brief` rank real logic above data accessors.
_PROPERTY_DECORATORS = {"property", "cached_property", "functools.cached_property"}


def _fn_kind(fn, default: str) -> str:
    decs = set(_decorator_names(fn))
    if decs & _PROPERTY_DECORATORS or any(d.endswith(".setter") or d.endswith(".getter")
                                          for d in decs):
        return "property"
    return default


def analyze(text: str) -> FileSummary:
    summary = FileSummary(language="python")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        summary.purpose = f"(Could not parse Python: {exc.msg})"
        summary.purpose_inferred = True
        return summary

    # Purpose: module docstring, else inferred.
    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        summary.purpose = first_sentence(mod_doc.split("\n\n")[0])
    else:
        summary.purpose_inferred = True

    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module.split(".")[0])

    # Top-level functions and classes (+ their methods). `end_lineno` is exact here —
    # one of the pay-offs of Python being the only language memo parses properly.
    def _end(node) -> int:
        return getattr(node, "end_lineno", 0) or 0

    def _class_sig(node: ast.ClassDef) -> str:
        bases = [ast.unparse(b) for b in node.bases] if node.bases else []
        decs = _decorator_names(node)
        sig = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
        return " ".join(f"@{d}" for d in decs) + (" " if decs else "") + sig

    def _visit(body, prefix: str) -> None:
        """Collect symbols recursively, qualifying names with their container.

        Recursion (rather than the previous fixed two levels) is what makes nested
        classes visible: `Config.Inner` and `Config.Inner.deep` were previously dropped
        entirely, so nothing inside a nested class existed in the call graph.
        """
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                summary.symbols.append(Symbol(
                    _fn_kind(node, "method" if prefix else "function"),
                    f"{prefix}{node.name}", _args_sig(node), _doc_first_line(node),
                    node.lineno, _end(node),
                ))
                # Closures are implementation detail, not surface area — don't descend.
            elif isinstance(node, ast.ClassDef):
                summary.symbols.append(Symbol(
                    "class", f"{prefix}{node.name}", _class_sig(node),
                    _doc_first_line(node), node.lineno, _end(node),
                ))
                _visit(node.body, f"{prefix}{node.name}.")

    _visit(tree.body, "")

    summary.dependencies = sorted(set(imports))
    summary.gotchas = _gotchas(text, tree)
    return summary


def _gotchas(text: str, tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        # Mutable default arguments — a classic Python footgun.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in node.args.defaults + node.args.kw_defaults:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    out.append(
                        f"Mutable default argument in '{node.name}()' "
                        "(shared across calls)."
                    )
                    break
        # eval/exec usage.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"eval", "exec"}:
                out.append(f"Uses {node.func.id}() — review for injection risk.")
        # Bare except.
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            out.append("Bare 'except:' swallows all exceptions (incl. KeyboardInterrupt).")
        # time.sleep — potential blocking.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sleep"
        ):
            out.append("Calls sleep() — blocking delay (units are seconds).")
    out.extend(scan_comment_markers(text))
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped = [x for x in out if not (x in seen or seen.add(x))]
    return deduped[:12]

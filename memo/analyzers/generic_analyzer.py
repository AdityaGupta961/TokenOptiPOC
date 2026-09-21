"""Fallback analyzer for allowlisted extensions with no dedicated parser.

Extracts only what's safe without language knowledge: leading comment as purpose,
and comment markers as gotchas. No symbol/dependency guessing (would be invented).
"""

from __future__ import annotations

from .base import FileSummary, first_sentence, scan_comment_markers


def analyze(text: str) -> FileSummary:
    summary = FileSummary(language="generic")
    lead: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(("#", "//", "--", ";", "*", "'")):
            lead.append(s.lstrip("#/-;*' ").strip())
        elif s == "":
            if lead:
                break
            continue
        else:
            break
    summary.purpose = first_sentence(" ".join(lead)) if lead else "Source file."
    summary.purpose_inferred = True
    summary.gotchas = scan_comment_markers(text)
    return summary

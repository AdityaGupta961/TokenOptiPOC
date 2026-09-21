"""Where each agentic IDE expects its project instructions, and in what shape.

This table is the single place a new IDE gets added. Paths and formats were verified
against each vendor's current documentation, because getting a filename or a frontmatter
key wrong means the rule silently never loads.

The organising question is **always-on versus on-demand**, and it matters more here than
for a typical tool: memo's entire claim is that it saves tokens, so paying a four-figure
token count on every request to advertise itself would eat the benefit it sells. Every
surface below is therefore classified by when the host agent loads it, and only the
`stub` — a short trigger list — goes anywhere unconditional.

  * Claude Code does NOT read AGENTS.md; `CLAUDE.md` is its file, and `@path` imports
    resolve up to 4 hops. But imports do **not** reduce context — imported files load at
    launch — so a `CLAUDE.md` carrying the full guide is pure always-on cost. A **Skill**
    at `.claude/skills/<name>/SKILL.md` is the right surface instead: auto-discovered with
    no settings entry, and only its `description` stays in context until it is invoked.
    The description therefore has to carry the triggers, because it is the only part the
    model sees when deciding.
  * Cursor reads `.cursor/rules/*.mdc` — a plain `.md` there is IGNORED. Four rule types
    exist, selected by frontmatter: `alwaysApply: true` (always), `alwaysApply: false`
    plus `description` ("Apply Intelligently" — the agent pulls it in when relevant),
    plus `globs` (auto-attach on matching files), or neither (manual `@rule` only).
    Cursor **also** reads AGENTS.md, so an `alwaysApply: true` rule that duplicates it
    loads the same text twice per request. That was memo's own bug: ~2,350 tokens of
    duplicated manual on every Cursor request.
  * GitHub Copilot in VS Code reads `.github/copilot-instructions.md` unconditionally and
    with no setting to enable — which makes it the reliable place for the stub. It also
    reads AGENTS.md and CLAUDE.md, but both are gated behind settings
    (`chat.useAgentsMdFile`, `chat.useClaudeMdFile`), so neither can be relied on. Its
    conditional surface, `.github/instructions/*.instructions.md`, needs
    `chat.includeApplyingInstructions` on, so memo routes Copilot to `memo guide` for the
    full body rather than to a file that may never load.
  * AGENTS.md is the cross-tool open standard (stewarded by the Linux Foundation's
    Agentic AI Foundation), read by Cursor, Codex, Gemini CLI and Zed. It is the canonical
    anchor — and unconditionally loaded, so it carries the stub.

Windsurf and Cline are deliberately absent: their docs were not reachable to verify, and
shipping a rule file to an unverified path is worse than shipping none.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- what the host agent calls its own built-in file tools -------------------
# Substituted into guide.GUIDE_TEMPLATE so the routing advice names tools the agent
# actually has. Getting this wrong is subtly costly: an agent told to use "Grep"
# when it only has "search" may decide the advice doesn't apply to it.
#
# Every value must be a NOUN PHRASE that reads correctly in all of these slots:
#   "Reach for <native_list> when ..."      "fall back to <native_list> and carry on"
#   "A file you can already name — <read>."  "A filename pattern — <glob>."
#   "... you can already pin down — <search>."
# An earlier version used verb phrases here and produced sentences like
# "Native a files-with-matches search is tiny" in the shipped files.

CLAUDE_NATIVE = {
    "{{native_list}}": "Glob, Grep and Read",
    "{{read}}": "`Read` it",
    "{{search}}": "a targeted `Grep`",
    "{{glob}}": "`Glob`",
}

GENERIC_NATIVE = {
    "{{native_list}}": "your own search and read tools",
    "{{read}}": "read it directly",
    "{{search}}": "a targeted text search",
    "{{glob}}": "your filename search",
}


@dataclass(frozen=True)
class Target:
    """One generated rule file for one host agent."""

    key: str                        # CLI selector, e.g. "cursor"
    label: str                      # human name for init's report
    rel_path: str                   # path relative to the repo root
    style: str                      # "stub" (short, always-on) | "full" (on-demand)
    native: dict                    # placeholder -> that agent's tool names
    frontmatter: str | None = None  # verbatim YAML block, for .mdc and SKILL.md
    exclusive: bool = False         # True = memo owns the whole file, no markers
    note: str = ""                  # shown after init, e.g. a setting to enable


# `alwaysApply: false` + a description is Cursor's "Apply Intelligently" type: the agent
# reads the description and pulls the rule in when it looks relevant. That makes the
# description the load-bearing part — it is the only text Cursor sees while deciding — so
# it is written as trigger conditions rather than as a summary. With `alwaysApply: true`
# this file would duplicate the AGENTS.md stub on every single request.
#
# Kept in sync with SKILL_FRONTMATTER's wording, including the revert described there: a
# "MUST load" imperative rewrite measured 0/8 organic uses versus 5/8 for this phrasing on
# the Claude Code Skill (the identical LLM-relevance-judgment mechanism Cursor uses here),
# so the softer phrasing below is the empirically better performer, not the theoretically
# recommended one.
CURSOR_FRONTMATTER = """\
---
description: >-
  Routing rules for the memo code-intelligence CLI. Load when the question is about
  code relationships or structure rather than one known file: who calls a method, what
  breaks if a symbol changes, how execution reaches code, where something is defined or
  referenced, which method holds a literal value, or what the repo architecture is. Also
  load for an ad-hoc structural query the fixed commands don't fit, a runtime/reflection
  edge that static analysis can't see, why a piece of code exists (an ADR), a call
  chain that crosses into another registered repo/service, who reads or writes a shared
  field/property, which files are riskiest to change (commit churn, not just size), what
  HTTP endpoints a repo exposes, or a literal that might live in a doc/config file rather
  than source.
alwaysApply: false
---"""

# A Skill's `description` is the only part Claude Code keeps in context until the skill is
# invoked, so it is doing the whole job the always-on stub does elsewhere: naming the
# question shapes that should trigger a load. It also has to say when NOT to load, or the
# skill fires on single-file reads where plain Read is cheaper.
#
# An A/B tried leading with the tool's identity and "MUST be used" imperative framing
# (Anthropic's own documented fix for under-triggering descriptions). Measured result was
# the opposite of the intent: 0/8 organic uses versus 5/8 for the phrasing below, across
# two repeated question types. Reverted for that reason — the wording below is the
# empirically better performer in this repo, even though it's the "softer" phrasing by the
# general guidance. Don't re-apply the imperative rewrite without re-measuring.
SKILL_FRONTMATTER = """\
---
name: memo
description: >-
  This skill should be used when investigating bugs, tracing save flows, doing
  root-cause analysis, or answering who calls a method, what breaks if a symbol changes,
  how execution reaches code, where something is defined or referenced, which method
  contains a literal value, or what the repo architecture is. Also use for a structural
  question the fixed commands don't fit (an ad-hoc graph query), a call edge that only
  exists via reflection/DI/dynamic dispatch (verified from a runtime trace), why a piece
  of code was built the way it was (an Architecture Decision Record), a call chain
  that crosses into another registered repo/service, who reads or writes a shared
  field/property (not a method call — callers/calls/trace can't see it), which files are
  riskiest to change (commit churn x size, not just call-graph hotspot count), what HTTP
  endpoints a repo exposes, or a literal search that came back empty but might live in a
  doc/config file rather than source. Also use when the user mentions memo brief, memo
  map, memo trace, memo callers, memo impact, memo peek, memo find, memo arch, memo
  query, memo adr, memo ingest-traces, memo refs, memo churn, or memo routes. Not for
  reading one file already open — use Read for that. Not for repo-wide search when memo
  map or memo callers would locate the symbol.
---"""


TARGETS: dict[str, Target] = {
    # Unconditionally loaded by Cursor, Copilot, Codex, Gemini CLI and Zed, so it carries
    # the short stub. Written first: it is the canonical anchor other surfaces point at.
    "agents": Target(
        key="agents",
        label="AGENTS.md (cross-tool standard)",
        rel_path="AGENTS.md",
        style="stub",
        native=GENERIC_NATIVE,
        note="Always-on: short trigger list only.",
    ),
    "claude": Target(
        key="claude",
        label="Claude Code (skill)",
        rel_path=".claude/skills/memo/SKILL.md",
        style="full",
        native=CLAUDE_NATIVE,
        frontmatter=SKILL_FRONTMATTER,
        # memo owns the file: a Skill needs its frontmatter on line 1, and nothing else
        # belongs in a single-purpose skill directory.
        exclusive=True,
        note="Body loads only when the skill is invoked.",
    ),
    "cursor": Target(
        key="cursor",
        label="Cursor",
        rel_path=".cursor/rules/memo.mdc",
        style="full",
        native=GENERIC_NATIVE,
        frontmatter=CURSOR_FRONTMATTER,
        # memo owns this file outright, so it is written whole rather than as a
        # managed block — which also keeps the frontmatter guaranteed to be line 1.
        exclusive=True,
        note="Apply Intelligently: pulled in on relevance, not every request.",
    ),
    "copilot": Target(
        key="copilot",
        label="GitHub Copilot (VS Code)",
        rel_path=".github/copilot-instructions.md",
        style="stub",
        native=GENERIC_NATIVE,
        note="Always-on and needs no setting; full guide via `memo guide`.",
    ),
}

ALL = ("agents", "claude", "cursor", "copilot")


def resolve(keys: list[str]) -> list[Target]:
    """Deduplicate selected keys into targets, in canonical order.

    Every surface is self-contained now — none imports another — so this is ordering and
    deduplication only. Order follows ALL so the cross-tool anchor is written first.
    """
    wanted = {k for k in keys}
    return [TARGETS[k] for k in ALL if k in wanted]

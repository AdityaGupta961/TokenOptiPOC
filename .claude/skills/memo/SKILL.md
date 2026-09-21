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
---

# `memo` — code intelligence for this repo

This repo has a prebuilt `memo` index in `.memo/`: per-file summaries plus a call
graph. `memo` answers structure and relationship questions without reading whole
files. Invoke it as `python -m memo.cli <cmd>` from anywhere in the repo.

## Reach for memo when the question spans files or is about relationships

| Need | Command |
|---|---|
| Who calls X, what X calls, how execution reaches X | `memo callers X` / `memo calls X` / `memo trace X --up` |
| What breaks if X changes | `memo impact X`, or `memo changed <git-ref>` for a diff |
| Which tests reach X, to verify a change without the whole suite | `memo tests X` — prints a ready-to-run filter |
| You have a stack trace or exception log | `memo trace-stack --file <log>` (or pipe it in) — resolves every frame at once, marks framework frames, flags stale line numbers |
| How does X work, and you don't yet know which file/symbol answers it | `memo brief "<terms>"` — returns ranked method source, several at once |
| How does X work, and X is a name you already have exactly | `memo peek X` — one call: signature, line, source |
| Which method contains a literal value | `memo find "<value>"` |
| Where X is defined and referenced | `memo map X` |
| What this repo is, where requests enter | `memo arch` |
| A question the fixed commands don't fit (e.g. "all methods in package X with >5 callers") | `memo query "symbols kind=method callers>5 path~X"` — run `memo schema` first for fields |
| Why a symbol reached its callers/dispatch even though it's not in the source | check `memo ingest-traces` has been run for it — verified edges outrank name-based ones |
| Why something is built this way, not just what it does | `memo adr for X` |
| A call chain that crosses into another repo/service | `memo trace X --down --cross-service` (only works for repos + boundaries already declared via `memo register`/`memo link`; `memo suggest-links` finds candidate boundaries to confirm) |
| Full picture of how X fits in (who calls it AND what it calls) | `memo neighbors X` — one call instead of `callers` + `calls` |
| Just the source at a coordinate you already have | `memo peek --at <file>:<line>` |
| Who reads/writes a shared field or property (not a method call — no `(`, so callers/calls/trace can't see it) | `memo refs FieldName` — C#/VB.NET `property` declarations only |
| Which files are risky to touch (churn, not just size or hotspot count) | `memo churn` — commits x tokens, ranked; needs a git repo |
| What HTTP endpoints this repo exposes | `memo routes` — structural, from the index, not a live scan |
| A literal that might live in a doc/config file, not just source (`find` returned nothing but you suspect it exists) | `memo find "<value>" --all` — scans non-cached files too |
| Running a real command (tests, lint, build, git) whose output would be mostly noise | `memo run -- <cmd> [args]` — compresses stdout, prints `memo recall <hash>` if anything was cut, never loses data |

For `brief`, `map` and `find`, pick rare domain terms (`IsRetroCase`, `retro`). Drop
generic ones (`data`, `service`, `get`) — they match everything and rank nothing.
`brief` ranks by name match, file relevance, real call-graph PageRank, and (with the
optional `[search]` extra installed) BM25 over each candidate's doc comment too — so a
query matching only a docstring, not any name, can still surface the right method.
`memo schema` lists every computed field `query` can filter/sort on, including `pagerank`.

## Reach for your own tools when the target is a single known thing

- A file you can already name — `Read` it, once. One cohesive read beats several `peek`s.
- A filename pattern — `Glob`.
- A rare literal string, or one definition you can already pin down — a targeted `Grep`.

## Rules

1. **Cite memo's `file:line` as given. Do not re-read the file to confirm it.**
   Re-verifying memo's output with your own reads is the largest single waste; it
   cancels out the saving. When you need to actually see the code behind a citation
   — not just cite it — that is `memo peek --at <file>:<line>` (or `memo peek <name>`
   if you have the name instead), never `Read` it: peek returns exactly that span,
   `Read` it returns the whole file. Read further with your own tools only for
   content memo did not return at all, such as a branch body or a config value.
2. Lead with the one command that fits. Take at most three follow-up calls, then
   answer. Do not explore by firing many small commands. In particular: if the
   question needs the source of *several* functions to answer (tracing a chain,
   understanding a subsystem), that is `brief`, in one call — not `peek` on each
   function by name one at a time. A/B measured: an agent that already knew this rule
   still called `peek` 8 times instead of `brief` once, at 15x the token cost for the
   same answer (49,651 vs. 3,397 tokens). If you find yourself about to call `peek`
   more than twice in the same investigation, stop and use `brief` instead.
3. Add `--terse` for minimal output, `--limit N` to cap rows, `--json` to parse. Every
   query command accepts all three.
4. If a command fails, returns nothing, or reports an empty cache, fall back to
   Glob, Grep and Read and carry on. Do not retry, and do not try to rebuild the index.
5. Never run `memo init` or `memo clear`. `init` overwrites these instruction files;
   `clear` destroys the index.
6. `memo adr add`, `memo ingest-traces`, `memo link`, `memo register` and
   `memo shim install`/`memo shim remove` write persistent state a teammate (or the
   next session on this machine) will later see (an ADR, a runtime edge, a
   cross-repo link, a PATH shim) — the same category as rule 5, not a query. Only
   run one because the user asked for that specifically, never speculatively while
   just investigating. `memo run -- <cmd>` is not in this category — it's the normal
   way to execute a command, not a side-effecting state write.

## How far to trust memo's answers

The call graph is **name-based**: an edge means the caller's body contains `name(`.
Edges are therefore *candidates*, not verified calls — same-named methods in different
classes get conflated, and reflection, dependency injection and dynamic dispatch are
invisible. That is precisely why rule 1 is safe rather than reckless: a wrong edge is
obvious from the line memo cites, so glance at that line instead of re-reading the
file. When memo warns `matches N definitions`, narrow with `--exact` or a qualified
name.

Rows carry a confidence marker where there is evidence either way. **No edge is ever
hidden by it** — a marker changes the order of results, never their membership.

**Rows arrive sorted by confidence, best first.** That ordering is the signal — in
`--terse` there is no marker on confident rows, because a marker would cost a token per
row to repeat what the sort already says.

| Marker | Meaning |
|---|---|
| `(verified)` | Confirmed by real execution via `memo ingest-traces` — not inferred, outranks every other tier |
| `(exact)` | The receiver's **declared type** matches this definition's type, or implements it — `ICaseProcessor _p;` then `_p.Save(x)` resolves to `CaseProcessor.Save` |
| `(strong)` | Same file with a fitting argument count, or the caller's own class |
| unmarked | No evidence either way; the common case |
| `!` or `(contra)` | The argument count cannot reach this definition |

`!` is the only marker `--terse` prints, because it is the one thing the ordering cannot
express. Glance at such a row before relying on it — but it is still a real `name(`
occurrence and may well be the call you want, since VB paren-less calls, C# extension
methods and generic calls all yield incomplete counts. Never report a `!` edge as absent.

An `(exact)` row is the one case worth trusting over your own guess: the receiver's type
was read from a declaration, not inferred from naming. Declared types are file-scoped, so
a field on the other half of a `partial class` is invisible, and a name declared as two
different types is reported as neither.

Symbols come from the last index; reference counts are read live from disk. Run
`memo status` if results look wrong — it reports whether the index has gone stale, and
if it has, say so rather than presenting stale coordinates as fact.

## On Windows and PowerShell

Do not pipe memo into `head`, `tail`, `grep` or `wc`; they do not exist there and the
command fails. Use `--terse` and `--limit N` instead.

Run **one** memo command per invocation, with nothing before or after it. `&&`, `||` and
`;` are bash operators — PowerShell rejects them as statement separators and the whole
call fails, which reads like "memo is broken" when memo was never reached. If that
happens, re-run the command on its own rather than debugging the chain.

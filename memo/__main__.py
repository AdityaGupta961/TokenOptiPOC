"""Allow `python -m memo` as a shorthand for `python -m memo.cli`.

memo is invoked via `python -m` rather than a console script on purpose: pip puts
`memo.exe` in a user-scheme Scripts directory that is frequently not on PATH
(especially on Windows), so `python -m` is the invocation that always works. Both
spellings are supported so a rule file can use whichever is clearer.
"""

from .cli import main

if __name__ == "__main__":
    main()

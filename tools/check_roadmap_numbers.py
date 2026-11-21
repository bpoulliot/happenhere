#!/usr/bin/env python3
"""Fail when two roadmap items carry the same number (roadmap 35).

``docs/ROADMAP.md`` is the only number registry: a session files its item under the next free
number read from ``main``. Two sessions that read the same number both file it, and nothing
noticed. This check runs in CI on every PR, docs-only ones included.

The roadmap names each item twice, and both are checked:

* a row of the ``## Items`` table whose first cell is a number: ``| 07 | 0 | ... |``;
* an item section heading (``##`` to ``####``) whose first token is a number: ``### 07 — ...``.

A number may carry a sub-letter or a suffix: ``288`` and ``288d`` and ``129-Q1`` are different
items; ``01`` and ``1`` are the same one. Headings that do not start with a number, and anything
inside a fenced code block, are not items. A file with no item at all is an error, not a pass: a
format change that stopped every line matching would otherwise pass on zero items.

Standard library only: the docs-only CI lane runs it with the runner's own ``python3``, without
installing the project.

    python3 tools/check_roadmap_numbers.py [docs/ROADMAP.md ...]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_FILES = ("docs/ROADMAP.md",)

# Duplicates already on main when this check landed, allowed by number with the reason. Never
# renumber another session's item to clear one; file it, and list it here.
# Empty: origin/main had none when item 35 measured it.
ALLOWED_DUPLICATES: dict[str, str] = {}

_NUMBER = r"(\d+)([A-Za-z]*(?:-[A-Za-z0-9]+)?)"
HEADING = re.compile(r"^#{2,4}[ \t]+" + _NUMBER + r"(?=[ \t]|$)")
TABLE_ROW = re.compile(r"^\|[ \t]*" + _NUMBER + r"[ \t]*\|")
ITEMS_SECTION = re.compile(r"^##[ \t]+Items[ \t]*$")
SECTION = re.compile(r"^#{1,6}[ \t]")


def _key(m: re.Match) -> str:
    return f"{int(m.group(1))}{m.group(2)}"


def items(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """``(line number, item key)`` for every item heading, and for every row of the Items table.

    Lines inside a fenced code block are neither. A table row counts only inside the
    ``## Items`` section (up to the next heading), so a measurement table in an item's own
    section never does.
    """
    headings, rows = [], []
    in_fence = in_items = False
    for no, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if SECTION.match(line):
            in_items = bool(ITEMS_SECTION.match(line))
            m = HEADING.match(line)
            if m:
                headings.append((no, _key(m)))
            continue
        if in_items:
            m = TABLE_ROW.match(line)
            if m:
                rows.append((no, _key(m)))
    return headings, rows


def duplicates(files: dict[str, str]) -> dict[str, list[str]]:
    """Every item key named twice in one kind (heading or table row), with ``file:line`` places.

    ``files`` maps a display name to the file's text, so an archive file can be checked with
    its roadmap as one registry.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for name, text in files.items():
        headings, rows = items(text)
        for kind, found in (("heading", headings), ("table row", rows)):
            for no, key in found:
                seen.setdefault((kind, key), []).append(f"{name}:{no}")
    out: dict[str, list[str]] = {}
    for (kind, key), where in seen.items():
        if len(where) > 1:
            out.setdefault(key, []).extend(f"{w} ({kind})" for w in where)
    return out


def main(argv: list[str] | None = None) -> int:
    paths = (argv if argv is not None else sys.argv[1:]) or list(DEFAULT_FILES)
    texts = {}
    for p in paths:
        try:
            texts[p] = Path(p).read_text(encoding="utf-8")
        except OSError as e:
            print(f"check_roadmap_numbers: cannot read {p}: {e}", file=sys.stderr)
            return 2
    n_head = n_rows = 0
    for p, t in texts.items():
        headings, rows = items(t)
        if not headings and not rows:
            print(f"check_roadmap_numbers: no roadmap item found in {p}", file=sys.stderr)
            return 2
        n_head += len(headings)
        n_rows += len(rows)
    dups = duplicates(texts)
    bad = {k: v for k, v in dups.items() if k not in ALLOWED_DUPLICATES}
    for k, where in sorted(dups.items()):
        if k in ALLOWED_DUPLICATES:
            print(f"allowed duplicate {k} ({ALLOWED_DUPLICATES[k]}): {', '.join(where)}")
    for k, where in sorted(bad.items()):
        print(f"DUPLICATE roadmap number {k}: {', '.join(where)}", file=sys.stderr)
    stale = sorted(set(ALLOWED_DUPLICATES) - set(dups))
    for k in stale:
        print(f"allowlisted duplicate {k} is no longer duplicated: remove it", file=sys.stderr)
    if bad or stale:
        return 1
    print(
        f"roadmap numbers: {n_head} item headings and {n_rows} table rows in "
        f"{', '.join(paths)}, no duplicates"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

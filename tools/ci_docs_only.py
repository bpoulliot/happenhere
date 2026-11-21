#!/usr/bin/env python3
"""Decide whether a pull request is docs-only (roadmap 35).

CI's ``changes`` job runs this on ``pull_request`` only and writes its one output line,
``docs_only=true`` or ``docs_only=false``, to ``$GITHUB_OUTPUT``. On ``true`` every required job
still runs and reports, but skips its heavy steps (installs, linters, the suite, CodeQL, the image
build), so no required check ever goes missing. Everything here fails OPEN, towards the full run:

* an empty diff is ``false``: no files is not evidence of a docs change;
* any path that does not end in ``.md`` is ``false``;
* a ``.md`` that code, a test or the build reads (``CODE_MD``, ``CODE_DIRS``) is code, so ``false``;
* a ``git diff`` that fails exits non-zero without printing, so ``$GITHUB_OUTPUT`` never gets the
  line and the workflow's ``!= 'true'`` runs every step.

The diff is ``base...head`` (from the merge base, so a ``main`` commit the branch lacks is not
counted) with ``--no-renames``, so a ``.py`` renamed to a ``.md`` shows its deleted ``.py``.

Standard library only: the job runs it with the runner's own ``python3``.

    python3 tools/ci_docs_only.py BASE_SHA HEAD_SHA
"""

from __future__ import annotations

import subprocess
import sys

# A .md outside CODE_DIRS that code, a test or the build reads, found by grepping app/, tests/,
# scripts/, tools/, .github/, the Dockerfile, pyproject.toml and .gitleaks.toml for `.md`, README,
# ROADMAP and glob() when item 35 landed. That grep found none outside CODE_DIRS: the one .md a
# test reads is app/geodata/SOURCE.md (tests/test_search_locations.py), which app/ covers, and
# the Dockerfile copies only requirements.txt, VERSION and app/.
CODE_MD: frozenset[str] = frozenset()
# A .md under these is a fixture, shipped data, a template or a workflow's input, never prose:
# code. app/ is copied whole into the image.
CODE_DIRS = ("app/", "tests/", "scripts/", "tools/", ".github/")

# docs/ROADMAP.md is NOT code although tools/check_roadmap_numbers.py and tests/test_ci_tools.py
# read it: both run in the docs lane too, so a docs-only PR that breaks the roadmap's numbering
# still goes red. CLAUDE.md is named in .gitleaks.toml's allowlist; gitleaks runs in both lanes.


def is_code(path: str) -> bool:
    return not path.endswith(".md") or path in CODE_MD or path.startswith(CODE_DIRS)


def docs_only(paths: list[str]) -> bool:
    """True only when there is at least one path and every one is prose Markdown."""
    paths = [p for p in paths if p.strip()]
    return bool(paths) and not any(is_code(p) for p in paths)


def changed_paths(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", f"{base}...{head}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2 or not all(a.strip() for a in args):
        print("usage: ci_docs_only.py BASE_SHA HEAD_SHA", file=sys.stderr)
        return 2
    try:
        paths = changed_paths(*args)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"ci_docs_only: git diff failed, full run: {e}", file=sys.stderr)
        return 1
    verdict = docs_only(paths)
    print(
        f"ci_docs_only: {len(paths)} changed paths; docs_only={str(verdict).lower()}",
        file=sys.stderr,
    )
    print(f"docs_only={str(verdict).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

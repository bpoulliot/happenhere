"""tools/check_roadmap_numbers.py and tools/ci_docs_only.py (roadmap 35).

Standard library ``unittest`` on purpose: CI's docs-only lane runs this file with the runner's
own ``python3`` (``python3 -m unittest -v tests/test_ci_tools.py``), without installing the
project, because one test reads the real ``docs/ROADMAP.md``. pytest collects it in the full
lane as usual.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


numbers = _load("check_roadmap_numbers")
detector = _load("ci_docs_only")


def _run(fn, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args)
    return code, out.getvalue(), err.getvalue()


ITEMS = "# r\n\n## Items\n\n| # | Tier | Item | Status |\n|---|---|---|---|\n"


class RoadmapNumbers(unittest.TestCase):
    def _check(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ROADMAP.md"
            p.write_text(text, encoding="utf-8")
            return _run(numbers.main, [str(p)])

    def test_a_duplicate_heading_is_red(self):
        code, _, err = self._check("# r\n\n### 12 — one — DONE\n\nx\n\n### 12 — two — READY\n")
        self.assertEqual(code, 1)
        self.assertIn("DUPLICATE roadmap number 12", err)

    def test_a_duplicate_table_row_is_red(self):
        text = ITEMS + "| 12 | 0 | one | DONE |\n| 12 | 1 | two | READY |\n\n### 12 — one\n"
        code, _, err = self._check(text)
        self.assertEqual(code, 1)
        self.assertIn("DUPLICATE roadmap number 12", err)
        self.assertIn("table row", err)

    def test_one_row_and_one_heading_per_item_is_green(self):
        text = (
            ITEMS + "| 12 | 0 | one | DONE |\n| 13 | 1 | two | READY |\n\n### 12 — a\n### 13 — b\n"
        )
        code, out, _ = self._check(text)
        self.assertEqual(code, 0)
        self.assertIn("2 item headings and 2 table rows", out)

    def test_leading_zero_is_the_same_number(self):
        code, _, err = self._check("### 01 — a\n### 1 — b\n")
        self.assertEqual(code, 1)
        self.assertIn("DUPLICATE roadmap number 1:", err)

    def test_sub_letters_and_suffixes_are_distinct_items(self):
        code, out, _ = self._check(
            "### 288 — base\n### 288d — d\n### 288e — e\n### 129-Q1 — q\n### 129 — base\n"
        )
        self.assertEqual(code, 0)
        self.assertIn("5 item headings", out)

    def test_a_repeated_sub_letter_is_red(self):
        code, _, err = self._check("### 288d — a\n### 288d — b\n")
        self.assertEqual(code, 1)
        self.assertIn("288d", err)

    def test_non_item_headings_fences_and_other_tables_are_ignored(self):
        text = (
            ITEMS
            + "| 33 | 0 | item | DONE |\n\n### 33 — item\n### Item 33's match table\n"
            + "| 33 | a measurement row, not an item |\n```\n### 33 — quoted\n| 33 | x |\n```\n"
        )
        code, out, _ = self._check(text)
        self.assertEqual(code, 0)
        self.assertIn("1 item headings and 1 table rows", out)

    def test_a_file_with_no_item_is_an_error_not_a_pass(self):
        code, _, err = self._check("# r\n\nprose only\n")
        self.assertEqual(code, 2)
        self.assertIn("no roadmap item", err)

    def test_the_real_roadmap_is_green(self):
        code, out, err = _run(numbers.main, [str(ROOT / "docs" / "ROADMAP.md")])
        self.assertEqual(code, 0, err)
        # The roadmap holds dozens of items: a regex that stopped matching would pass a
        # duplicate check on zero lines, so both counts are asserted too.
        words = out.split("roadmap numbers: ")[1].split()
        self.assertGreater(int(words[0]), 30)  # item headings
        self.assertGreater(int(words[4]), 30)  # table rows

    def test_an_unreadable_file_is_an_error_not_a_pass(self):
        code, _, _ = _run(numbers.main, ["/nonexistent/ROADMAP.md"])
        self.assertEqual(code, 2)


class DocsOnly(unittest.TestCase):
    def test_markdown_only_is_docs_only(self):
        self.assertTrue(detector.docs_only(["docs/ROADMAP.md", "docs/measurements/35-x.md"]))
        self.assertTrue(detector.docs_only(["CLAUDE.md"]))

    def test_markdown_plus_python_is_not(self):
        self.assertFalse(detector.docs_only(["docs/ROADMAP.md", "app/dedup.py"]))

    def test_a_non_markdown_file_alone_is_not(self):
        for p in ("requirements.txt", "Dockerfile", "docs/notes.txt", ".github/workflows/ci.yml"):
            with self.subTest(p=p):
                self.assertFalse(detector.docs_only(["docs/ROADMAP.md", p]))

    def test_markdown_that_code_or_tests_read_is_code(self):
        # app/geodata/SOURCE.md is read by tests/test_search_locations.py and shipped in the image.
        for p in (
            "app/geodata/SOURCE.md",
            "tests/fixtures/sample.md",
            "scripts/notes.md",
            "tools/notes.md",
            ".github/pull_request_template.md",
        ):
            with self.subTest(p=p):
                self.assertFalse(detector.docs_only(["docs/ROADMAP.md", p]))

    def test_an_empty_diff_fails_open(self):
        self.assertFalse(detector.docs_only([]))
        self.assertFalse(detector.docs_only([""]))


class DocsOnlyAgainstGit(unittest.TestCase):
    """main() over a throwaway repository: the diff it reads, not just the rule."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._git("init", "-q", "-b", "main")
        (Path(self.repo) / "docs").mkdir()
        (Path(self.repo) / "docs" / "ROADMAP.md").write_text("# r\n")
        (Path(self.repo) / "mod.py").write_text("x = 1\n")
        self.base = self._commit("base")

    def _git(self, *args):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        return subprocess.run(
            ["git", "-C", self.repo, *args], env=env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def _commit(self, msg):
        self._git("add", "-A")
        self._git("commit", "-q", "--allow-empty", "-m", msg)
        return self._git("rev-parse", "HEAD")

    def _main(self, base, head):
        cwd = os.getcwd()
        os.chdir(self.repo)
        try:
            return _run(detector.main, [base, head])
        finally:
            os.chdir(cwd)

    def test_a_markdown_commit_is_docs_only(self):
        (Path(self.repo) / "docs" / "ROADMAP.md").write_text("# r\n\nmore\n")
        head = self._commit("docs")
        code, out, _ = self._main(self.base, head)
        self.assertEqual((code, out), (0, "docs_only=true\n"))

    def test_a_python_change_is_not(self):
        (Path(self.repo) / "docs" / "ROADMAP.md").write_text("# r\n\nmore\n")
        (Path(self.repo) / "mod.py").write_text("x = 2\n")
        head = self._commit("code")
        code, out, _ = self._main(self.base, head)
        self.assertEqual((code, out), (0, "docs_only=false\n"))

    def test_a_python_file_renamed_to_markdown_is_not(self):
        self._git("mv", "mod.py", "mod.md")
        head = self._commit("rename")
        code, out, _ = self._main(self.base, head)
        self.assertEqual((code, out), (0, "docs_only=false\n"))

    def test_an_empty_diff_is_not(self):
        head = self._commit("empty")
        code, out, _ = self._main(self.base, head)
        self.assertEqual((code, out), (0, "docs_only=false\n"))

    def test_a_trunk_commit_the_branch_lacks_is_not_counted(self):
        self._git("checkout", "-q", "-b", "pr")
        (Path(self.repo) / "docs" / "ROADMAP.md").write_text("# r\n\nmore\n")
        head = self._commit("docs on the branch")
        self._git("checkout", "-q", "main")
        (Path(self.repo) / "mod.py").write_text("x = 3\n")
        trunk = self._commit("code on main")
        code, out, _ = self._main(trunk, head)
        self.assertEqual((code, out), (0, "docs_only=true\n"))

    def test_a_failed_diff_prints_nothing_and_fails(self):
        code, out, err = self._main(self.base, "0" * 40)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("full run", err)

    def test_missing_arguments_print_nothing(self):
        code, out, _ = _run(detector.main, [self.base])
        self.assertEqual((code, out), (2, ""))


if __name__ == "__main__":
    unittest.main()

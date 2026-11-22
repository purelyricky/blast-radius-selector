"""Focused checks for patch safety and scope selection; no benchmark answers."""
import ast
import difflib
import random
import json
from pathlib import Path
import tempfile
import unittest

from dependencies import BroadChange, DependencySelector, body_regions, changed_functions
from patches import InvalidPatch, apply_patch, parse_patch


def patch(old, new):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile="a/sqlglot/example.py", tofile="b/sqlglot/example.py"))


class PatchSafety(unittest.TestCase):
    def test_round_trips(self):
        rng = random.Random(19)
        for _ in range(400):
            original = [f"line {rng.randrange(100)}\n" for _ in range(rng.randrange(1, 60))]
            modified = list(original)
            for _ in range(rng.randrange(1, 8)):
                start = rng.randrange(len(modified) + 1)
                modified[start:start + rng.randrange(5)] = [f"new {rng.randrange(100)}\n" for _ in range(rng.randrange(5))]
            old, new = "".join(original), "".join(modified)
            if old != new:
                parsed = parse_patch(patch(old, new))
                self.assertEqual(apply_patch(old, parsed[0]), new)

    def test_rejects_context_mismatch(self):
        parsed = parse_patch(patch("one\ntwo\n", "one\nthree\n"))
        with self.assertRaises(InvalidPatch):
            apply_patch("one\nfour\n", parsed[0])

    def test_rejects_malformed_counts(self):
        with self.assertRaises(InvalidPatch):
            parse_patch("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1 @@\n-old\n+new\n")

    def test_rejects_path_escape(self):
        with self.assertRaises(InvalidPatch):
            parse_patch("--- a/../x.py\n+++ b/../x.py\n@@ -1 +1 @@\n-old\n+new\n")

    def test_body_scope_and_header_fallback(self):
        before = ast.parse("class X:\n def f(self, x=1):\n  return x\n")
        after = ast.parse("class X:\n def f(self, x=1):\n  return x+1\n")
        self.assertEqual([n.name for n in changed_functions(before, after)], ["f"])
        with self.assertRaises(BroadChange):
            changed_functions(before, ast.parse("class X:\n def f(self, x=2):\n  return x\n"))

    def test_global_assignment_fallback(self):
        with self.assertRaises(BroadChange):
            changed_functions(ast.parse("x=1"), ast.parse("x=2"))

    def test_stable_branch_narrows_to_return(self):
        old = ast.parse("def f(x):\n if x:\n  return 1\n return 2\n").body[0]
        new = ast.parse("def f(x):\n if x:\n  return 3\n return 2\n").body[0]
        self.assertEqual([n.lineno for n in body_regions(old, new)], [3])

    def test_predicate_change_includes_old_entry(self):
        old = ast.parse("def f(x):\n if x:\n  return 1\n return 2\n").body[0]
        new = ast.parse("def f(x):\n if not x:\n  return 1\n return 2\n").body[0]
        self.assertEqual([n.lineno for n in body_regions(old, new)], [2])

    def test_insertion_uses_enclosing_scope(self):
        old = ast.parse("def f(x):\n return x\n").body[0]
        new = ast.parse("def f(x):\n x+=1\n return x\n").body[0]
        self.assertIs(body_regions(old, new)[0], old)


class SelectionSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = "def f(x):\n if x:\n  return 1\n return 2\n"
        self.path = Path(self.tmp.name) / "map.json"
        self.data = {"version": 2, "revision": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "ids": ["A", "B"], "always": [], "sources": {"sqlglot/example.py": self.source}, "imports": {}, "groups": [[0], [1], [0, 1]], "lines": {"sqlglot/example.py": {"2": 2, "3": 0, "4": 1}}, "reads": {"tests/fixtures/data.sql": 0}, "import_lines": {}}
        # Deliberately renumber and reverse the runtime input.
        self.tests = [{"number": 91, "id": "B", "file": "tests/test_x.py"}, {"number": 17, "id": "A", "file": "tests/test_x.py"}]

    def selector(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")
        return DependencySelector(self.tests, self.path, precision="statement")

    def test_branch_selection_survives_renumbering(self):
        selected = self.selector().select(patch(self.source, self.source.replace("return 1", "return 3")))
        self.assertEqual(selected, {17})

    def test_predicate_and_insertion_include_both_paths(self):
        selector = self.selector()
        self.assertEqual(selector.select(patch(self.source, self.source.replace("if x:", "if not x:"))), {17, 91})
        self.assertEqual(selector.select(patch(self.source, "def f(x):\n raise ValueError()\n" + "\n".join(self.source.splitlines()[1:]) + "\n")), {17, 91})

    def test_unknown_runtime_test_is_always_selected(self):
        self.tests.append({"number": 44, "id": "unseen", "file": "tests/test_new.py"})
        self.assertEqual(self.selector().select(patch(self.source, "# comment\n" + self.source)), {44})

    def test_import_execution_and_syntax_errors_run_everything(self):
        self.data["import_lines"] = {"sqlglot/example.py": [3]}
        selector = self.selector()
        self.assertEqual(selector.select(patch(self.source, self.source.replace("return 1", "return 3"))), {17, 91})
        self.assertEqual(selector.select(patch(self.source, self.source.replace("if x:", "if x"))), {17, 91})

    def test_bad_patch_is_conservative(self):
        self.assertEqual(self.selector().select("not a patch"), {17, 91})

    def test_data_file_uses_recorded_readers(self):
        text = "--- a/tests/fixtures/data.sql\n+++ b/tests/fixtures/data.sql\n@@ -1 +1 @@\n-SELECT 1;\n+SELECT 2;\n"
        self.assertEqual(self.selector().select(text), {17})

    def test_equivalence_applies_only_without_other_ast_edits(self):
        self.source = "def f(x):\n if x is None:\n  return 1\n return 2\n"
        self.data["sources"]["sqlglot/example.py"] = self.source
        eq = {"revision": self.data["revision"], "proven": ["sqlglot/example.py::f::0"]}
        (self.path.parent / "eq_sites.json").write_text(json.dumps(eq), encoding="utf-8")
        selector = self.selector()
        equivalent = self.source.replace("x is None", "x == None")
        self.assertEqual(selector.select(patch(self.source, equivalent)), set())
        mixed = equivalent.replace("return 1", "return 3")
        self.assertEqual(selector.select(patch(self.source, mixed)), {17, 91})


if __name__ == "__main__":
    unittest.main()

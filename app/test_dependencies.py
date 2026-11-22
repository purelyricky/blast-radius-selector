"""Focused checks for patch safety and scope selection; no benchmark answers."""
import ast
import difflib
import random
import unittest

from dependencies import BroadChange, changed_functions
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


if __name__ == "__main__":
    unittest.main()

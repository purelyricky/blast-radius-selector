import ast
import unittest

from compare_equivalence import equivalent_edits


class ComparisonEquivalence(unittest.TestCase):
    def check(self, old, new, sites=(0,)):
        proven = {f"sqlglot/x.py::f::{n}" for n in sites}
        return equivalent_edits(ast.parse(old), ast.parse(new), "sqlglot/x.py", proven)

    def test_operator_only_swap(self):
        self.assertTrue(self.check("def f(x): return x is None", "def f(x): return x == None"))
        self.assertTrue(self.check("def f(x): return x is not None", "def f(x): return x != None"))

    def test_other_edits_are_rejected(self):
        self.assertFalse(self.check("def f(x): return x is None", "def f(x): return x == 1"))
        self.assertFalse(self.check("def f(x): return x is None", "def f(y): return y == None"))

    def test_ordinal_shifts_are_rejected(self):
        self.assertFalse(self.check("def f(x): return x is None", "def f(x):\n if x == 1: return True\n return x == None"))

    def test_unproven_sites_are_rejected(self):
        self.assertFalse(self.check("def f(x): return x is None", "def f(x): return x == None", sites=()))

    def test_boolean_integer_literals_do_not_match(self):
        self.assertFalse(self.check("def f(x):\n a=1\n return x is None", "def f(x):\n a=True\n return x == None"))

    def test_every_changed_site_needs_proof(self):
        old = "def f(x,y): return (x is None, y is None)"
        new = "def f(x,y): return (x == None, y == None)"
        self.assertFalse(self.check(old, new, sites=(0,)))
        self.assertTrue(self.check(old, new, sites=(0, 1)))


if __name__ == "__main__":
    unittest.main()

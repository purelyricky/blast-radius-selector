import ast
import unittest

from package_changes import body_only, generator_kind, region_imported


class PackageChanges(unittest.TestCase):
    def test_body_edit_is_local(self):
        old = ast.parse("from x import y\ndef f(x): return x\n")
        new = ast.parse("from x import y\ndef f(x): return x+1\n")
        self.assertTrue(body_only(old, new))

    def test_eager_layout_changes_are_rejected(self):
        for old, new in [("from x import y", "from z import y"), ("def f(x=1): return x", "def f(x=2): return x"), ("def f(x): return x", "def f(x): return x\ndef g(): pass")]:
            self.assertFalse(body_only(ast.parse(old), ast.parse(new)))

    def test_generator_kind_does_not_include_nested_functions(self):
        function = ast.parse("def f():\n def g(): yield 1\n return g\n").body[0]
        self.assertFalse(generator_kind(function))
        self.assertTrue(generator_kind(function.body[0]))

    def test_definition_event_is_not_a_body_execution(self):
        function = ast.parse("def f():\n return 1\n").body[0]
        self.assertFalse(region_imported(function, function, [1]))
        self.assertTrue(region_imported(function, function, [2]))


if __name__ == "__main__":
    unittest.main()

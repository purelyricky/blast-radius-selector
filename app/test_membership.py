import ast
import unittest
from membership import membership_changes


class MembershipTests(unittest.TestCase):
    def change(self, before, after):
        return membership_changes(ast.parse(before), ast.parse(after))

    def test_unused_plain_module_helper(self):
        found = self.change('x=1', 'x=1\ndef new_helper():\n return 2')
        self.assertEqual(found[0], {'new_helper'})

    def test_shadowing_existing_bindings_stays_broad(self):
        self.assertIsNone(self.change('class ErrorLevel: pass', 'class ErrorLevel: pass\nErrorLevel=None'))
        self.assertIsNone(self.change('import os', 'import os\ndef os():\n return 1'))
        self.assertIsNone(self.change('def f(): return 1', 'def f(): return 1\ndef f(): return 2'))

    def test_metaclass_visible_class_members_stay_broad(self):
        self.assertIsNone(self.change('class X: pass', 'class X:\n def new_method(self): return 1'))
        self.assertIsNone(self.change('class X:\n def f(self): return 1', 'class X: pass'))

    def test_eager_headers_and_imports_stay_broad(self):
        for addition in ('import no_such_module', 'x=1/0', 'def f(x=1/0): return x', '@missing\ndef f(): pass', 'def f(x: Missing): pass'):
            self.assertIsNone(self.change('', addition))


if __name__ == '__main__':
    unittest.main()

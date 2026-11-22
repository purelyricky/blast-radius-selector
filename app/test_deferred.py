import ast
import unittest

from deferred import deferred_body_changes


class DeferredTests(unittest.TestCase):
    def changes(self, before, after):
        return deferred_body_changes(ast.parse(before).body[0], ast.parse(after).body[0])

    def test_lambda_body(self):
        self.assertEqual(len(self.changes('def f():\n return lambda x: x+1', 'def f():\n return lambda x: x+2')), 1)

    def test_default_lambda_is_eager_header(self):
        before = 'def f():\n def g(cb=lambda x: x+1):\n  return cb\n return g'
        self.assertIsNone(self.changes(before, before.replace('x+1', 'x+2')))

    def test_generator_kind_is_observable_at_creation(self):
        before = 'def f():\n def g():\n  return 1\n return g'
        self.assertIsNone(self.changes(before, before.replace('return 1', 'yield 1')))
        self.assertIsNone(self.changes('def f():\n return lambda: 1', 'def f():\n return lambda: (yield 1)'))


if __name__ == '__main__':
    unittest.main()

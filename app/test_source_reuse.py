import ast
import unittest
from source_reuse import SourceSpans, reuse_statements


class ReuseTests(unittest.TestCase):
    def check(self, source, modified):
        before, after = ast.parse(source), ast.parse(modified)
        original = ast.dump(before, include_attributes=True)
        expected = ast.dump(after, include_attributes=False)
        result = reuse_statements(before, after, SourceSpans(source), modified)
        self.assertEqual(ast.dump(result, include_attributes=False), expected)
        self.assertEqual(ast.dump(before, include_attributes=True), original)
        return before, result

    def test_unchanged_functions_are_shared_after_line_shifts(self):
        before, after = self.check('def f():\n return 1\ndef g():\n return 2\n', '# extra\n\ndef f():\n return 1\ndef g():\n return 3\n')
        self.assertIs(before.body[0], after.body[0])
        self.assertIsNot(before.body[1], after.body[1])

    def test_decorators_cannot_be_hidden_by_matching_declaration_spans(self):
        before, after = self.check('@one\ndef f():\n return 1\n', '@two\ndef f():\n return 1\n')
        self.assertIsNot(before.body[0], after.body[0])
        self.check('@one\nclass X:\n pass\n', '@two\nclass X:\n pass\n')

    def test_headers_unicode_and_nested_statements(self):
        self.check('def f(x=1):\n s="é"; return x\n', 'def f(x=2):\n s="é"; return x\n')
        before, after = self.check('class X:\n def f(self):\n  return "é"\n def g(self):\n  return 2\n', 'class X:\n def f(self):\n  return "é"\n def g(self):\n  return 3\n')
        self.assertIs(before.body[0].body[0], after.body[0].body[0])

    def test_reordering_or_new_scopes_preserves_semantic_ast(self):
        self.check('x=1\ny=2\n', 'y=2\nx=1\n')
        self.check('def f():\n x=1\n return x\n', 'def f():\n if True:\n  x=1\n return x\n')


if __name__ == '__main__':
    unittest.main()

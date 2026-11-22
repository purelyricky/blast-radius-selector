import ast
import random
import unittest

from build_predicate_values import Recorder, Rewrite, merge
from predicate_values import OPS, PredicateRefiner, changed_comparisons, fingerprint, unchanged


class PredicateTests(unittest.TestCase):
    def test_numeric_bounds_cover_interior_boundaries(self):
        self.assertFalse(unchanged(['n', 0, 10], ('lt', 1, 4), ('lt', 1, 5)))
        self.assertTrue(unchanged(['n', 0, 3], ('lt', 1, 4), ('lt', 1, 5)))
        self.assertFalse(unchanged(['n', 0, 4], ('lt', 1, 4), ('le', 1, 4)))
        self.assertTrue(unchanged(['n', 0, 3], ('lt', 1, 4), ('le', 1, 4)))

    def test_numeric_proof_has_no_false_exclusions(self):
        rng = random.Random(918)
        for _ in range(2000):
            a, b = sorted(rng.sample(range(-20, 21), 2))
            old, new = (rng.choice(list(OPS)), rng.randrange(2), rng.randrange(-25, 26)), (rng.choice(list(OPS)), 0, rng.randrange(-25, 26))
            new = (new[0], old[1], new[2])
            if unchanged(['n', a, b], old, new):
                for value in [a + i/4 for i in range(4*(b-a)+1)]:
                    x = OPS[old[0]](old[2], value) if old[1] == 0 else OPS[old[0]](value, old[2])
                    y = OPS[new[0]](new[2], value) if new[1] == 0 else OPS[new[0]](value, new[2])
                    self.assertEqual(x, y)

    def test_large_integer_and_float_boundaries(self):
        n = 2**80
        self.assertFalse(unchanged(['n', n, n+1], ('eq', 1, n), ('eq', 1, n+1)))
        self.assertFalse(unchanged(['n', .1, .2], ('lt', 1, .15), ('lt', 1, .16)))

    def test_string_hash_presence_only_overselects(self):
        state = ['s', [fingerprint('seen')]]
        self.assertTrue(unchanged(state, ('eq', 1, 'absent'), ('eq', 1, 'also absent')))
        self.assertFalse(unchanged(state, ('eq', 1, 'seen'), ('eq', 1, 'absent')))
        self.assertFalse(unchanged(state, ('eq', 1, 'absent'), ('ne', 1, 'also absent')))
        self.assertFalse(unchanged(['?'], ('eq', 1, 'x'), ('eq', 1, 'y')))

    def test_unchanged_operand_and_whole_module_required(self):
        old = ast.parse('def f(x):\n return x < 4\n')
        self.assertIsNotNone(changed_comparisons(old, ast.parse('def f(x):\n return x <= 5\n'), 'x.py'))
        self.assertIsNone(changed_comparisons(old, ast.parse('def f(x):\n return y <= 5\n'), 'x.py'))
        self.assertIsNone(changed_comparisons(old, ast.parse('def f(x):\n return x <= 5\nz=1\n'), 'x.py'))

    def test_import_and_unsafe_contexts(self):
        metadata = {'version': 1, 'revision': 'r', 'ids': ['a','b'], 'groups': [['n',0,2], ['n',4,4]], 'sites': {'x.py:2:8': {'tests': [[0,0],[1,1]], 'import': 0}}, 'unsafe': [0]}
        refiner = PredicateRefiner(metadata, 'r', ['a','b'])
        before, after = ast.parse('def f(x):\n return x < 4'), ast.parse('def f(x):\n return x <= 4')
        self.assertEqual(refiner.select(before, after, 'x.py'), {0,1})
        metadata['sites']['x.py:2:8']['import'] = 1
        self.assertIsNone(refiner.select(before, after, 'x.py'))

    def test_probe_preserves_original_comparison_result_and_unknowns(self):
        recorder = Recorder()
        class Custom:
            def __lt__(self, other):
                return 'custom-result'
        self.assertEqual(recorder.compare('site', 'lt', 1, Custom(), 4), 'custom-result')
        self.assertEqual(recorder.states[('site','')], ['?'])
        self.assertEqual(merge(['n',0,2], ['n',1,4]), ['n',0,4])
        self.assertEqual(merge(['n',0,2], ['s',[]]), ['?'])

    def test_future_annotations_are_not_transformed(self):
        tree = ast.parse('from __future__ import annotations\ndef f(x: A < 2) -> B == 3:\n return x < 4')
        rewritten = Rewrite('x.py', set(), future=True).visit(tree)
        self.assertIsInstance(rewritten.body[1].args.args[0].annotation, ast.Compare)
        self.assertIsInstance(rewritten.body[1].returns, ast.Compare)
        self.assertIsInstance(rewritten.body[1].body[0].value, ast.Call)


if __name__ == '__main__':
    unittest.main()

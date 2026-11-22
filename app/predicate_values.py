"""Replay pure comparisons against bounded baseline primitive observations.

Only whole-module edits confined to one-operator comparisons with an unchanged
dynamic operand are eligible. Unknown values and import-time changes retain
the ordinary selector. Metadata is generated from the pinned source alone.
"""
import ast
from fractions import Fraction
import hashlib
import math
import operator

OPS = {name: getattr(operator, name) for name in ('eq', 'ne', 'lt', 'le', 'gt', 'ge')}
NAMES = {ast.Eq: 'eq', ast.NotEq: 'ne', ast.Lt: 'lt', ast.LtE: 'le', ast.Gt: 'gt', ast.GtE: 'ge'}


def literal(node):
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return None
    if type(value) in (int, bool) and int(value).bit_length() <= 512:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str:
        return value
    return None


def description(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or type(node.ops[0]) not in NAMES:
        return None
    left, right = literal(node.left), literal(node.comparators[0])
    if left is None and right is None:
        return NAMES[type(node.ops[0])], 2, None
    if left is not None and right is not None:
        return None
    side = 0 if left is not None else 1
    return NAMES[type(node.ops[0])], side, left if side == 0 else right


def site_key(path, node):
    return f'{path}:{node.lineno}:{node.col_offset}'


def fingerprint(value):
    return hashlib.blake2b(value.encode('utf-8', 'surrogatepass'), digest_size=12).hexdigest()


def unchanged(state, old, new):
    """True only when every value represented by state gives the same result."""
    if not state or state[0] == '?':
        return False
    old_op, side, old_constant = old
    new_op, new_side, new_constant = new
    if side != new_side:
        return False
    if state[0] == 'r' and side == 2:
        def outcome(op, relation):
            return op == 'ne' if relation is None else OPS[op](relation, 0)
        return all(outcome(old_op, relation) == outcome(new_op, relation) for n, relation in enumerate((-1, 0, 1, None)) if state[1] & (1 << n))
    def result(op, constant, value):
        return OPS[op](constant, value) if side == 0 else OPS[op](value, constant)
    if state[0] == 'n' and type(old_constant) in (int, bool, float) and type(new_constant) in (int, bool, float):
        low, high = Fraction(state[1]), Fraction(state[2])
        old_constant, new_constant = Fraction(old_constant), Fraction(new_constant)
        points = sorted({low, high} | {x for x in (old_constant, new_constant) if low <= x <= high})
        probes = points + [(a+b)/2 for a, b in zip(points, points[1:])]
        return all(result(old_op, old_constant, value) == result(new_op, new_constant, value) for value in probes)
    if state[0] == 's' and type(old_constant) is str and type(new_constant) is str:
        # A hash collision can only add a potential match, so cannot cause an
        # unsafe exclusion. Presence is deliberately treated as uncertain.
        observed = state[1]
        if fingerprint(old_constant) in observed or fingerprint(new_constant) in observed:
            return False
        if old_op in ('eq', 'ne') and new_op in ('eq', 'ne'):
            return old_op == new_op
        return old_constant == new_constant and frozenset((old_op, new_op)) in (frozenset(('lt', 'le')), frozenset(('gt', 'ge')))
    return False


def changed_comparisons(before, after, path):
    changes = []
    def equal(a, b):
        if isinstance(a, ast.AST) and isinstance(b, ast.AST):
            if ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False):
                return True
            if type(a) is not type(b):
                return False
            if isinstance(a, ast.Compare):
                old, new = description(a), description(b)
                if old is None or new is None or old[1] != new[1]:
                    return False
                old_dynamic = [a.left, a.comparators[0]] if old[1] == 2 else [a.comparators[0] if old[1] == 0 else a.left]
                new_dynamic = [b.left, b.comparators[0]] if new[1] == 2 else [b.comparators[0] if new[1] == 0 else b.left]
                if any(ast.dump(x, include_attributes=False) != ast.dump(y, include_attributes=False) for x, y in zip(old_dynamic, new_dynamic)):
                    return False
                changes.append((site_key(path, a), old, new, a))
                return True
            return all(equal(getattr(a, field), getattr(b, field)) for field in a._fields)
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
        return type(a) is type(b) and a == b
    return changes if equal(before, after) and changes else None


class PredicateRefiner:
    def __init__(self, metadata, revision, ids):
        if metadata.get('version') != 2 or metadata.get('revision') != revision or metadata.get('ids') != ids:
            raise ValueError('predicate metadata differs from baseline')
        self.metadata = metadata

    def select(self, before, after, path, coverage=None):
        changes = changed_comparisons(before, after, path)
        if changes is None:
            return None
        selected = set()
        unsafe = set(self.metadata.get('unsafe', []))
        groups = self.metadata['groups']
        for key, old, new, node in changes:
            if unsafe:
                selected.update(unsafe if coverage is None else unsafe.intersection(coverage(node)))
            site = self.metadata['sites'].get(key)
            if site is None:
                return None
            imported = site.get('import')
            if imported is not None and not unchanged(groups[imported], old, new):
                return None
            for index, group in site['tests']:
                if not unchanged(groups[group], old, new):
                    selected.add(index)
        return selected

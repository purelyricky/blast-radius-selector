"""Comparison-operator equivalence proven from baseline execution.

`a is b` and `a == b` give the same answer whenever both operands' classes use
the default object equality (identity). Which types actually reach each
comparison site is observed once by running the pinned baseline suite with the
sites instrumented (build time only). A site whose every observed operand pair
is default-equality is "proven": swapping is<->== (is not<->!=) there cannot
change any test. Unobserved sites are never listed. Production applies the
result only by normalising the operators of proven sites in the before/after
ASTs, so any other edit still goes through coverage selection.

No patch, commit id or edit-kind table is involved.
"""
from __future__ import annotations

import ast

ELIGIBLE = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)


def iter_functions(tree):
    """Yield (qualname, node) for every function, stable across small edits."""
    seen = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                count = seen.get(name, 0)
                seen[name] = count + 1
                qual = name if count == 0 else f"{name}#{count}"
                yield qual, child
                yield from visit(child, name + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                yield from visit(child, f"{prefix}{child.name}.")
            else:
                yield from visit(child, prefix)

    yield from visit(tree, "")


def eligible_compares(function):
    """Single-operator comparisons owned by `function` (not nested defs), in walk order."""
    result = []
    stack = [function]
    while stack:
        node = stack.pop(0)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Compare) and len(child.ops) == 1 and isinstance(child.ops[0], ELIGIBLE):
                result.append(child)
            stack.append(child)
    return result


def site_keys(path, tree):
    """Map id(Compare node) -> site key string."""
    keys = {}
    for qual, function in iter_functions(tree):
        for ordinal, node in enumerate(eligible_compares(function)):
            keys[id(node)] = f"{path}::{qual}::{ordinal}"
    return keys


def normalize(tree, path, proven):
    """Return a copy of `tree` with proven comparison sites canonicalised (is->==, is not->!=)."""
    import copy

    tree = copy.deepcopy(tree)
    if not proven:
        return tree
    for qual, function in iter_functions(tree):
        for ordinal, node in enumerate(eligible_compares(function)):
            if f"{path}::{qual}::{ordinal}" in proven:
                op = node.ops[0]
                if isinstance(op, ast.Is):
                    node.ops[0] = ast.Eq()
                elif isinstance(op, ast.IsNot):
                    node.ops[0] = ast.NotEq()
    return tree


# ---------------------------------------------------------------- build time

_OPS = {ast.Is: "is", ast.IsNot: "is not", ast.Eq: "==", ast.NotEq: "!="}


class _Rewriter(ast.NodeTransformer):
    def __init__(self, keys):
        self.keys = keys

    def visit_Compare(self, node):
        self.generic_visit(node)
        key = self.keys.get(id(node))
        if key is None:
            return node
        call = ast.Call(
            func=ast.Name(id="__eqprobe__", ctx=ast.Load()),
            args=[ast.Constant(key), ast.Constant(_OPS[type(node.ops[0])]), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


def instrument_source(path, source):
    tree = ast.parse(source)
    keys = site_keys(path, tree)
    # generic_visit above replaces children, so look keys up before transforming.
    return ast.fix_missing_locations(_Rewriter(keys).visit(tree)), len(keys)


class Probe:
    """Evaluates the flipped operator next to the original at every is/==/!= site.

    A site is "proven" when every observed operand pair gives the same answer
    under both forms (so swapping them cannot change any test); otherwise the
    tests during which the answers differed are recorded per site.
    """

    def __init__(self):
        self.seen = set()
        self.differ = {}
        self.current = ""
        self.busy = False

    def __call__(self, key, op, left, right):
        self.seen.add(key)
        if op == "is":
            result = left is right
        elif op == "is not":
            result = left is not right
        elif op == "==":
            result = left == right
        else:
            result = left != right
        if self.busy:
            # Observed while evaluating another site's alternative form (e.g. the `is` inside
            # Expr.__eq__ itself): the interaction is unknown, so this site is never proven.
            self.differ.setdefault(key, set()).add("")
            return result
        self.busy = True
        try:
            if op == "is":
                other = left == right
            elif op == "is not":
                other = left != right
            elif op == "==":
                other = left is right
            else:
                other = left is not right
            same = other is result or (type(other) is bool and type(result) is bool and other == result)
        except BaseException:
            same = False
        finally:
            self.busy = False
        if not same:
            self.differ.setdefault(key, set()).add(self.current)
        return result

    def proven(self):
        return sorted(self.seen - set(self.differ))


_SWAPS = (frozenset((ast.Is, ast.Eq)), frozenset((ast.IsNot, ast.NotEq)))


def flip_sites(before, after, keys):
    """(site key, old Compare node) of the comparisons whose only difference is an is<->== / is not<->!= swap.

    Returns None when anything else differs (operands, statements, literals,
    other operators), so the caller keeps the ordinary selection.
    """
    sites = []
    pending = [(before, after)]
    while pending:
        old, new = pending.pop()
        if old is new:
            continue
        if type(old) is not type(new):
            return None
        if isinstance(old, ast.AST):
            skip = False
            if isinstance(old, ast.Compare) and len(old.ops) == len(new.ops) == 1 and type(old.ops[0]) is not type(new.ops[0]):
                if frozenset((type(old.ops[0]), type(new.ops[0]))) not in _SWAPS or id(old) not in keys:
                    return None
                sites.append((keys[id(old)], old))
                skip = True
            for field in old._fields:
                if not (skip and field == "ops"):
                    pending.append((getattr(old, field, None), getattr(new, field, None)))
        elif isinstance(old, list):
            if len(old) != len(new):
                return None
            pending.extend(zip(old, new))
        elif old != new:
            return None
    return sites or None

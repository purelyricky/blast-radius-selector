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
    def __init__(self):
        self.unsafe = set()
        self.seen = set()
        self.cache = {}

    def __call__(self, key, op, left, right):
        pair = (type(left), type(right))
        safe = self.cache.get(pair)
        if safe is None:
            safe = self.classify(pair)
            self.cache[pair] = safe
        self.seen.add(key)
        if not safe:
            self.unsafe.add(key)
        if op == "is":
            return left is right
        if op == "is not":
            return left is not right
        if op == "==":
            return left == right
        return left != right

    @staticmethod
    def classify(pair):
        import enum

        def default_eq(t):
            return t is type(None) or (t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__)

        def plain_enum(t):
            # Members are unique per value, so equal (even through str/int
            # mixins) means the very same member. Flags can build new members.
            return (
                issubclass(t, enum.Enum)
                and not issubclass(t, enum.Flag)
                and t.__eq__ in (object.__eq__, str.__eq__, int.__eq__, enum.Enum.__eq__)
                and t.__ne__ in (object.__ne__, str.__ne__, int.__ne__, enum.Enum.__ne__)
            )

        ta, tb = pair
        if default_eq(ta) and default_eq(tb):
            return True
        if ta is tb and plain_enum(ta):
            return True
        none = type(None)
        return (ta is none and plain_enum(tb)) or (tb is none and plain_enum(ta))

    def proven(self):
        return sorted(self.seen - self.unsafe)

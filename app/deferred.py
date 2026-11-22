"""Edits confined to bodies of lambdas / nested functions inside a function.

A lambda or nested def is *created* when its enclosing function runs (possibly
at import time) but its body only runs when the resulting callable is called.
When everything except such bodies is identical, the change can only be seen
by tests that execute those bodies (their lines carry test contexts in the
baseline coverage), provided the callable is not itself invoked while
importing.
"""
import ast
import copy

_DEFERRED = (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef)


def _mask(function):
    function = copy.deepcopy(function)
    stack = [function]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Lambda):
                child.body = ast.Constant(None)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child.body = [ast.Pass()]
            else:
                stack.append(child)
                continue
            stack.append(child)  # decorators, defaults and annotations stay eager
    return function


def _outermost(node):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFERRED):
            yield child
        else:
            yield from _outermost(child)


def deferred_body_changes(old_function, new_function):
    """Old nested callables whose bodies differ, or None if any eager part differs."""
    if ast.dump(_mask(old_function), include_attributes=False) != ast.dump(_mask(new_function), include_attributes=False):
        return None
    old_nodes, new_nodes = list(_outermost(old_function)), list(_outermost(new_function))
    if len(old_nodes) != len(new_nodes):
        return None
    changed = []
    for old, new in zip(old_nodes, new_nodes):
        if type(old) is not type(new):
            return None
        if ast.dump(old, include_attributes=False) != ast.dump(new, include_attributes=False):
            changed.append(old)
    return changed or None

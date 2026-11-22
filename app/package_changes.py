"""Prove that a package initializer changes only deferred function bodies."""
import ast
import copy


def body_only(before, after):
    def compare(old_body, new_body):
        if len(old_body) != len(new_body):
            return False
        for old, new in zip(old_body, new_body):
            if ast.dump(old, include_attributes=False) == ast.dump(new, include_attributes=False):
                continue
            if type(old) is not type(new) or not isinstance(old, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            old_header, new_header = copy.copy(old), copy.copy(new)
            old_header.body, new_header.body = [], []
            if ast.dump(old_header, include_attributes=False) != ast.dump(new_header, include_attributes=False):
                return False
            if ast.get_docstring(old, clean=False) != ast.get_docstring(new, clean=False):
                return False
            if isinstance(old, ast.ClassDef) and not compare(old.body, new.body):
                return False
        return True
    return compare(before.body, after.body)


def generator_kind(function):
    pending = list(function.body) if isinstance(function.body, list) else [function.body]
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return False


def nested_body_only(before, after):
    if type(before) is not type(after) or not isinstance(before, (ast.FunctionDef, ast.AsyncFunctionDef)) or before.decorator_list or after.decorator_list:
        return False
    old_header, new_header = copy.copy(before), copy.copy(after)
    old_header.body, new_header.body = [], []
    return ast.dump(old_header, include_attributes=False) == ast.dump(new_header, include_attributes=False) and ast.get_docstring(before, clean=False) == ast.get_docstring(after, clean=False) and generator_kind(before) == generator_kind(after)


def region_imported(function, region, imported_lines):
    start = function.body[0].lineno if region is function else region.lineno
    return any(start <= line <= region.end_lineno for line in imported_lines)

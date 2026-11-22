"""Select tests using baseline-derived source and execution dependencies.

This module never imports sqlglot and never reads sample failing-test answers.
"""
from __future__ import annotations

import ast
from collections import defaultdict
import copy
import gzip
import json
from pathlib import Path

from patches import InvalidPatch, apply_patch, parse_patch


class BroadChange(ValueError):
    pass


def _dump(node):
    return ast.dump(node, include_attributes=False)


def changed_functions(before, after, pairs=False):
    """Find body-only function edits; reject structural/import-time changes."""
    result = []
    def compare(old_body, new_body):
        if len(old_body) != len(new_body):
            raise BroadChange("scope members added or removed")
        for old, new in zip(old_body, new_body):
            if _dump(old) == _dump(new):
                continue
            if type(old) is not type(new):
                raise BroadChange("statement kind changed")
            if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)):
                old_header, new_header = copy.copy(old), copy.copy(new)
                old_header.body, new_header.body = [], []
                if _dump(old_header) != _dump(new_header):
                    raise BroadChange("function header changed")
                if ast.get_docstring(old, clean=False) != ast.get_docstring(new, clean=False):
                    raise BroadChange("function metadata changed")
                if any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(new)):
                    raise BroadChange("function can alter shared lexical state")
                result.append((old, new) if pairs else old)
            elif isinstance(old, ast.ClassDef):
                old_header, new_header = copy.copy(old), copy.copy(new)
                old_header.body, new_header.body = [], []
                if _dump(old_header) != _dump(new_header):
                    raise BroadChange("class header changed")
                compare(old.body, new.body)
            else:
                raise BroadChange("import-time statement changed")
    compare(before.body, after.body)
    return result


def body_regions(before, after):
    """Narrow body edits to stable statement regions that execute before them.

    A changed predicate selects every test that evaluated that predicate,
    regardless of its old branch. Insertions/removals select the enclosing
    old scope, so code without an old line never disappears from selection.
    """
    regions = []
    def sequence(old_body, new_body, parent):
        if len(old_body) != len(new_body):
            regions.append(parent)
            return
        for old, new in zip(old_body, new_body):
            if _dump(old) == _dump(new):
                continue
            if type(old) is not type(new):
                regions.append(old)
                continue
            if isinstance(old, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                old_header, new_header = copy.copy(old), copy.copy(new)
                old_header.body, new_header.body = [], []
                if hasattr(old, "orelse"):
                    old_header.orelse, new_header.orelse = [], []
                if _dump(old_header) == _dump(new_header):
                    sequence(old.body, new.body, old)
                    if hasattr(old, "orelse"):
                        sequence(old.orelse, new.orelse, old)
                else:
                    # The complete old statement includes its entry line,
                    # including tests which previously skipped the body.
                    regions.append(old)
            elif isinstance(old, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                old_header, new_header = copy.copy(old), copy.copy(new)
                for node in (old_header, new_header):
                    node.body, node.orelse, node.finalbody = [], [], []
                    node.handlers = [copy.copy(handler) for handler in node.handlers]
                    for handler in node.handlers:
                        handler.body = []
                if _dump(old_header) == _dump(new_header):
                    sequence(old.body, new.body, old)
                    sequence(old.orelse, new.orelse, old)
                    sequence(old.finalbody, new.finalbody, old)
                    for old_handler, new_handler in zip(old.handlers, new.handlers):
                        sequence(old_handler.body, new_handler.body, old)
                else:
                    regions.append(old)
            else:
                regions.append(old)
    sequence(before.body, after.body, before)
    return regions


class DependencySelector:
    def __init__(self, tests, map_path, precision="function"):
        self.tests = tests
        self.precision = precision
        self.all_numbers = {test["number"] for test in tests}
        self.by_file = defaultdict(set)
        for test in tests:
            self.by_file[test["file"].replace("\\", "/")].add(test["number"])
        opener = gzip.open if Path(map_path).suffix == ".gz" else open
        with opener(map_path, "rt", encoding="utf-8") as handle:
            self.map = json.load(handle)
        if self.map.get("version") not in (1, 2) or self.map.get("revision") != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
            raise ValueError("incompatible dependency map")
        number_by_id = {test["id"]: test["number"] for test in tests}
        self.numbers = [number_by_id.get(nodeid) for nodeid in self.map["ids"]]
        self.always = self.all_numbers - set(self.numbers)
        self.always.update(self._numbers(self.map.get("always", [])))
        self.source_trees = {}
        self.function_tests = {}
        self.last_reason = ""

    def _numbers(self, indices):
        return {self.numbers[index] for index in indices if self.numbers[index] is not None}

    def _indices(self, group):
        return self.map["groups"][group] if self.map["version"] == 2 else group

    def _function_tests(self, path, node):
        key = (path, node.lineno, node.end_lineno)
        if key not in self.function_tests:
            lines = self.map["lines"].get(path, {})
            indices = set()
            for lineno in range(node.lineno, node.end_lineno + 1):
                if str(lineno) in lines:
                    indices.update(self._indices(lines[str(lineno)]))
            self.function_tests[key] = self._numbers(indices)
        return self.function_tests[key]

    def select(self, patch_text):
        try:
            patches = parse_patch(patch_text)
            if len({patch.old for patch in patches}) != len(patches):
                raise BroadChange("repeated file diff")
            selected = set(self.always)
            scopes = 0
            for patch in patches:
                path = patch.old
                if patch.new != path or path.endswith(("/__init__.py", "/conftest.py")) or path in ("conftest.py", "tests/helpers.py"):
                    raise BroadChange("global setup, package initializer, or renamed file")
                if path in self.map["reads"]:
                    selected.update(self._numbers(self._indices(self.map["reads"][path])))
                if path not in self.map["sources"]:
                    if path.startswith("tests/fixtures/") and path in self.map["reads"]:
                        continue
                    raise BroadChange("unknown source or data dependency")
                source = self.map["sources"][path]
                modified = apply_patch(source, patch)
                # Compile bytes too: a changed encoding cookie can invalidate
                # an otherwise unchanged AST parsed from a Unicode string.
                compile(modified.encode("utf-8"), path, "exec", dont_inherit=True)
                before = self.source_trees.get(path)
                if before is None:
                    before = ast.parse(source, filename=path)
                    self.source_trees[path] = before
                after = ast.parse(modified, filename=path)
                if path.startswith("tests/"):
                    selected.update(self.by_file.get(path, self.all_numbers))
                    continue
                functions = changed_functions(before, after, pairs=True)
                for node, new_node in functions:
                    body_start = node.body[0].lineno
                    if any(body_start <= line <= node.end_lineno for line in self.map.get("import_lines", {}).get(path, [])):
                        raise BroadChange("changed function executes during module import")
                    regions = body_regions(node, new_node) if self.precision == "statement" else [node]
                    for region in regions:
                        selected.update(self._function_tests(path, region))
                scopes += len(functions)
            self.last_reason = f"coverage of {scopes} changed function scopes and fixture readers"
            return selected
        except (InvalidPatch, BroadChange, SyntaxError, UnicodeError, ValueError, TypeError, RecursionError, MemoryError) as error:
            self.last_reason = f"full-suite fallback: {type(error).__name__}: {error}"
            return self.all_numbers

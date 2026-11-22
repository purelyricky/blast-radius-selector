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


def changed_functions(before, after):
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
                result.append(old)
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


class DependencySelector:
    def __init__(self, tests, map_path):
        self.tests = tests
        self.all_numbers = {test["number"] for test in tests}
        self.by_file = defaultdict(set)
        for test in tests:
            self.by_file[test["file"].replace("\\", "/")].add(test["number"])
        with gzip.open(map_path, "rt", encoding="utf-8") as handle:
            self.map = json.load(handle)
        if self.map.get("version") != 1 or self.map.get("revision") != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
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

    def _function_tests(self, path, node):
        key = (path, node.lineno, node.end_lineno)
        if key not in self.function_tests:
            lines = self.map["lines"].get(path, {})
            indices = set()
            for lineno in range(node.lineno, node.end_lineno + 1):
                indices.update(lines.get(str(lineno), []))
            self.function_tests[key] = self._numbers(indices)
        return self.function_tests[key]

    def select(self, patch_text):
        try:
            patches = parse_patch(patch_text)
            selected = set(self.always)
            scopes = 0
            for patch in patches:
                path = patch.old
                if patch.new != path or path.endswith(("/__init__.py", "/conftest.py")) or path in ("conftest.py", "tests/helpers.py"):
                    raise BroadChange("global setup, package initializer, or renamed file")
                selected.update(self._numbers(self.map["reads"].get(path, [])))
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
                functions = changed_functions(before, after)
                for node in functions:
                    body_start = node.body[0].lineno
                    if any(body_start <= line <= node.end_lineno for line in self.map.get("import_lines", {}).get(path, [])):
                        raise BroadChange("changed function executes during module import")
                    selected.update(self._function_tests(path, node))
                scopes += len(functions)
            self.last_reason = f"coverage of {scopes} changed function scopes and fixture readers"
            return selected
        except (InvalidPatch, BroadChange, SyntaxError, UnicodeError, ValueError, TypeError, RecursionError, MemoryError) as error:
            self.last_reason = f"full-suite fallback: {type(error).__name__}: {error}"
            return self.all_numbers

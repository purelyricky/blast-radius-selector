"""Select tests using baseline-derived source and execution dependencies.

This module never imports sqlglot and never reads sample failing-test answers.
"""
from __future__ import annotations

import ast
import re
from collections import defaultdict
import copy
import gzip
import json
from functools import lru_cache
from pathlib import Path

from compare_equivalence import equivalent_edits
from equivalence import flip_sites, site_keys
from deferred import deferred_body_changes
from membership import membership_changes, name_regex
from family import change_ok, docstring_only, header_default_changes, known_names
from patches import InvalidPatch, apply_patch, parse_patch
from package_changes import body_only as package_body_only, generator_kind, nested_body_only, region_imported
from source_reuse import SourceSpans, reuse_statements


_INERT = re.compile(r"^(docs/.*|\.github/.*|posts/.*|[^/]*\.(md|rst)|LICENSE|CHANGELOG[^/]*)$")
_PYTEST_CONFIG = ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "setup.py")
_FAMILY = re.compile(r"^sqlglot/(dialects|parsers|generators|typing)/([a-z0-9_]+)\.py$")
_COMPARE = re.compile(r"^[+-](?![+-]{2}).*(==|!=)", re.M)  # an is<->== swap always shows one of these
_PREDICATE = re.compile(r"^[+-](?![+-]{2}).*(==|!=|<|>)", re.M)


class BroadChange(ValueError):
    pass


@lru_cache(maxsize=8192)
def _dump(node):
    return ast.dump(node, include_attributes=False)


def ast_equal(a, b):
    """Structural equality ignoring positions (same relation as comparing ast.dump strings, without building them)."""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for field in a._fields:
            if not ast_equal(getattr(a, field, None), getattr(b, field, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(ast_equal(x, y) for x, y in zip(a, b))
    return a == b


def _is_literal(node):
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def literal_only_change(old, new):
    """True when old/new differ only in same-type constant leaves, structure unchanged."""
    if dump_cached(old) == dump_cached(new):
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        # Same-type, non-degenerate leaf swap (e.g. -1 -> -2, 16 -> 17, 'a' -> 'b'):
        # no structure changes, so nothing downstream sees a new shape or type.
        a, b = old.value, new.value
        return type(a) is type(b) and not (isinstance(a, (str, bytes)) and (not a or not b))
    for field in old._fields:
        a, b = getattr(old, field, None), getattr(new, field, None)
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b) or not all(literal_only_change(x, y) if isinstance(x, ast.AST) and isinstance(y, ast.AST) else x == y for x, y in zip(a, b)):
                return False
        elif isinstance(a, ast.AST) and isinstance(b, ast.AST):
            if not literal_only_change(a, b):
                return False
        elif a != b:
            return False
    return True


def dump_cached(node):
    return _dump(node) if isinstance(node, ast.AST) else node


def changed_functions(before, after, pairs=False):
    """Find body-only function edits; reject structural/import-time changes."""
    result = []
    def compare(old_body, new_body):
        if len(old_body) != len(new_body):
            raise BroadChange("scope members added or removed")
        for old, new in zip(old_body, new_body):
            if ast_equal(old, new):
                continue
            if type(old) is not type(new):
                raise BroadChange("statement kind changed")
            if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)):
                old_header, new_header = copy.copy(old), copy.copy(new)
                old_header.body, new_header.body = [], []
                if not ast_equal(old_header, new_header):
                    raise BroadChange("function header changed")
                if ast.get_docstring(old, clean=False) != ast.get_docstring(new, clean=False):
                    raise BroadChange("function metadata changed")
                if generator_kind(old) != generator_kind(new):
                    raise BroadChange("callable generator kind changed")
                if any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(new)):
                    raise BroadChange("function can alter shared lexical state")
                result.append((old, new) if pairs else old)
            elif isinstance(old, ast.ClassDef):
                old_header, new_header = copy.copy(old), copy.copy(new)
                old_header.body, new_header.body = [], []
                if not ast_equal(old_header, new_header):
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
            if ast_equal(old, new):
                continue
            if type(old) is not type(new):
                regions.append(old)
                continue
            if nested_body_only(old, new):
                regions.extend(body_regions(old, new))
                continue
            if isinstance(old, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                old_header, new_header = copy.copy(old), copy.copy(new)
                old_header.body, new_header.body = [], []
                if hasattr(old, "orelse"):
                    old_header.orelse, new_header.orelse = [], []
                if ast_equal(old_header, new_header):
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
                if ast_equal(old_header, new_header):
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
        self.number_by_id = number_by_id
        self.numbers = [number_by_id.get(nodeid) for nodeid in self.map["ids"]]
        self.always = self.all_numbers - set(self.numbers)
        self.always.update(self._numbers(self.map.get("always", [])))
        self.source_trees = {}
        self.source_spans = {}
        self.import_lines = self.map.get("import_lines", {})
        execution_path = Path(map_path).with_name("import_execution.json")
        if execution_path.exists():
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            if execution.get("revision") == self.map["revision"]:
                self.import_lines = {path: sorted(set(self.import_lines.get(path, [])) | set(execution["lines"].get(path, []))) for path in self.map["sources"]}
        self.function_tests = {}
        self.last_reason = ""
        self.refiner = None
        self.shared_refiner = None
        self.family_names = None
        self._mentions_pytest = False
        self._map_path = Path(map_path)
        self._shared_loaded = False
        self._shared_metadata = None
        self._predicate_loaded = False
        self._predicate_refiner = None
        self.proven_sites = frozenset()
        self.flip_differ = None
        self.equivalence_keys = {}
        eq_path = Path(map_path).with_name("eq_sites.json")
        if eq_path.exists():
            eq = json.loads(eq_path.read_text(encoding="utf-8"))
            if eq.get("revision") == self.map["revision"]:
                self.proven_sites = frozenset(eq["proven"])
                if "differ" in eq:
                    self.flip_differ = eq["differ"]

    @property
    def shared_metadata(self):
        """Per-class reader metadata, loaded on first use (most patches never need it)."""
        if not self._shared_loaded:
            self._shared_loaded = True
            path = self._map_path.with_name("shared_reads.json.gz")
            if not path.exists():
                path = self._map_path.with_name("shared_reads.json")
            if path.exists():
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rt", encoding="utf-8") as handle:
                    shared = json.load(handle)
                if shared.get("version") == 2 and shared.get("revision") == self.map["revision"] and shared.get("ids") == self.map["ids"]:
                    self._shared_metadata = shared
        return self._shared_metadata

    @property
    def predicate_refiner(self):
        if not self._predicate_loaded:
            self._predicate_loaded = True
            path = self._map_path.with_name("predicate_values.json.gz")
            if path.exists():
                from predicate_values import PredicateRefiner
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    self._predicate_refiner = PredicateRefiner(json.load(handle), self.map["revision"], self.map["ids"])
        return self._predicate_refiner

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

    def _coverage_indices(self, path, node):
        lines = self.map['lines'].get(path, {})
        return {index for number in range(node.lineno, node.end_lineno + 1) if str(number) in lines for index in self._indices(lines[str(number)])}

    def _with_readers(self, patch_text, selected):
        """Add tests that read the patched file as data (e.g. TestTyping inspects code constants)."""
        for patch in parse_patch(patch_text):
            if patch.old in self.map["reads"]:
                selected.update(self._numbers(self._indices(self.map["reads"][patch.old])))
        return selected

    def _flip_tests(self, site):
        """Test numbers whose baseline run saw is/== disagree at `site`; None if any context cannot be resolved."""
        numbers = set()
        for context in self.flip_differ.get(site, ()):
            if context.startswith("scope:"):
                scope = context[len("scope:"):]
                members = [number for nodeid, number in self.number_by_id.items() if nodeid == scope or nodeid.startswith(scope + "::")]
                if not members:
                    return None
                numbers.update(members)
            else:
                number = self.number_by_id.get(context)
                if number is None:  # import/collection time ('') or an unknown test
                    return None
                numbers.add(number)
        return numbers

    def _inert_path(self, path):
        """A path no baseline test can observe: never imported, opened or collected."""
        if path in self.map["sources"] or path in self.map["reads"]:
            return False
        if _INERT.match(path):
            return True
        name = path.rsplit("/", 1)[-1]
        if name in _PYTEST_CONFIG:
            return False
        if path.startswith("tests/"):
            return path.endswith(".py") and name != "__init__.py"  # a new test module is not in tests.json
        if path.startswith("sqlglot/"):
            # A brand-new module nothing in the baseline imports (dialect packages auto-register, keep those broad).
            return path.endswith(".py") and name != "__init__.py" and not _FAMILY.match(path)
        return True  # repository metadata outside the packages: pyproject, setup, Makefile, benchmarks, ...

    def _inert_tier(self, patch_text):
        """Paths no test can read: prose docs, CI metadata, packaging files, unreferenced new modules."""
        try:
            patches = parse_patch(patch_text)
        except InvalidPatch:
            return False
        if not patches:
            return False
        self._mentions_pytest = "pytest" in patch_text.lower()
        for patch in patches:
            paths = {path for path in (patch.old, patch.new) if path != "/dev/null"}
            if not paths or not all(self._inert_path(path) for path in paths):
                return False
        return True

    def _docstring_tier(self, patch_text):
        try:
            patches = parse_patch(patch_text)
            for patch in patches:
                source = self.map["sources"].get(patch.old)
                if source is None or patch.new != patch.old or not patch.old.startswith("sqlglot/") or patch.old.endswith("/__init__.py"):
                    return False
                if not docstring_only(ast.parse(source), ast.parse(apply_patch(source, patch))):
                    return False
            return bool(patches)
        except (InvalidPatch, SyntaxError, ValueError):
            return False

    def _membership_tier(self, patch_text):
        """Added/removed whole definitions: select tests that execute lines mentioning the names."""
        try:
            patches = parse_patch(patch_text)
            selected = set(self.always)
            for patch in patches:
                source = self.map["sources"].get(patch.old)
                if source is None or patch.new != patch.old or not patch.old.startswith("sqlglot/") or patch.old.endswith("/__init__.py"):
                    return None
                modified = apply_patch(source, patch)
                after = compile(modified.encode('utf-8'), patch.old, 'exec', flags=ast.PyCF_ONLY_AST, dont_inherit=True)
                compile(after, patch.old, 'exec', dont_inherit=True)
                found = membership_changes(ast.parse(source), after)
                if found is None:
                    return None
                names, removed = found
                for node in removed:  # tests that ran a removed definition
                    selected.update(self._function_tests(patch.old, node))
                excluded = {(patch.old, line) for node in removed for line in range(node.lineno, node.end_lineno + 1)}
                removed_names = set()
                for node in removed:
                    removed_names.add(node.name if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None)
                    if isinstance(node, ast.Assign):
                        removed_names.update(t.id for t in node.targets)
                removed_names.discard(None)
                if names:
                    # Changed bindings can affect module initialization through
                    # both removal and shadowing of names used by eager code.
                    hazard = name_regex(names)
                    for path, text in self.map["sources"].items():
                        if not hazard.search(text):
                            continue
                        import_lines = set(self.import_lines.get(path, ()))
                        for number, line in enumerate(text.splitlines(), 1):
                            if number in import_lines and (path, number) not in excluded and hazard.search(line):
                                return None
                pattern = name_regex(names)
                for path, lines in self.map["lines"].items():
                    text = self.map["sources"].get(path)
                    if text is None or not pattern.search(text):
                        continue
                    for number, line in enumerate(text.splitlines(), 1):
                        if (path, number) not in excluded and pattern.search(line):
                            group = lines.get(str(number))
                            if group is not None:
                                selected.update(self._numbers(self._indices(group)))
                # Test modules that mention the name textually are readers as well.
                for path, text in self.map["sources"].items():
                    if path.startswith("tests/") and pattern.search(text):
                        selected.update(self.by_file.get(path, ()))
            return selected if patches else None
        except (InvalidPatch, SyntaxError, ValueError):
            return None

    def _header_tier(self, patch_text):
        """Same-type literal default-value edits: only callers of the function are affected."""
        try:
            patches = parse_patch(patch_text)
            selected = set(self.always)
            for patch in patches:
                source = self.map["sources"].get(patch.old)
                if source is None or patch.new != patch.old or not patch.old.startswith("sqlglot/") or patch.old.endswith("/__init__.py"):
                    return None
                functions = header_default_changes(ast.parse(source), ast.parse(apply_patch(source, patch)))
                if functions is None:
                    return None
                import_lines = self.map.get("import_lines", {}).get(patch.old, [])
                for node in functions:
                    if any(node.body[0].lineno <= line <= node.end_lineno for line in import_lines):
                        return None
                    selected.update(self._function_tests(patch.old, node))
            return selected if patches else None
        except (InvalidPatch, SyntaxError, ValueError):
            return None

    def _family_tier(self, patch_text):
        """Middle tier for pure-literal edits to a dialect's own modules.

        Only used when every changed node is a literal (constants, tuples,
        lists, dicts) so the edit cannot raise at import time. A test can then
        depend on the changed value only by using that dialect (or a dialect
        inheriting from it, found through the static import graph), which
        executes code in the dialect's modules (dialects/, parsers/,
        generators/, typing/ <name>.py) or lives in its test file. Select every
        test that executed a line in those modules during the baseline plus the
        dialect's test file. Anything else keeps the full-suite fallback.
        """
        try:
            patches = parse_patch(patch_text)
        except InvalidPatch:
            return None
        selected = set(self.always)
        names = set()
        for patch in patches:
            match = _FAMILY.match(patch.old)
            if patch.new != patch.old or not match or match.group(2) in ("dialect", "__init__"):
                return None
            source = self.map["sources"].get(patch.old)
            if source is None:
                return None
            try:
                after = ast.parse(apply_patch(source, patch))
            except (SyntaxError, ValueError, InvalidPatch):
                return None
            before = self.source_trees.get(patch.old) or ast.parse(source)
            if self.family_names is None:
                self.family_names = known_names(self.map["sources"])
            if not change_ok(before, after, self.family_names):
                return None
            names.add(match.group(2))
        # Inheriting dialects: any family module that imports a changed family module.
        family_files = {path: _FAMILY.match(path).group(2) for path in self.map["lines"] if _FAMILY.match(path)}
        grew = True
        while grew:
            grew = False
            changed_paths = {path for path, name in family_files.items() if name in names}
            for path, imported in self.map.get("imports", {}).items():
                match = _FAMILY.match(path)
                if match and match.group(2) not in names and match.group(2) not in ("dialect", "__init__") and changed_paths.intersection(imported):
                    names.add(match.group(2))
                    grew = True
        for name in names:
            members = [path for path, member in family_files.items() if member == name]
            if not members:
                return None
            for path in members:
                selected.update(self._numbers(i for g in self.map["lines"][path].values() for i in self._indices(g)))
            selected.update(self.by_file.get(f"tests/dialects/test_{name}.py", ()))
        return selected

    def select(self, patch_text):
        try:
            patches = parse_patch(patch_text)
            if len({patch.old for patch in patches}) != len(patches):
                raise BroadChange("repeated file diff")
            selected = set(self.always)
            scopes = 0
            for patch in patches:
                path = patch.old
                if patch.new != path or path.endswith("/conftest.py") or path in ("conftest.py", "tests/helpers.py"):
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
                after = compile(modified.encode("utf-8"), path, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
                compile(after, path, "exec", dont_inherit=True)
                before = self.source_trees.get(path)
                if before is None:
                    before = ast.parse(source, filename=path)
                    self.source_trees[path] = before
                if path not in self.source_spans:
                    self.source_spans[path] = SourceSpans(source)
                after = reuse_statements(before, after, self.source_spans[path], modified)
                if path.endswith("/__init__.py") and not package_body_only(before, after):
                    raise BroadChange("package initializer has eager or structural changes")
                if path.startswith("tests/"):
                    selected.update(self.by_file.get(path, self.all_numbers))
                    continue
                if self.proven_sites and len(patches) == 1 and _COMPARE.search(patch_text):
                    # is<->== swaps at sites whose observed operands all use
                    # identity equality cannot change any test (equivalence.py).
                    if path not in self.equivalence_keys:
                        self.equivalence_keys[path] = site_keys(path, before)
                    if equivalent_edits(before, after, path, self.proven_sites, self.equivalence_keys[path]):
                        continue
                    flips = flip_sites(before, after, self.equivalence_keys[path])
                    if flips is not None and self.flip_differ is not None:
                        # Swap observed to change an answer only in these tests (baseline evaluation of both forms).
                        # Sites running while importing, or with unresolvable contexts, keep the ordinary selection.
                        import_lines = self.import_lines.get(path, ())
                        refined = set()
                        for site, node in flips:
                            if site in self.flip_differ and any(node.lineno <= line <= node.end_lineno for line in import_lines):
                                refined = None
                                break
                            tests = self._flip_tests(site)
                            if tests is None:
                                refined = None
                                break
                            refined.update(tests)
                        if refined is not None:
                            selected.update(refined)
                            scopes += len(flips)
                            continue
                if self.predicate_refiner is not None and len(patches) == 1 and _PREDICATE.search(patch_text):
                    indices = self.predicate_refiner.select(before, after, path, coverage=lambda node: self._coverage_indices(path, node))
                    if indices is not None:
                        selected.update(self._numbers(indices))
                        scopes += 1
                        continue
                deferred = []
                try:
                    functions = changed_functions(before, after, pairs=True)
                except BroadChange:
                    if self.refiner is None:
                        from elements import ElementRefiner
                        self.refiner = ElementRefiner(self.map)
                    normalized, deferred = self.refiner.normalize(path, before, after)
                    try:
                        functions = changed_functions(before, normalized, pairs=True)
                    except BroadChange:
                        if self.shared_metadata is None:
                            raise
                        if self.shared_refiner is None:
                            from shared import SharedRefiner
                            self.shared_refiner = SharedRefiner(self.map["sources"], self.shared_metadata)
                        normalized, shared_indices = self.shared_refiner.normalize(path, before, normalized)
                        functions = changed_functions(before, normalized, pairs=True)
                        selected.update(self._numbers(shared_indices))
                for region_path, region in deferred:
                    selected.update(self._function_tests(region_path, region))
                scopes += len(deferred)
                for node, new_node in functions:
                    if any(region_imported(node, node, self.import_lines.get(path, [])) for _ in (0,)):
                        changed = deferred_body_changes(node, new_node)
                        if changed is not None:
                            if self.refiner is None:
                                from elements import ElementRefiner
                                self.refiner = ElementRefiner(self.map)
                            if not any(self.refiner.imported_callable(path, nested) for nested in changed):
                                for nested in changed:
                                    selected.update(self._function_tests(path, nested))
                                scopes += 1
                                continue
                    regions = body_regions(node, new_node) if self.precision == "statement" else [node]
                    for region in regions:
                        if region_imported(node, region, self.import_lines.get(path, [])):
                            raise BroadChange("changed region executes during module import")
                        selected.update(self._function_tests(path, region))
                scopes += len(functions)
            self.last_reason = f"coverage of {scopes} changed function scopes and fixture readers"
            return selected
        except (InvalidPatch, BroadChange, SyntaxError, UnicodeError, ValueError, TypeError, RecursionError, MemoryError) as error:
            self.last_reason = f"full-suite fallback: {type(error).__name__}: {error}"
            if isinstance(error, BroadChange):
                if self._inert_tier(patch_text):
                    self.last_reason = "documentation-only paths"
                    return set(self.always)
                if self._docstring_tier(patch_text):
                    self.last_reason = "docstring-only edits"
                    return self._with_readers(patch_text, set(self.always))
                members = self._membership_tier(patch_text)
                if members is not None:
                    self.last_reason = "name reachability of added/removed definitions"
                    return self._with_readers(patch_text, members)
                headers = self._header_tier(patch_text)
                if headers is not None:
                    self.last_reason = "literal default values of changed functions"
                    return self._with_readers(patch_text, headers)
                family = self._family_tier(patch_text)
                if family is not None:
                    self.last_reason = f"dialect-family touch set after: {error}"
                    return family
            return self.all_numbers

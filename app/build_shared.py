"""Observe baseline readers of class values without altering the checkout.

An import loader wraps reads of source-declared class fields. Each read keeps
the original attribute semantics and records the concrete class and its MRO,
so inherited fields and class setup are attributed conservatively.
"""
import argparse
import ast
import builtins
from collections import defaultdict
import concurrent.futures
import importlib.abc
import importlib.machinery
import importlib
import json
import os
from pathlib import Path
import sys

from build_map import InstrumentedExecutor, PINNED, module_name


class Rewrite(ast.NodeTransformer):
    def __init__(self, fields, future):
        self.fields = fields
        self.future = future
        self.classname = ""

    def visit_ClassDef(self, node):
        previous = self.classname
        self.classname = node.name
        result = self.generic_visit(node)
        self.classname = previous
        return result

    def visit_arg(self, node):
        if node.annotation is not None and not self.future:
            node.annotation = self.visit(node.annotation)
        return node

    def visit_FunctionDef(self, node):
        returns = node.returns
        if self.future:
            node.returns = None
        result = self.generic_visit(node)
        if self.future:
            result.returns = returns
        return result

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node):
        annotation = node.annotation
        if self.future:
            node.annotation = ast.Constant(None)
        result = self.generic_visit(node)
        if self.future:
            result.annotation = annotation
        return result

    def visit_Attribute(self, node):
        node = self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load) or node.attr not in self.fields:
            return node
        name = node.attr
        if name.startswith("__") and not name.endswith("__") and self.classname:
            name = "_" + self.classname.lstrip("_") + name
        result = ast.Call(func=ast.Name(id="__br_shared_attr__", ctx=ast.Load()), args=[node.value, ast.Constant(name)], keywords=[])
        return ast.copy_location(result, node)

    def visit_Call(self, node):
        node = self.generic_visit(node)
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in ("getattr", "hasattr", "vars", "getmembers", "getmembers_static", "getattr_static"):
            result = ast.Call(func=ast.Name(id="__br_dynamic_attr__", ctx=ast.Load()), args=[node.func, ast.Constant(name)] + node.args, keywords=node.keywords)
            return ast.copy_location(result, node)
        return node


def class_fields(trees):
    fields = set()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for statement in node.body:
                    if isinstance(statement, ast.Assign):
                        fields.update(target.id for target in statement.targets if isinstance(target, ast.Name))
                    elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                        fields.add(statement.target.id)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id in ("cls", "klass"):
                        fields.add(target.attr)
    return fields | {"__dict__", "__mro__"}


class Recorder:
    def __init__(self, modules, fields):
        self.modules = modules
        self.fields = fields
        self.context = ""
        self.items = []
        self.failed = set()
        self.used = defaultdict(set)
        self.field_used = defaultdict(set)
        self.metaclasses = defaultdict(set)
        self.imports = defaultdict(set)
        self.type_keys = {}
        self.seen = set()
        self.import_depth = 0

    def record(self, obj, name, caller):
        owner = obj if isinstance(obj, type) else type(obj)
        keys = self.type_keys.get(owner)
        if keys is None:
            keys = []
            for base in type.__getattribute__(owner, "__mro__"):
                module = type.__getattribute__(base, "__module__")
                if module in self.modules:
                    keys.append(self.modules[module] + "::" + type.__getattribute__(base, "__qualname__"))
            self.type_keys[owner] = keys
        if not keys:
            return
        if self.context and not self.import_depth:
            marker = (owner, self.context)
            if marker not in self.seen:
                self.seen.add(marker)
                for key in keys:
                    self.used[key].add(self.context)
            if name in self.fields:
                field_marker = (owner, name, self.context)
                if field_marker not in self.seen:
                    self.seen.add(field_marker)
                    for key in keys:
                        self.field_used[key + "::" + name].add(self.context)
        if (not self.context or self.import_depth) and name in self.fields:
            code = caller.f_code
            source_path = code.co_filename.replace("\\", "/")
            if "/sqlglot/" in source_path:
                source_path = "sqlglot/" + source_path.split("/sqlglot/", 1)[1]
            elif "/tests/" in source_path:
                source_path = "tests/" + source_path.split("/tests/", 1)[1]
            for key in keys:
                self.imports[key + "::" + name].add((source_path, code.co_firstlineno, code.co_qualname))
                for meta in type.__getattribute__(type(owner), "__mro__"):
                    self.metaclasses[key].add(type.__getattribute__(meta, "__module__") + "::" + type.__getattribute__(meta, "__qualname__"))

    def pytest_collection_modifyitems(self, items):
        self.items = [item.nodeid for item in items]

    def pytest_runtest_setup(self, item):
        self.context = "scope:" + item.parent.nodeid

    def pytest_runtest_call(self, item):
        self.context = item.nodeid

    def pytest_runtest_teardown(self, item):
        self.context = "scope:" + item.parent.nodeid

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root, trees, fields, recorder):
        self.root = root
        self.trees = trees
        self.fields = fields
        self.recorder = recorder

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in ("sqlglot", "tests"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return None
        try:
            relative = Path(spec.origin).relative_to(self.root).as_posix()
        except ValueError:
            return None
        if relative not in self.trees:
            return None
        fields = self.fields
        origin = spec.origin
        recorder = self.recorder
        class Loader(importlib.machinery.SourceFileLoader):
            def exec_module(self, module):
                recorder.import_depth += 1
                try:
                    return super().exec_module(module)
                finally:
                    recorder.import_depth -= 1

            def get_code(self, fullname):
                tree = ast.parse(Path(origin).read_text(encoding="utf-8"), filename=origin)
                future = any(isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names) for node in tree.body)
                rewritten = Rewrite(fields, future).visit(tree)
                return compile(ast.fix_missing_locations(rewritten), origin, "exec", dont_inherit=True)
        spec.loader = Loader(fullname, origin)
        return spec


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--imports-only", action="store_true")
    args = parser.parse_args()
    root = args.source.resolve()
    if (root / ".git/HEAD").read_text().strip() != PINNED:
        raise RuntimeError("source checkout must be detached at the pinned revision")
    output = args.output.resolve()
    trees = {path.relative_to(root).as_posix(): ast.parse(path.read_text(encoding="utf-8")) for package in ("sqlglot", "tests") for path in (root / package).rglob("*.py")}
    fields = class_fields(trees)
    modules = {module_name(path): path for path in trees}
    recorder = Recorder(modules, fields)
    get_attribute = builtins.getattr
    def attribute(obj, name):
        recorder.record(obj, name, sys._getframe(1))
        return get_attribute(obj, name)
    def dynamic(function, kind, *arguments, **keywords):
        if arguments and kind in ("vars", "getmembers", "getmembers_static"):
            recorder.record(arguments[0], "__dict__", sys._getframe(1))
        elif len(arguments) >= 2 and isinstance(arguments[1], str):
            recorder.record(arguments[0], arguments[1], sys._getframe(1))
        return function(*arguments, **keywords)
    builtins.__br_shared_attr__ = attribute
    builtins.__br_dynamic_attr__ = dynamic
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder(root, trees, fields, recorder))
    concurrent.futures.ProcessPoolExecutor = InstrumentedExecutor
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.chdir(root)
    import pytest
    options = ["tests", "-q", "--assert=plain", "--tb=short", "-p", "no:cacheprovider"]
    if args.imports_only:
        options.append("--collect-only")
    code = pytest.main(options, plugins=[recorder])
    if code:
        raise RuntimeError(f"instrumented baseline failed: {sorted(recorder.failed)}")
    if args.imports_only:
        for module, path in sorted(modules.items()):
            if module.startswith("sqlglot") and not path.endswith("/__main__.py"):
                importlib.import_module(module)
    ids = sorted(recorder.items)
    index = {nodeid: n for n, nodeid in enumerate(ids)}
    expanded = {nodeid: [n] for nodeid, n in index.items()}
    def expand(context):
        if context not in expanded:
            scope = context.removeprefix("scope:")
            expanded[context] = [n for nodeid, n in index.items() if nodeid == scope or nodeid.startswith(scope + "::")]
        return expanded[context]
    used = {key: sorted({n for context in contexts for n in expand(context)}) for key, contexts in recorder.used.items()}
    field_used = {key: sorted({n for context in contexts for n in expand(context)}) for key, contexts in recorder.field_used.items()}
    imports = {key: sorted(consumers) for key, consumers in recorder.imports.items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 2, "revision": PINNED, "ids": ids, "used": used, "fields": field_used, "imports": imports, "metaclasses": {key: sorted(value) for key, value in recorder.metaclasses.items()}}, separators=(",", ":")), encoding="utf-8")
    print(f"Shared-field map: {len(ids)} tests, {len(used)} classes, {len(imports)} imported fields, {output.stat().st_size} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()

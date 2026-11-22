"""Observe baseline primitive comparison inputs; never consume patches/answers."""
import argparse
import ast
import builtins
import concurrent.futures
import gzip
import importlib
import importlib.abc
import importlib.machinery
import json
import math
import os
from pathlib import Path
import sys
import tracemalloc

from build_map import InstrumentedExecutor, PINNED, module_name
from predicate_values import OPS, description, fingerprint, site_key


class Rewrite(ast.NodeTransformer):
    def __init__(self, path, sites, future=False):
        self.path, self.sites, self.future = path, sites, future

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

    def visit_Compare(self, node):
        desc = description(node)
        node = self.generic_visit(node)
        if desc is None:
            return node
        key = site_key(self.path, node)
        self.sites.add(key)
        call = ast.Call(func=ast.Name(id='__br_predicate__', ctx=ast.Load()), args=[ast.Constant(key), ast.Constant(desc[0]), ast.Constant(desc[1]), node.left, node.comparators[0]], keywords=[])
        return ast.copy_location(call, node)


class Recorder:
    def __init__(self):
        self.context = ''
        self.import_depth = 0
        self.items = []
        self.failed = set()
        self.sites = set()
        self.states = {}
        self.unsafe = set()
        self.hashes = {}

    def compare(self, key, op, side, left, right):
        value = right if side == 0 else left
        context = '' if self.import_depth else self.context
        if context not in self.unsafe:
            if tracemalloc.is_tracing():
                self.unsafe.add(context)
            else:
                marker = (key, context)
                state = self.states.get(marker)
                if state is None or state[0] != '?':
                    kind = type(value)
                    def numeric(item):
                        return type(item) in (int, bool) and int(item).bit_length() <= 512 or type(item) is float and math.isfinite(item)
                    if side == 2:
                        if numeric(left) and numeric(right) or type(left) is str and type(right) is str:
                            relation = 0 if left == right else -1 if left < right else 1
                            bit = 1 << (relation + 1)
                            if state is None:
                                self.states[marker] = ['r', bit]
                            elif state[0] == 'r':
                                state[1] |= bit
                            else:
                                self.states[marker] = ['?']
                        else:
                            self.states[marker] = ['?']
                    elif numeric(value):
                        if state is None:
                            self.states[marker] = ['n', value, value]
                        elif state[0] != 'n':
                            self.states[marker] = ['?']
                        else:
                            state[1], state[2] = min(state[1], value), max(state[2], value)
                    elif kind is str:
                        if state is not None and state[0] != 's':
                            self.states[marker] = ['?']
                        else:
                            if state is None:
                                state = self.states[marker] = ['s', set()]
                            hashed = self.hashes.get(value)
                            if hashed is None:
                                hashed = fingerprint(value)
                                if len(self.hashes) >= 4096:
                                    self.hashes.clear()
                                self.hashes[value] = hashed
                            state[1].add(hashed)
                            if len(state[1]) > 5000:
                                self.states[marker] = ['?']
                    else:
                        self.states[marker] = ['?']
        return OPS[op](left, right)

    def pytest_collection_modifyitems(self, items):
        self.items = [item.nodeid for item in items]

    def pytest_runtest_setup(self, item):
        self.context = 'scope:' + item.parent.nodeid

    def pytest_runtest_call(self, item):
        self.context = item.nodeid

    def pytest_runtest_teardown(self, item):
        self.context = 'scope:' + item.parent.nodeid

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self, root, recorder):
        self.root, self.recorder = root, recorder

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] != 'sqlglot':
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not spec.origin or not spec.origin.endswith('.py'):
            return None
        try:
            relative = Path(spec.origin).relative_to(self.root).as_posix()
        except ValueError:
            return None
        origin, recorder = spec.origin, self.recorder
        class Loader(importlib.machinery.SourceFileLoader):
            def exec_module(self, module):
                recorder.import_depth += 1
                try:
                    return super().exec_module(module)
                finally:
                    recorder.import_depth -= 1

            def get_code(self, fullname):
                tree = ast.parse(Path(origin).read_text(encoding='utf-8'), filename=origin)
                future = any(isinstance(node, ast.ImportFrom) and node.module == '__future__' and any(alias.name == 'annotations' for alias in node.names) for node in tree.body)
                tree = Rewrite(relative, recorder.sites, future).visit(tree)
                return compile(ast.fix_missing_locations(tree), origin, 'exec', dont_inherit=True)
        spec.loader = Loader(fullname, origin)
        return spec


def merge(a, b):
    if a is None:
        return b
    if a[0] != b[0] or a[0] == '?':
        return ['?']
    if a[0] == 'n':
        return ['n', min(a[1], b[1]), max(a[2], b[2])]
    if a[0] == 'r':
        return ['r', a[1] | b[1]]
    values = set(a[1]) | set(b[1])
    return ['s', sorted(values)] if len(values) <= 5000 else ['?']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if (root / '.git/HEAD').read_text().strip() != PINNED:
        raise RuntimeError('source checkout must be detached at pinned revision')
    recorder = Recorder()
    builtins.__br_predicate__ = recorder.compare
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder(root, recorder))
    concurrent.futures.ProcessPoolExecutor = InstrumentedExecutor
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.chdir(root)
    import pytest
    code = pytest.main(['tests', '-q', '--assert=plain', '--tb=short', '-p', 'no:cacheprovider'], plugins=[recorder])
    if code:
        raise RuntimeError(f'instrumented baseline failed: {sorted(recorder.failed)}')
    recorder.context = ''
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        if path.name != '__main__.py':
            importlib.import_module(module_name(path.relative_to(root).as_posix()))
    ids = sorted(recorder.items)
    index = {nodeid: n for n, nodeid in enumerate(ids)}
    expanded = {nodeid: [n] for nodeid, n in index.items()}
    def expand(context):
        if context not in expanded:
            scope = context.removeprefix('scope:')
            expanded[context] = [n for nodeid, n in index.items() if nodeid == scope or nodeid.startswith(scope + '::')]
        return expanded[context]
    profiles = {key: {'tests': {}} for key in recorder.sites}
    for (key, context), state in recorder.states.items():
        if state[0] == 's':
            state = ['s', sorted(state[1])]
        if not context:
            profiles[key]['import'] = state
        else:
            for n in expand(context):
                profiles[key]['tests'][n] = merge(profiles[key]['tests'].get(n), state)
    groups, lookup = [], {}
    def intern(state):
        encoded = json.dumps(state, separators=(',', ':'))
        if encoded not in lookup:
            lookup[encoded] = len(groups)
            groups.append(state)
        return lookup[encoded]
    for site in profiles.values():
        site['tests'] = [[n, intern(state)] for n, state in sorted(site['tests'].items())]
        if 'import' in site:
            site['import'] = intern(site['import'])
        if '' in recorder.unsafe:
            site['import'] = intern(['?'])
    payload = {'version': 1, 'revision': PINNED, 'ids': ids, 'groups': groups, 'sites': profiles, 'unsafe': sorted({n for context in recorder.unsafe if context for n in expand(context)})}
    output.parent.mkdir(parents=True, exist_ok=True)
    opener = gzip.open if output.suffix == '.gz' else open
    with opener(output, 'wt', encoding='utf-8') as stream:
        json.dump(payload, stream, separators=(',', ':'))
    print(f'Predicate profiles: {len(ids)} tests, {len(profiles)} sites, {len(groups)} states, {output.stat().st_size} bytes; unsafe={payload["unsafe"]}', file=sys.stderr)


if __name__ == '__main__':
    main()

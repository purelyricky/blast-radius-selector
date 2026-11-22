"""Independent full-suite validation of a generated source mutation (development)."""
import argparse
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

from build_map import PINNED
from patches import apply_patch, parse_patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--tests', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if (root/'.git/HEAD').read_text().strip() != PINNED:
        raise RuntimeError('pinned checkout required')
    case = json.loads(args.case.read_text())
    patches = parse_patch(case['patch'])
    modified = {patch.old: apply_patch((root/patch.old).read_text(encoding='utf-8'), patch) for patch in patches}
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] != 'sqlglot':
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or not spec.origin:
                return None
            relative = Path(spec.origin).relative_to(root).as_posix()
            if relative not in modified:
                return None
            origin = spec.origin
            class Loader(importlib.machinery.SourceFileLoader):
                def get_code(self, fullname):
                    return compile(modified[relative], origin, 'exec', dont_inherit=True)
            spec.loader = Loader(fullname, origin)
            return spec
    class Reports:
        def __init__(self):
            self.failed, self.errors = set(), []
        def pytest_runtest_logreport(self, report):
            if report.failed:
                self.failed.add(report.nodeid)
        def pytest_collectreport(self, report):
            if report.failed:
                self.errors.append(report.nodeid)
    tests = json.loads(args.tests.read_text())['tests']
    numbers = {test['id']: test['number'] for test in tests}
    reports = Reports()
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    os.chdir(root)
    import pytest
    code = pytest.main(['tests', '-q', '--assert=plain', '--tb=short', '-p', 'no:cacheprovider'], plugins=[reports])
    failed = {numbers[nodeid] for nodeid in reports.failed if nodeid in numbers}
    missed = sorted(failed - set(case['selected']))
    result = {'case': case['site'], 'exit': int(code), 'failures': len(failed), 'missed': missed, 'collection_errors': reports.errors}
    output.write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(result), file=sys.stderr)
    if missed or reports.errors or int(code) not in (0,1):
        raise SystemExit(1)


if __name__ == '__main__':
    main()

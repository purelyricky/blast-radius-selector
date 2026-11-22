"""Independent full-suite validation of a generated source mutation (development)."""
import argparse
import json
import os
from pathlib import Path
import sys

from build_map import PINNED
from mutation_overlay import install


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
    os.environ['BR_MUTATION_SOURCE'] = str(root)
    os.environ['BR_MUTATION_CASE'] = str(args.case.resolve())
    install(root, args.case.resolve())
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


if os.environ.get('BR_MUTATION_SOURCE'):
    # multiprocessing 'spawn' executes this script before unpickling pool
    # jobs, so their test modules import the same modified SQLGlot source.
    install(os.environ['BR_MUTATION_SOURCE'], os.environ['BR_MUTATION_CASE'])

if __name__ == '__main__':
    main()

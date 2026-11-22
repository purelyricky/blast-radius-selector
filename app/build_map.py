"""Build dependency metadata from unmodified sqlglot at the pinned revision.

Run from this repository (never from an extracted download):
    python app/build_map.py --source vendor/sqlglot --output artifacts/map.json.gz
The map records pytest node IDs, never benchmark test numbers or patch answers.
"""
from __future__ import annotations

import argparse
import ast
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import sys
import time

PINNED = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def module_name(path):
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def imports_for(path, tree, modules):
    result = set()
    package = module_name(path) if path.endswith("/__init__.py") else module_name(path).rpartition(".")[0]
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                prefix = ".".join(parts[: len(parts) - node.level + 1])
                base = ".".join(filter(None, (prefix, node.module)))
            else:
                base = node.module or ""
            names = [base] + [base + "." + alias.name for alias in node.names]
        for name in names:
            while name:
                if name in modules:
                    result.add(modules[name])
                name = name.rpartition(".")[0]
    parts = path.split("/")[:-1]
    for count in range(1, len(parts) + 1):
        parent = "/".join(parts[:count]) + "/__init__.py"
        if parent in modules.values():
            result.add(parent)
    return result


class Recorder:
    def __init__(self, root, cov):
        self.root = root
        self.prefix = str(root) + os.sep
        self.cov = cov
        self.context = ""
        self.items = []
        self.failed = set()
        self.reads = defaultdict(set)
        self.path_cache = {}

    def switch(self, context):
        self.context = context
        self.cov.switch_context(context)

    def audit(self, event, args):
        if event != "open" or not self.context or not isinstance(args[0], (str, bytes)):
            return
        path = os.fsdecode(args[0])
        relative = self.path_cache.get(path)
        if relative is None:
            absolute = os.path.abspath(path)
            relative = absolute[len(self.prefix):].replace(os.sep, "/") if absolute.startswith(self.prefix) else ""
            self.path_cache[path] = relative
        if relative and not relative.startswith(".git/") and "__pycache__" not in relative:
            self.reads[relative].add(self.context)

    def pytest_collection_modifyitems(self, items):
        self.items = [item.nodeid for item in items]

    def pytest_runtest_setup(self, item):
        # unittest setUpClass runs here only for the first test. Expand this
        # context to the entire class, including every fixture reader.
        self.switch("scope:" + item.parent.nodeid)

    def pytest_runtest_call(self, item):
        self.switch(item.nodeid)

    def pytest_runtest_teardown(self, item):
        self.switch("scope:" + item.parent.nodeid)

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, default=Path("artifacts/.coverage"))
    parser.add_argument("--reuse", action="store_true", help="reuse completed coverage and recorder files")
    args = parser.parse_args()
    root = args.source.resolve()
    output = args.output.resolve()
    coverage_path = args.coverage.resolve()
    coverage_path.parent.mkdir(parents=True, exist_ok=True)
    recorder_path = coverage_path.with_suffix(".recorder.json")
    started = time.monotonic()
    import coverage
    cov = coverage.Coverage(data_file=str(coverage_path), source=[str(root / "sqlglot"), str(root / "tests")], config_file=False)
    if args.reuse:
        cov.load()
        recorded = json.loads(recorder_path.read_text())
    else:
        import pytest
        recorder = Recorder(root, cov)
        sys.addaudithook(recorder.audit)
        sys.path.insert(0, str(root))
        os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        old_cwd = Path.cwd()
        os.chdir(root)
        cov.start()
        try:
            code = pytest.main(["tests", "-q", "--tb=short", "-p", "no:cacheprovider"], plugins=[recorder])
        finally:
            cov.stop()
            cov.save()
            os.chdir(old_cwd)
        recorded = {"items": recorder.items, "failed": sorted(recorder.failed), "reads": {k: sorted(v) for k, v in recorder.reads.items()}, "exit": int(code)}
        recorder_path.write_text(json.dumps(recorded), encoding="utf-8")

    ids = sorted(recorded["items"])
    if recorded["exit"] not in (0, 1) or not ids:
        raise RuntimeError(f"Incomplete test collection: pytest exit {recorded['exit']}")
    index = {nodeid: n for n, nodeid in enumerate(ids)}
    expanded = {nodeid: [n] for nodeid, n in index.items()}
    def expand(context):
        if context not in expanded:
            scope = context.removeprefix("scope:")
            expanded[context] = [n for nodeid, n in index.items() if nodeid == scope or nodeid.startswith(scope + "::")] if context.startswith("scope:") else []
        return expanded[context]

    sources = {}
    for package in ("sqlglot", "tests"):
        for path in sorted((root / package).rglob("*.py")):
            sources[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    modules = {module_name(p): p for p in sources}
    imports = {path: sorted(imports_for(path, ast.parse(source), modules)) for path, source in sources.items()}
    data = cov.get_data()
    lines = {}
    import_lines = {}
    for absolute in sorted(data.measured_files()):
        try:
            relative = Path(absolute).relative_to(root).as_posix()
        except ValueError:
            continue
        if relative not in sources:
            continue
        file_lines = {}
        import_lines[relative] = []
        for line, contexts in data.contexts_by_lineno(absolute).items():
            if "" in contexts:
                import_lines[relative].append(line)
            tests = sorted({n for context in contexts for n in expand(context)})
            if tests:
                file_lines[str(line)] = tests
        lines[relative] = file_lines
    reads = {path: sorted({n for context in contexts for n in expand(context)}) for path, contexts in recorded["reads"].items()}
    if not any(lines.values()):
        raise RuntimeError("No test coverage recorded")
    result = {"version": 1, "revision": PINNED, "ids": ids, "always": [index[nodeid] for nodeid in recorded["failed"]], "sources": sources, "imports": imports, "lines": lines, "import_lines": import_lines, "reads": reads}
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(result, handle, separators=(",", ":"), ensure_ascii=False)
    print(f"Map: {len(ids)} tests, {len(sources)} files, {sum(map(len, lines.values()))} covered lines, {len(reads)} read files, {len(recorded['failed'])} baseline failures; {output.stat().st_size} bytes; {time.monotonic() - started:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()

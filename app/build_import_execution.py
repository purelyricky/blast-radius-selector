"""Collect executable lines of every pinned module's initialization."""
import argparse
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys

from build_map import PINNED, module_name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.source.resolve(), args.output.resolve()
    if (root / ".git/HEAD").read_text().strip() != PINNED:
        raise RuntimeError("source checkout must be detached at the pinned revision")
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import coverage
    import pytest
    tracer = coverage.Coverage(data_file=None, source=[str(root / "sqlglot")], config_file=False)
    tracer.start()
    with contextlib.redirect_stdout(io.StringIO()):
        code = pytest.main(["tests", "--collect-only", "-q", "-p", "no:cacheprovider"])
    if code:
        raise RuntimeError("incomplete baseline test collection")
    for path in sorted((root / "sqlglot").rglob("*.py")):
        if path.name != "__main__.py":
            importlib.import_module(module_name(path.relative_to(root).as_posix()))
    tracer.stop()
    data = tracer.get_data()
    lines = {Path(path).relative_to(root).as_posix(): sorted(data.lines(path) or []) for path in data.measured_files() if Path(path).is_relative_to(root)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"version": 1, "revision": PINNED, "lines": lines}, separators=(",", ":")), encoding="utf-8")
    print(f"Import execution: {len(lines)} modules, {sum(map(len, lines.values()))} lines, {output.stat().st_size} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()

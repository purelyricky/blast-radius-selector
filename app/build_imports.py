"""Observe callable elements executed while importing the pinned baseline.

This supplements line coverage: lambda creation and invocation can share a
source line, so ordinary line coverage cannot separate their import effects.
"""
import argparse
import importlib
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.source.resolve()
    sys.path.insert(0, str(root))
    calls = set()
    failed = []
    prefix = str(root) + "/"
    def profile(frame, event, arg):
        if event == "call":
            code = frame.f_code
            filename = code.co_filename.replace("\\", "/")
            if filename.startswith(prefix) and code.co_name != "<module>":
                calls.add((filename[len(prefix):], code.co_firstlineno, code.co_name))
    sys.setprofile(profile)
    try:
        for path in sorted((root / "sqlglot").rglob("*.py")):
            if path.name == "__main__.py":
                continue
            name = path.relative_to(root).as_posix().removesuffix(".py").replace("/", ".").removesuffix(".__init__")
            try:
                importlib.import_module(name)
            except Exception:
                failed.append(path.relative_to(root).as_posix())
    finally:
        sys.setprofile(None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"calls": sorted(calls), "failed": failed}, separators=(",", ":")), encoding="utf-8")
    print(f"Observed {len(calls)} import-time callables; {len(failed)} unavailable modules", file=sys.stderr)


if __name__ == "__main__":
    main()

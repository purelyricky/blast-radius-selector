"""Validate a dependency map against an external sample kit (not map input)."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dependencies import DependencySelector
from packer import pack


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--precision", choices=("function", "statement"), default="function")
    args = parser.parse_args()
    started = time.process_time()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    selector = DependencySelector(tests, args.map, precision=args.precision)
    items = {test["number"]: (test["number"], round(test["seconds"] * 1000)) for test in tests}
    with args.output.open("w", encoding="utf-8", newline="\n") as output:
        for path in sorted((args.data / "commits").glob("*.patch")):
            selected = selector.select(path.read_text(encoding="utf-8"))
            workers = pack([items[n] for n in sorted(selected)])
            print(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")), file=output)
            print(f"{path.stem}: {len(selected)}/{len(tests)} tests; {selector.last_reason}", file=sys.stderr)
    print(f"CPU {time.process_time() - started:.3f}s", file=sys.stderr)


if __name__ == "__main__":
    main()

import json, sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from packer import pack

DATA = Path(os.environ.get("BR_DATA", "/data"))


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    items = [(t["number"], round(t["seconds"] * 1000)) for t in tests]
    item_by_number = dict((item[0], item) for item in items)
    all_numbers = frozenset(item_by_number)
    cache = {all_numbers: pack(items)}
    selector = None
    map_path = Path(os.environ.get("BR_MAP", str(Path(__file__).with_name("dependency_map.json"))))
    try:
        from dependencies import DependencySelector
        selector = DependencySelector(tests, map_path, precision=os.environ.get("BR_PRECISION", "statement"))
    except Exception as error:
        print(f"Dependency map unavailable; using full suite: {type(error).__name__}", file=sys.stderr)
    for p in sorted((DATA / "commits").glob("*.patch")):
        selected = all_numbers
        if selector is not None:
            try:
                selected = frozenset(selector.select(p.read_text(encoding="utf-8")))
            except Exception as error:
                print(f"{p.stem}: full-suite fallback: {type(error).__name__}", file=sys.stderr)
            if os.environ.get("BR_DEBUG"):
                print(f"{p.stem}: {len(selected)} tests; {selector.last_reason}", file=sys.stderr)
        # Measurement only (branch diag-3): drop every heavy test (>= 3 s recorded) from each selection.
        selected = frozenset(n for n in selected if item_by_number[n][1] < 3000)
        if selected not in cache:
            cache[selected] = pack([item_by_number[n] for n in sorted(selected)])
        print(json.dumps({"commit": p.stem, "workers": cache[selected]}, separators=(",", ":")))


if __name__ == "__main__":
    main()

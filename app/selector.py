import json, sys, os
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from packer import pack

DATA = Path(os.environ.get("BR_DATA", "/data"))


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    items = [(t["number"], round(t["seconds"] * 1000)) for t in tests]
    workers = pack(items)
    for p in sorted((DATA / "commits").glob("*.patch")):
        print(json.dumps({"commit": p.stem, "workers": workers}))


if __name__ == "__main__":
    main()

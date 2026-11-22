"""Intern repeated baseline reader sets in an ordinary JSON artifact."""
import argparse
import json
from pathlib import Path


def intern(values):
    groups = []
    index = {}
    result = {}
    for name, entries in values.items():
        key = json.dumps(entries, separators=(",", ":"))
        if key not in index:
            index[key] = len(groups)
            groups.append(entries)
        result[name] = index[key]
    return result, groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.input.read_text(encoding="utf-8"))
    data.pop("used", None)
    data["fields"], data["groups"] = intern(data["fields"])
    data["imports"], data["reader_groups"] = intern(data["imports"])
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    temporary.replace(args.output)
    print(f"{args.output.stat().st_size} bytes; {len(data['groups'])} test sets; {len(data['reader_groups'])} import reader sets")


if __name__ == "__main__":
    main()

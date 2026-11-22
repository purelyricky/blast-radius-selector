"""Intern repeated baseline reader sets in an ordinary JSON artifact."""
import argparse
import gzip
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
    opener = gzip.open if args.input.suffix == '.gz' else open
    with opener(args.input, 'rt', encoding='utf-8') as handle:
        data = json.load(handle)
    data.pop("used", None)
    data["fields"], data["groups"] = intern(data["fields"])
    data["imports"], data["reader_groups"] = intern(data["imports"])
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    opener = gzip.open if args.output.suffix == '.gz' else open
    with opener(temporary, 'wt', encoding='utf-8') as handle:
        json.dump(data, handle, separators=(',', ':'))
    temporary.replace(args.output)
    print(f"{args.output.stat().st_size} bytes; {len(data['groups'])} test sets; {len(data['reader_groups'])} import reader sets")


if __name__ == "__main__":
    main()

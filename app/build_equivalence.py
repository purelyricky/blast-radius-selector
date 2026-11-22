"""Build-time probe: which is/== sites only ever see default-equality operands.

    python app/build_equivalence.py --source vendor/sqlglot --output artifacts/eq_sites.json
Runs the unmodified-behaviour baseline suite on an instrumented copy of the
pinned sqlglot (the checkout itself is never modified).
"""
import argparse, builtins, concurrent.futures, json, os, shutil, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from equivalence import Probe, instrument_source
from build_map import InstrumentedExecutor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--workdir", type=Path)
    args = ap.parse_args()
    started = time.monotonic()
    work = args.workdir or Path(tempfile.mkdtemp())
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(args.source, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    total = 0
    for p in sorted((work / "sqlglot").rglob("*.py")):
        rel = p.relative_to(work).as_posix()
        tree, n = instrument_source(rel, p.read_text(encoding="utf-8"))
        total += n
        import ast
        p.write_text(ast.unparse(tree), encoding="utf-8")
    probe = Probe()
    builtins.__eqprobe__ = probe
    concurrent.futures.ProcessPoolExecutor = InstrumentedExecutor
    sys.path.insert(0, str(work))
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.chdir(work)
    import pytest
    code = pytest.main(["tests", "-q", "--tb=short", "-p", "no:cacheprovider"])
    if int(code) not in (0, 1):
        raise SystemExit(f"baseline suite failed under instrumentation: exit {int(code)}")
    out = {"revision": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa", "sites": total, "observed": len(probe.seen), "proven": probe.proven()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"sites={total} observed={len(probe.seen)} proven={len(out['proven'])} {time.monotonic()-started:.0f}s", file=sys.stderr)


if __name__ == "__main__":
    main()

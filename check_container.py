"""Independently check an image under the published runtime restrictions."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from score import evaluate_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("data", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts/container-review.jsonl"))
    args = parser.parse_args()
    code = """import json, resource, runpy, sys, time
started = time.process_time()
runpy.run_path('/app/selector.py', run_name='__main__')
print('RESOURCES ' + json.dumps({'cpu': time.process_time()-started, 'rss_kb': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}), file=sys.stderr)
"""
    command = ["docker", "run", "--rm", "--network", "none", "--cpus", "2", "--memory", "4g", "--pids-limit", "256", "--read-only", "--user", "nobody", "--env", "HOME=/tmp", "--tmpfs", "/tmp:rw,size=64m", "--mount", f"type=bind,source={args.data.resolve().as_posix()},target=/data,readonly", "--entrypoint", "python", args.image, "-c", code]
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, timeout=180)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(result.stdout)
    print(f"Exit {result.returncode}; stdout {len(result.stdout)} bytes; wall {time.monotonic()-started:.3f}s")
    print(result.stderr.decode("utf-8", errors="replace"), end="")
    if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
        return 1
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    answers = json.loads((args.data / "answers.json").read_text())["commits"]
    evaluation = evaluate_run(result.stdout, tests, answers)
    print(json.dumps(evaluation, sort_keys=True))
    return 0 if evaluation["ok"] and not evaluation["missed_commits"] else 1


if __name__ == "__main__":
    sys.exit(main())

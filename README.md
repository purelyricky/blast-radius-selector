# Blast Radius selector

MIT-licensed regression test selection and worker packing for the Builders Buildathon RWX track.

The selector uses dependency metadata generated solely from unmodified sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. It never reads sample answers, stores patch-to-answer tables, or imports sqlglot while deciding what to run. Runtime test numbers are resolved from pytest node IDs, so numbering may change.

## Method

1. Observe per-test source coverage and fixture reads on the pinned baseline. Class setup dependencies extend to every test in the class. Process-pool computations use synchronous instrumentation and pickle round trips to preserve copy boundaries and attribute their dependencies to the submitting test.
2. Strictly apply each patch to pinned source and compile it without execution. Malformed patches, unknown dependencies, syntax/import hazards and unsupported structural changes use a conservative fallback.
3. Compare Python ASTs. Body changes select baseline consumers of the changed statement region; insertions and changed control flow include the enclosing entry region. Deferred lambda bodies and proven pure closure factories are attributed to their callable consumers. Import-time callables retain conservative handling.

Shared class values use a separate baseline reader map, including concrete classes, inherited fields, metaclass consumers and reflective attribute reads. Eager readers must have a supported unchanged flow before a class-value change is narrowed. Package initializer body edits keep the same top-level structure and headers. Generator-kind changes, changed callable defaults and unsupported eager flows retain broader selection.

Predicate replay supports edits confined to single-operator comparisons with unchanged dynamic operand expressions. Baseline numeric bounds, string fingerprints and primitive operand relation bits identify tests whose predicate outcomes can change. Numeric proofs cover the complete recorded interval; string fingerprints prove absence only. Two dynamic primitive operands support operator-only edits when all observed less/equal/greater relations have unchanged outcomes. Unknown operands and changed import-time outcomes retain conservative selection. File readers and memory-sensitive contexts remain protected. The profiles contain baseline observations, not mutations or failing-test answers.
3b. Comparison equivalence: `build_equivalence.py` runs the baseline suite on an instrumented copy of the pinned sqlglot and records the operand types at every single-operator `is`/`==`/`!=` site. A site whose operands always use identity equality (default object equality, `None`, or members of one non-Flag Enum) is "proven"; swapping `is`/`==` there is normalized away before diffing (`equivalence.py`, `eq_sites.json`).
3c. Dialect-family tier: when an edit cannot be refined but only swaps same-type constant leaves (for example `-1` to `-2`) inside a dialect's own modules, select the tests that executed that dialect family's code in the baseline (plus inheriting dialects and the dialect's test file) instead of the full suite. Idea of file-level dynamic dependencies follows Ekstazi (Gligoric et al., ISSTA 2015); name-based refinement follows NameRTS (Wang, Pradel, Liu, ISSTA 2026, arXiv:2605.25356), read for ideas only, no code copied.
4. Pack selected tests with longest-first scheduling for each worker count from one through eight, minimizing the busiest worker plus two seconds per worker. Reuse identical selections across the batch.

Maps are ordinary JSON artifacts, with larger maps stored using gzip, committed in the repository. Docker builds need no network downloads beyond the pinned Python base image. The production process reads `/data/tests.json` and `/data/commits/*.patch`, emits only the required JSON lines on stdout, and needs no writable project files.

## Build and validate

```sh
docker build -t blast-radius-selector .
python check_container.py blast-radius-selector /path/to/sample
python app/test_dependencies.py
python app/test_elements.py
python app/test_shared.py
python app/test_package_changes.py
python app/test_deferred.py
python app/test_compare_equivalence.py
python app/test_predicate_values.py
```

`check_container.py` runs the image without network access, as nobody, with a read-only filesystem, two CPUs, 4 GB memory, 256 processes and a 64 MB temporary directory. It checks stdout with the official sample scorer and reports simulated time, process CPU and peak memory.

For native validation, set `BR_DATA` to the sample directory. `BR_MAP` can select a dependency map; `BR_DEBUG=1` explains selections on stderr.

## Reproduce the metadata

Clone the public sqlglot repository under `vendor/sqlglot`, detach at the pinned commit above, and build `Dockerfile.map`. This uses the scorer's exact Python/SQLite image and pinned public PyPI dependencies. Mount the checkout at `/source`, this repository's `app` directory at `/app`, and an output directory at `/output`.

Run these commands inside that image, then copy the resulting maps into `app/`:

```sh
python /app/build_imports.py --source /source --output /output/import-calls.json
python /app/build_map.py --source /source --output /output/dependency_map.json.gz --coverage /output/.coverage --import-calls /output/import-calls.json
python /app/build_import_execution.py --source /source --output /output/import_execution.json
python /app/build_shared.py --source /source --output /output/shared-raw.json.gz
python /app/compact_shared.py /output/shared-raw.json.gz /output/shared_reads.json.gz
python /app/build_predicate_values.py --source /source --output /output/predicate_values.json.gz
```

The baseline suite must collect completely and pass. Source snapshots, coverage, import effects and fixture dependencies are metadata inputs; benchmark patches and failing-test labels are not inputs.

## Third-party code and libraries

- [SQLGlot](https://github.com/tobymao/sqlglot), pinned revision above, MIT. Its source is embedded in the map to validate incoming diffs. Its license is included in `THIRD_PARTY_LICENSES/sqlglot.txt`.
- Python's standard library and the official pinned `python:3.13-slim` Docker image.
- The official Builders Blast Radius starter kit's `score.py`, used for local validation.
- Metadata generation uses coverage.py, pytest, duckdb, pandas, NumPy, python-dateutil, pytz, typing_extensions, setuptools and setuptools-scm, with versions in `Dockerfile.map` and their transitive public PyPI dependencies. These packages are not installed in the production image.

The implementation and dependency metadata are original team work derived from the pinned upstream revision. No other team's code is used.

# DECISIONS - RWX Blast Radius - test selection + sharding for sqlglot

Curated current truth (short). History lives in `log.txt`. Add: decision, why, date/time, who.

- 2026-11-22 11:49 codex: Build pinned-source per-test coverage and fixture-read maps, with strict patch application and conservative syntax/import/unknown fallbacks. Static imports alone select almost everything. Store pytest node IDs so runtime test numbering can change. No sample answers enter map generation.
- 2026-11-22 12:13 codex: Refine deferred callable bodies and shared-value consumers only with pinned-source AST/bytecode and baseline observations. Preserve import/syntax/unknown fallback. No patch IDs or edit-kind answer tables enter selection.
- 2026-11-22 12:47 lead: Retain a dialect-family fallback for same-type literal leaf changes, including static import descendants. This is an explicit expected-cost tradeoff with residual eager-consumer risk; prefer proven shared-reader handling when available.
- 2026-11-22 12:53 both: Shared-reader build 25e58da approved after 35 safety checks and restricted Docker validation (115.094 simulated sample seconds, zero misses). Ordinary JSON maps plus packed Git history remain below 20 MB. No patch answers enter any metadata builder.
- 2026-11-22 13:56 codex: Add optional baseline predicate replay for comparison-only edits. Prove unchanged numeric outcomes across full observed intervals; string fingerprints prove absence only. Unknown operands and import changes retain broader selection. Full baseline passed at 14:07; independent synthetic mutation validation and lead review are required before submission.
- 2026-11-22 14:07 codex: Store large JSON maps with gzip to reduce repository size; loaders validate revision and node-ID alignment. Compression is lossless and does not alter selection.

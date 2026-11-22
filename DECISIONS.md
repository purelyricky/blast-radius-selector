# DECISIONS - RWX Blast Radius - test selection + sharding for sqlglot

Curated current truth (short). History lives in `log.txt`. Add: decision, why, date/time, who.

- 2026-11-22 11:49 codex: Build pinned-source per-test coverage and fixture-read maps, with strict patch application and conservative syntax/import/unknown fallbacks. Static imports alone select almost everything. Store pytest node IDs so runtime test numbering can change. No sample answers enter map generation.

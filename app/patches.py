"""Strict, dependency-free parser for unified source patches.

Reject rather than guess when a diff cannot be applied to the pinned source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re


class InvalidPatch(ValueError):
    pass


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    old: str
    new: str
    hunks: list[Hunk] = field(default_factory=list)
    old_no_newline: bool = False
    new_no_newline: bool = False


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


def _path(header, prefix):
    value = header[4:].split("\t", 1)[0]
    if value.startswith('"'):
        try:
            value = json.loads(value)
        except (ValueError, TypeError) as error:
            raise InvalidPatch("quoted path") from error
    if value == "/dev/null":
        return value
    if value.startswith(prefix):
        value = value[len(prefix):]
    if not value or value.startswith("/") or "\\" in value or any(part in ("", ".", "..") for part in value.split("/")):
        raise InvalidPatch("unsafe path")
    return value


def parse_patch(text):
    result = []
    current = None
    hunk = None
    old_header = None
    metadata = False
    old_seen = new_seen = 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = None
            hunk = None
            old_header = None
            metadata = True
        elif line.startswith("@@ "):
            match = HUNK.fullmatch(line)
            if current is None or match is None:
                raise InvalidPatch("hunk without file")
            old_start, old_count, new_start, new_count = match.groups()
            hunk = Hunk(int(old_start), int(old_count or 1), int(new_start), int(new_count or 1))
            current.hunks.append(hunk)
            old_seen = new_seen = 0
        elif hunk is not None and line and line[0] in " +-":
            # A header-looking removed or added line is still hunk content.
            if old_seen < hunk.old_count or new_seen < hunk.new_count:
                hunk.lines.append(line)
                old_seen += line[0] != "+"
                new_seen += line[0] != "-"
            elif line.startswith("--- "):
                old_header = _path(line, "a/")
                hunk = None
            else:
                raise InvalidPatch("extra hunk content")
        elif line.startswith("--- "):
            old_header = _path(line, "a/")
            hunk = None
        elif line.startswith("+++ "):
            if old_header is None:
                raise InvalidPatch("new path without old path")
            current = FilePatch(old_header, _path(line, "b/"))
            result.append(current)
            old_header = None
            hunk = None
        elif line == "\\ No newline at end of file":
            # Trailing newline does not change dependency scopes. Source AST
            # checks below still verify the resulting Python code.
            if hunk is None:
                raise InvalidPatch("newline marker without hunk")
            if not hunk.lines:
                raise InvalidPatch("newline marker without content")
            previous = hunk.lines[-1][0]
            current.old_no_newline |= previous in " -"
            current.new_no_newline |= previous in " +"
        elif line.startswith(("index ", "old mode ", "new mode ", "new file mode ", "deleted file mode ", "similarity index ", "rename from ", "rename to ")):
            if not metadata:
                raise InvalidPatch("metadata without git header")
        elif line == "":
            if hunk is not None:
                raise InvalidPatch("unprefixed blank hunk line")
        else:
            raise InvalidPatch("unsupported diff content")
    if not result or any(not file.hunks for file in result):
        raise InvalidPatch("no textual changes")
    for file in result:
        for hunk in file.hunks:
            if sum(s[0] != "+" for s in hunk.lines) != hunk.old_count or sum(s[0] != "-" for s in hunk.lines) != hunk.new_count:
                raise InvalidPatch("hunk line counts")
    return result


def apply_patch(source, patch):
    old_lines = source.splitlines()
    new_lines = []
    cursor = 0
    for hunk in patch.hunks:
        start = hunk.old_start - (1 if hunk.old_count else 0)
        if start < cursor or start > len(old_lines):
            raise InvalidPatch("hunk position")
        new_lines.extend(old_lines[cursor:start])
        if len(new_lines) != hunk.new_start - (1 if hunk.new_count else 0):
            raise InvalidPatch("new hunk position")
        cursor = start
        for line in hunk.lines:
            if line[0] != "+":
                if cursor >= len(old_lines) or old_lines[cursor] != line[1:]:
                    raise InvalidPatch("source context mismatch")
                cursor += 1
            if line[0] != "-":
                new_lines.append(line[1:])
    new_lines.extend(old_lines[cursor:])
    trailing_newline = not patch.new_no_newline and (source.endswith("\n") or patch.old_no_newline)
    return "\n".join(new_lines) + ("\n" if new_lines and trailing_newline else "")

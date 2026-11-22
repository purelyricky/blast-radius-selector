"""Whole-definition additions/removals found by name reachability.

When a patch only adds or removes whole statements that bind names (functions,
module/class attributes, imports), a test can observe the change only by
looking those names up. Static identifier occurrences in the pinned sources
(NameRTS-style name propagation) bound the consumers: tests that executed a
line mentioning the name, plus tests that executed a removed definition's own
lines. Dispatch-by-name conventions and class additions keep the full-suite
fallback (see DISPATCH).
"""
import ast
import copy
import difflib
import re

DISPATCH = re.compile(r"(_sql$|^_?parse_|^_?build_|_builder$|^visit_|^__|^test)")
_DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign, ast.Import, ast.ImportFrom)


def _dump(node):
    return ast.dump(node, include_attributes=False)


def bound_names(statement):
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {statement.name}
    if isinstance(statement, ast.Assign):
        names = set()
        for target in statement.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            else:
                raise ValueError("complex assignment target")
        return names
    if isinstance(statement, ast.AnnAssign):
        if not isinstance(statement.target, ast.Name):
            raise ValueError("complex assignment target")
        return {statement.target.id}
    names = set()
    for alias in statement.names:
        if alias.name == "*":
            raise ValueError("star import")
        names.add(alias.asname or alias.name.split(".")[0])
    return names


def membership_changes(before, after):
    """Return (names, removed_nodes) if only whole definitions were added/removed, else None."""
    names, removed = set(), []

    def walk(old_body, new_body):
        matcher = difflib.SequenceMatcher(a=[_dump(s) for s in old_body], b=[_dump(s) for s in new_body], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            olds, news = old_body[i1:i2], new_body[j1:j2]
            if tag == "replace" and len(olds) == len(news) == 1 and isinstance(olds[0], ast.ClassDef) and isinstance(news[0], ast.ClassDef):
                a, b = olds[0], news[0]
                ha, hb = copy.copy(a), copy.copy(b)
                ha.body, hb.body = [], []
                if _dump(ha) != _dump(hb):
                    raise ValueError("class header changed")
                walk(a.body, b.body)
                continue
            for statement in olds + news:
                if not isinstance(statement, _DEFINITION):
                    raise ValueError("non-definition statement changed")
                bound = bound_names(statement)
                if any(DISPATCH.search(name) for name in bound):
                    raise ValueError("dispatch-by-name convention")
                names.update(bound)
            removed.extend(olds)

    try:
        walk(before.body, after.body)
    except ValueError:
        return None
    return (names, removed) if names else None


def name_regex(names):
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")

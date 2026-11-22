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


def _dump(node):
    return ast.dump(node, include_attributes=False)


def _plain_function(node, future_annotations):
    """A def whose creation cannot raise and whose name is its only effect."""
    if node.decorator_list or node.args.defaults or any(default is not None for default in node.args.kw_defaults):
        return False
    if not future_annotations:
        annotations = [arg.annotation for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs + [a for a in (node.args.vararg, node.args.kwarg) if a is not None]]
        if any(a is not None for a in annotations) or node.returns is not None:
            return False
    return True


def _literal(node):
    try:
        ast.literal_eval(node)
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def bound_names(statement, in_class, future_annotations):
    """Names bound by a statement that is safe to add or remove; raises ValueError otherwise."""
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if not _plain_function(statement, future_annotations):
            raise ValueError("eager function header")
        return {statement.name}
    if isinstance(statement, ast.Assign) and not in_class and all(isinstance(t, ast.Name) for t in statement.targets) and _literal(statement.value):
        return {t.id for t in statement.targets}
    raise ValueError("definition kind not provable")


def membership_changes(before, after):
    """Return (names, removed_nodes) if only whole definitions were added/removed, else None."""
    names, removed = set(), []
    future_annotations = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in before.body)

    def walk(old_body, new_body, in_class=False):
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
                walk(a.body, b.body, True)
                continue
            for statement in olds + news:
                bound = bound_names(statement, in_class, future_annotations)
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

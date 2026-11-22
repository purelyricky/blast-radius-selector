"""Check comparison-only equivalence without copying or rewriting ASTs."""
import ast

from equivalence import site_keys


def equivalent_edits(before, after, path, proven, keys=None):
    """True only if all semantic AST differences are proven operator swaps.

    Every operand, binding, statement and other literal must match. This also
    guarantees that the baseline comparison ordinals cannot shift.
    """
    if not proven:
        return False
    keys = site_keys(path, before) if keys is None else keys
    changed = False
    pending = [(before, after)]
    while pending:
        old, new = pending.pop()
        if old is new:
            continue
        if type(old) is not type(new):
            return False
        if isinstance(old, ast.AST):
            skip_operators = False
            if isinstance(old, ast.Compare) and len(old.ops) == len(new.ops) == 1 and type(old.ops[0]) is not type(new.ops[0]):
                pair = frozenset((type(old.ops[0]), type(new.ops[0])))
                if pair not in (frozenset((ast.Is, ast.Eq)), frozenset((ast.IsNot, ast.NotEq))) or keys.get(id(old)) not in proven:
                    return False
                skip_operators = True
                changed = True
            for field in old._fields:
                if not (skip_operators and field == "ops"):
                    pending.append((getattr(old, field, None), getattr(new, field, None)))
        elif isinstance(old, list):
            if len(old) != len(new):
                return False
            pending.extend(zip(old, new))
        elif old != new:
            return False
    return changed

"""Measurement only (branch diag-4): coarse class of the minimal AST differences of a patch."""
import ast


def labels(old, new, out):
    if type(old) is not type(new):
        out.add("other")
        return
    if isinstance(old, ast.AST):
        if ast.dump(old) == ast.dump(new):
            return
        if isinstance(old, ast.Compare):
            same_shape = len(old.ops) == len(new.ops) and len(old.comparators) == len(new.comparators)
            if same_shape and ast.dump(old.left) == ast.dump(new.left) and all(ast.dump(a) == ast.dump(b) for a, b in zip(old.comparators, new.comparators)):
                out.add("cmp")  # operator-only change
                return
            if same_shape:
                for f in ("left",):
                    labels(getattr(old, f), getattr(new, f), out)
                for a, b in zip(old.comparators, new.comparators):
                    labels(a, b, out)
                if [type(o) for o in old.ops] != [type(o) for o in new.ops]:
                    out.add("cmp")
                return
            out.add("other")
            return
        if isinstance(old, ast.Constant):
            out.add("const")
            return
        if isinstance(old, ast.BoolOp) and type(old.op) is not type(new.op):
            out.add("bool")
            return
        if isinstance(old, ast.BinOp) and type(old.op) is not type(new.op):
            out.add("arith")
            return
        for f in old._fields:
            a, b = getattr(old, f, None), getattr(new, f, None)
            if isinstance(a, list) and isinstance(b, list):
                if len(a) != len(b):
                    out.add("other")
                    continue
                for x, y in zip(a, b):
                    labels(x, y, out)
            elif isinstance(a, ast.AST) and isinstance(b, ast.AST):
                labels(a, b, out)
            elif a != b:
                out.add("other")
    elif old != new:
        out.add("other")


def classify(selector, patch_text):
    from patches import parse_patch, apply_patch
    out = set()
    for patch in parse_patch(patch_text):
        source = selector.map["sources"].get(patch.old)
        if source is None or patch.new != patch.old or not patch.old.endswith(".py"):
            out.add("other")
            continue
        labels(ast.parse(source), ast.parse(apply_patch(source, patch)), out)
    return out

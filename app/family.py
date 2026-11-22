"""Shape-preserving edit check for the dialect-family tier.

An edit qualifies when old and new ASTs have identical structure and differ
only in leaves that cannot change how the program is built:
  * a constant replaced by a non-degenerate constant of the same type;
  * a name or attribute replaced by another identifier that is defined
    somewhere in the pinned sources (so the swap cannot raise NameError or
    AttributeError for a name that does not exist at all).
Added, removed or reordered nodes never qualify.
"""
import ast
import re

_DEFINED = re.compile(r"^\s*(?:class|def)\s+(\w+)|^\s*(\w+)\s*(?::[^=\n]+)?=[^=]", re.M)


_EXPORTED = re.compile(r"^(?:class|def)\s+(\w+)|^(\w+)\s*(?::[^=\n]+)?=[^=]", re.M)


def known_names(sources):
    """{'all': every defined identifier, 'exp': identifiers defined in sqlglot/expressions/}."""
    names, exp = set(), set()
    for path, text in sources.items():
        for match in _DEFINED.finditer(text):
            name = match.group(1) or match.group(2)
            names.add(name)
        if path.startswith("sqlglot/expressions/"):
            for match in _EXPORTED.finditer(text):
                exp.add(match.group(1) or match.group(2))
    return {"all": names, "exp": exp}


def _leaf_ok(old, new, known, in_function):
    if isinstance(old, ast.Constant):
        a, b = old.value, new.value
        return type(a) is type(b) and not (isinstance(a, (str, bytes)) and (not a or not b))
    if isinstance(old, ast.Name):
        # A bare name swap outside a function body runs at import time, where
        # an undefined name would break importing; only allow it inside functions.
        return in_function and type(old.ctx) is type(new.ctx) and isinstance(old.ctx, ast.Load) and new.id in known["all"]
    if isinstance(old, ast.Attribute):
        if not (type(old.ctx) is type(new.ctx) and isinstance(old.ctx, ast.Load) and dump(old.value) == dump(new.value)):
            return False
        # Only exp.<Name> swaps are provable from top-level definitions of the expression modules.
        return isinstance(old.value, ast.Name) and old.value.id == "exp" and new.attr in known["exp"]
    return False


def dump(node):
    return ast.dump(node, include_attributes=False)


def change_ok(old, new, known, in_function=False):
    if isinstance(old, ast.AST) and isinstance(new, ast.AST):
        if type(old) is not type(new):
            return False
        if dump(old) == dump(new):
            return True
        if isinstance(old, (ast.Constant, ast.Name, ast.Attribute)):
            return _leaf_ok(old, new, known, in_function)
        callable_node = isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        # Defaults, decorators and annotations run at definition time; only bodies run later.
        return all(
            change_ok(getattr(old, f, None), getattr(new, f, None), known, in_function or (callable_node and f == "body"))
            for f in old._fields
        )
    if isinstance(old, list) and isinstance(new, list):
        return len(old) == len(new) and all(change_ok(a, b, known, in_function) for a, b in zip(old, new))
    return old == new


def strip_docstrings(tree):
    import copy

    tree = copy.deepcopy(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def docstring_only(before, after):
    """True when the trees differ only in docstring text (never read at runtime by sqlglot)."""
    return dump(strip_docstrings(before)) == dump(strip_docstrings(after))


def header_default_changes(before, after):
    """Functions whose only difference is a same-type literal default value.

    Returns the list of old FunctionDef nodes, or None when anything else
    differs (statement added/removed/reordered, other header parts, bodies).
    """
    changed = []

    def literal_defaults_ok(old_args, new_args):
        pairs = list(zip(old_args.defaults, new_args.defaults)) + list(zip(old_args.kw_defaults, new_args.kw_defaults))
        if len(old_args.defaults) != len(new_args.defaults) or len(old_args.kw_defaults) != len(new_args.kw_defaults):
            return False
        for a, b in pairs:
            if a is None or b is None:
                if a is not b:
                    return False
                continue
            if dump(a) != dump(b):
                if not (isinstance(a, ast.Constant) and isinstance(b, ast.Constant)):
                    return False
                if type(a.value) is not type(b.value) or (isinstance(a.value, (str, bytes)) and (not a.value or not b.value)):
                    return False
        return True

    def bodies(old_body, new_body):
        if len(old_body) != len(new_body):
            return False
        return all(statement(o, n) for o, n in zip(old_body, new_body))

    def statement(old, new):
        if dump(old) == dump(new):
            return True
        if type(old) is not type(new):
            return False
        if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not literal_defaults_ok(old.args, new.args):
                return False
            import copy

            a, b = copy.copy(old), copy.copy(new)
            a.args, b.args = copy.copy(old.args), copy.copy(new.args)
            a.args.defaults = b.args.defaults = []
            a.args.kw_defaults = b.args.kw_defaults = []
            a.body, b.body = [], []
            if dump(a) != dump(b):
                return False
            if dump(ast.Module(body=old.body, type_ignores=[])) != dump(ast.Module(body=new.body, type_ignores=[])):
                if not bodies(old.body, new.body):
                    return False
            else:
                pass
            changed.append(old)
            return True
        if isinstance(old, ast.ClassDef):
            import copy

            a, b = copy.copy(old), copy.copy(new)
            a.body, b.body = [], []
            return dump(a) == dump(b) and bodies(old.body, new.body)
        return False

    return changed if bodies(before.body, after.body) and changed else None

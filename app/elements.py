"""Source-derived refinement of deferred callable elements.

Changing a lambda body does not evaluate that body at definition time. A
factory consisting solely of function definitions and a return also defers
its captured arguments. Attribute these changes to the returned callable's
baseline consumers, while retaining full fallback for other eager effects.
"""
import ast
import copy
from functools import lru_cache
from package_changes import generator_kind


class UnsupportedElement(ValueError):
    pass


@lru_cache(maxsize=4096)
def dump(node):
    return ast.dump(node, include_attributes=False)


def module_name(path):
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


class ElementRefiner:
    def __init__(self, metadata):
        self.metadata = metadata
        self.sources = metadata["sources"]
        self.modules = {module_name(path): path for path in self.sources}
        self.trees = {}
        self.factory_cache = {}
        imported = metadata.get("import_calls")
        self.imported = {tuple(item) for item in imported.get("calls", [])} if imported is not None and not imported.get("failed") else None

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = ast.parse(self.sources[path])
        return self.trees[path]

    def imported_callable(self, path, node):
        name = "<lambda>" if isinstance(node, ast.Lambda) else node.name
        return self.imported is None or (path, node.lineno, name) in self.imported

    def resolve_factory(self, path, function, seen=()):
        if not isinstance(function, ast.Name) or (path, function.id) in seen:
            raise UnsupportedElement("unresolved factory")
        key = (path, function.id)
        if key in self.factory_cache:
            return self.factory_cache[key]
        matches = []
        for node in self.tree(path).body:
            if isinstance(node, ast.FunctionDef) and node.name == function.id:
                matches.append((path, node))
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    if (alias.asname or alias.name) == function.id:
                        target = self.modules.get(node.module)
                        if target:
                            matches.append(self.resolve_factory(target, ast.Name(id=alias.name), seen + (key,)))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == function.id for target in targets):
                    raise UnsupportedElement("factory name rebound")
        if len(matches) != 1:
            raise UnsupportedElement("ambiguous factory")
        result = matches[0]
        self.factory_cache[key] = result
        return result

    def returned_callables(self, path, function):
        if function.decorator_list:
            raise UnsupportedElement("decorated factory")
        body = function.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if not body or not isinstance(body[-1], ast.Return):
            raise UnsupportedElement("eager factory statements")
        definitions = {node.name: node for node in body[:-1] if isinstance(node, ast.FunctionDef)}
        if len(definitions) != len(body) - 1:
            raise UnsupportedElement("eager factory statements")
        returned = body[-1].value
        if isinstance(returned, ast.Name) and returned.id in definitions:
            result = [definitions[returned.id]]
        elif isinstance(returned, ast.Lambda):
            result = [returned]
        else:
            raise UnsupportedElement("factory does not return a local callable")
        future_annotations = any(isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names) for node in self.tree(path).body)
        for node in list(definitions.values()) + result:
            if getattr(node, "decorator_list", []) or node.args.defaults or any(default is not None for default in node.args.kw_defaults):
                raise UnsupportedElement("eager nested callable header")
            if not future_annotations:
                annotations = [arg.annotation for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
                annotations += [getattr(node, "returns", None)]
                if any(annotation is not None for annotation in annotations):
                    raise UnsupportedElement("eager nested annotations")
            if any(isinstance(child, (ast.Global, ast.Nonlocal)) for child in ast.walk(node)):
                raise UnsupportedElement("shared lexical state")
        if any(self.imported_callable(path, node) for node in result):
            raise UnsupportedElement("returned callable executes during import")
        return result

    def _deferred_call(self, path, old, new, blocked_names):
        if isinstance(old.func, ast.Name) and old.func.id in blocked_names:
            raise UnsupportedElement("class-local factory binding")
        if dump(old.func) != dump(new.func) or len(old.args) != len(new.args) or [kw.arg for kw in old.keywords] != [kw.arg for kw in new.keywords]:
            raise UnsupportedElement("factory binding changed")
        for before, after in list(zip(old.args, new.args)) + [(a.value, b.value) for a, b in zip(old.keywords, new.keywords)]:
            if dump(before) != dump(after):
                try:
                    ast.literal_eval(before)
                    ast.literal_eval(after)
                except (ValueError, TypeError, SyntaxError) as error:
                    raise UnsupportedElement("eager factory argument") from error
        target_path, factory = self.resolve_factory(path, old.func)
        return [(target_path, node) for node in self.returned_callables(target_path, factory)]

    def _deferred_statement(self, path, before, after, blocked_names=(), allow_factories=True):
        regions = []
        def compare(old, new):
            if isinstance(old, ast.AST) and isinstance(new, ast.AST):
                if dump(old) == dump(new):
                    return
                if type(old) is not type(new):
                    raise UnsupportedElement("changed eager statement shape")
                if isinstance(old, ast.Lambda):
                    if dump(old.args) != dump(new.args) or self.imported_callable(path, old) or generator_kind(old) != generator_kind(new):
                        raise UnsupportedElement("lambda header or import effects")
                    regions.append((path, old))
                    return
                if isinstance(old, ast.Call) and allow_factories:
                    regions.extend(self._deferred_call(path, old, new, blocked_names))
                    return
                for field in old._fields:
                    compare(getattr(old, field, None), getattr(new, field, None))
            elif isinstance(old, list) and isinstance(new, list):
                if len(old) != len(new):
                    raise UnsupportedElement("eager list shape changed")
                for a, b in zip(old, new):
                    compare(a, b)
            elif type(old) is not type(new) or old != new:
                raise UnsupportedElement("changed eager value")
        compare(before, after)
        if not regions:
            raise UnsupportedElement("not a deferred body edit")
        return regions

    def normalize(self, path, before, after):
        """Return AST with recognized deferred edits reverted, plus consumers."""
        regions = []
        def compare(old_body, new_body, in_class=False, in_function=False):
            if len(old_body) != len(new_body):
                return new_body
            replacements = {}
            bound = set()
            for index, (old, new) in enumerate(zip(old_body, new_body)):
                blocked = set(bound) if in_class else set()
                if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(old.name)
                elif isinstance(old, (ast.Assign, ast.AnnAssign)):
                    targets = old.targets if isinstance(old, ast.Assign) else [old.target]
                    bound.update(target.id for target in targets if isinstance(target, ast.Name))
                elif isinstance(old, (ast.Import, ast.ImportFrom)):
                    bound.update(alias.asname or alias.name.split(".")[0] for alias in old.names)
                if dump(old) == dump(new):
                    continue
                if isinstance(old, ast.ClassDef) and isinstance(new, ast.ClassDef):
                    body = compare(old.body, new.body, in_class=True, in_function=in_function)
                    if body is not new.body:
                        replacement = copy.copy(new)
                        replacement.body = body
                        replacements[index] = replacement
                elif isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)) and type(old) is type(new):
                    body = compare(old.body, new.body, in_function=True)
                    if body is not new.body:
                        replacement = copy.copy(new)
                        replacement.body = body
                        replacements[index] = replacement
                else:
                    try:
                        found = self._deferred_statement(path, old, new, blocked, allow_factories=not in_function)
                    except UnsupportedElement:
                        continue
                    replacements[index] = old
                    regions.extend(found)
            return [replacements.get(index, node) for index, node in enumerate(new_body)] if replacements else new_body
        body = compare(before.body, after.body)
        normalized = copy.copy(after) if body is not after.body else after
        if body is not after.body:
            normalized.body = body
        return normalized, regions

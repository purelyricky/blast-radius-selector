"""Refine literal class values with observed readers and checked eager flows."""
import ast
import copy
from functools import lru_cache


class UnsafeValue(ValueError):
    pass


@lru_cache(maxsize=4096)
def dump(node):
    return ast.dump(node, include_attributes=False)


def shape(value):
    if isinstance(value, dict):
        return (dict, tuple((type(key), repr(key), shape(child)) for key, child in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value), tuple(shape(child) for child in value))
    if isinstance(value, (set, frozenset)):
        raise UnsafeValue("set membership changes")
    return type(value)


def parents_for(node):
    return {id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}


def field_access(node, name):
    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr == name:
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr") and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == name


class SharedRefiner:
    def __init__(self, sources, metadata):
        if metadata.get("version") != 2:
            raise UnsafeValue("incomplete shared-field metadata")
        self.sources = sources
        self.metadata = metadata
        self.trees = {}
        self.scope_cache = {}
        self.meta_cache = {}

    def readers(self, key):
        value = self.metadata["imports"].get(key, [])
        return self.metadata["reader_groups"][value] if isinstance(value, int) else value

    def indices(self, key):
        value = self.metadata["fields"].get(key, [])
        return self.metadata["groups"][value] if isinstance(value, int) else value

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = ast.parse(self.sources[path])
        return self.trees[path]

    def scope(self, path, firstline, qualname):
        key = (path, firstline, qualname)
        if key not in self.scope_cache:
            tree = self.tree(path)
            if qualname == "<module>":
                result = tree
            else:
                candidates = []
                comprehension_types = {"<genexpr>": ast.GeneratorExp, "<listcomp>": ast.ListComp, "<setcomp>": ast.SetComp, "<dictcomp>": ast.DictComp, "<lambda>": ast.Lambda}
                special = comprehension_types.get(qualname.split(".")[-1])
                for node in ast.walk(tree):
                    if special is not None and isinstance(node, special) and node.lineno == firstline:
                        candidates.append(node)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                        if firstline in (start, node.lineno) and node.name == qualname.split(".")[-1]:
                            candidates.append(node)
                if len(candidates) != 1:
                    raise UnsafeValue("unresolved eager reader")
                result = candidates[0]
            self.scope_cache[key] = result
        return self.scope_cache[key]

    def metaclasses_safe(self, classkey):
        if classkey in self.meta_cache:
            return self.meta_cache[classkey]
        metas = self.metadata.get("metaclasses", {}).get(classkey)
        if not metas:
            raise UnsafeValue("unobserved metaclass")
        module_paths = {path.removesuffix(".py").replace("/", ".").removesuffix(".__init__"): path for path in self.sources}
        for meta in metas:
            if meta in ("builtins::type", "builtins::object"):
                continue
            module, qualname = meta.split("::", 1)
            path = module_paths.get(module)
            if path is None:
                raise UnsafeValue("external metaclass")
            classes = [node for node in ast.walk(self.tree(path)) if isinstance(node, ast.ClassDef) and node.name == qualname.split(".")[-1]]
            if len(classes) != 1:
                raise UnsafeValue("unresolved metaclass")
            meta_node = classes[0]
            for method in meta_node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if method.name in ("__setattr__", "__getattribute__"):
                    raise UnsafeValue("custom metaclass attribute semantics")
                if method.name not in ("__new__", "__init__"):
                    continue
                arguments = method.args.posonlyargs + method.args.args
                if len(arguments) < 4:
                    raise UnsafeValue("opaque metaclass namespace")
                namespace = arguments[3].arg
                parents = parents_for(method)
                for use in ast.walk(method):
                    if not isinstance(use, ast.Name) or not isinstance(use.ctx, ast.Load) or use.id != namespace:
                        continue
                    parent = parents.get(id(use))
                    if not (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute) and parent.func.attr in ("__new__", "__init__") and isinstance(parent.func.value, ast.Call) and isinstance(parent.func.value.func, ast.Name) and parent.func.value.func.id == "super"):
                        raise UnsafeValue("metaclass observes namespace contents")
        self.meta_cache[classkey] = True
        return True

    @staticmethod
    def _keys_only(use, parents):
        parent = parents.get(id(use))
        if isinstance(parent, ast.Compare):
            return any(isinstance(op, (ast.In, ast.NotIn)) and comparator is use for op, comparator in zip(parent.ops, parent.comparators))
        if isinstance(parent, ast.Attribute) and parent.value is use and parent.attr == "keys":
            call = parents.get(id(parent))
            return isinstance(call, ast.Call) and call.func is parent and not call.args and not call.keywords
        if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is use:
            return True
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "len" and parent.args == [use] and not parent.keywords:
            return True
        return isinstance(parent, ast.If) and parent.test is use

    def _derived_boolean_keys(self, access, old_value, new_value, parents):
        if not isinstance(old_value, dict) or not isinstance(new_value, dict) or not all(type(value) is bool for value in list(old_value.values()) + list(new_value.values())):
            raise UnsafeValue("not a boolean-value projection")
        method = parents.get(id(access))
        call = parents.get(id(method))
        generator = parents.get(id(call))
        comprehension = parents.get(id(generator))
        assignment = parents.get(id(comprehension))
        if not (isinstance(access, ast.Attribute) and isinstance(access.value, ast.Name) and isinstance(method, ast.Attribute) and method.attr == "items" and isinstance(call, ast.Call) and not call.args and not call.keywords and isinstance(generator, ast.comprehension) and generator.iter is call and isinstance(comprehension, ast.SetComp) and comprehension.generators == [generator] and isinstance(generator.target, ast.Tuple) and len(generator.target.elts) == 2 and all(isinstance(x, ast.Name) for x in generator.target.elts)):
            raise UnsafeValue("unproved import-time projection")
        key_name, value_name = (node.id for node in generator.target.elts)
        if not isinstance(comprehension.elt, ast.Name) or comprehension.elt.id != key_name or generator.is_async:
            raise UnsafeValue("unsafe projection output")
        for condition in generator.ifs:
            condition = condition.operand if isinstance(condition, ast.UnaryOp) and isinstance(condition.op, ast.Not) else condition
            if not isinstance(condition, ast.Name) or condition.id != value_name:
                raise UnsafeValue("unsafe projection predicate")
        if not (isinstance(assignment, ast.Assign) and assignment.value is comprehension and len(assignment.targets) == 1 and isinstance(assignment.targets[0], ast.Attribute) and isinstance(assignment.targets[0].value, ast.Name) and assignment.targets[0].value.id == access.value.id):
            raise UnsafeValue("projection escapes receiver class")
        target = assignment.targets[0].attr
        if target.startswith("__"):
            raise UnsafeValue("projection changes special class semantics")
        return target

    def eager_read(self, scope, field, old_value, new_value):
        parents = parents_for(scope)
        accesses = [node for node in ast.walk(scope) if field_access(node, field)]
        if not accesses:
            raise UnsafeValue("opaque eager field read")
        derived = set()
        for access in accesses:
            if isinstance(access, ast.Call) and access.func.id == "hasattr":
                continue
            if isinstance(old_value, dict) and self._keys_only(access, parents):
                continue
            parent = parents.get(id(access))
            if isinstance(old_value, dict) and isinstance(parent, ast.Assign) and parent.value is access and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                alias = parent.targets[0].id
                uses = [node for node in ast.walk(scope) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == alias]
                if uses and all(self._keys_only(use, parents) for use in uses):
                    continue
            derived.add(self._derived_boolean_keys(access, old_value, new_value, parents))
        return derived

    def opaque_eager_read(self, scope, field):
        parents = parents_for(scope)
        accesses = [node for node in ast.walk(scope) if isinstance(node, ast.Attribute) and node.attr == "__dict__"]
        if not accesses:
            raise UnsafeValue("opaque class reflection")
        for access in accesses:
            parent = parents.get(id(access))
            if isinstance(parent, ast.Compare) and self._keys_only(access, parents):
                continue
            if isinstance(parent, ast.Subscript) and isinstance(parent.slice, ast.Constant) and isinstance(parent.slice.value, str) and parent.slice.value != field:
                continue
            call = parents.get(id(parent))
            if isinstance(parent, ast.Attribute) and parent.attr == "get" and isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str) and call.args[0].value != field:
                continue
            if self._keys_only(access, parents):
                continue
            raise UnsafeValue("class namespace values escape")

    def prove_imports(self, classkey, field, old_value, new_value):
        derived = set()
        for reader in self.readers(classkey + "::" + field):
            scope = self.scope(*reader)
            derived.update(self.eager_read(scope, field, old_value, new_value))
        for reader in self.readers(classkey + "::__dict__"):
            self.opaque_eager_read(self.scope(*reader), field)
        for name in derived:
            if self.readers(classkey + "::" + name):
                raise UnsafeValue("derived field has eager consumers")
            for reader in self.readers(classkey + "::__dict__"):
                self.opaque_eager_read(self.scope(*reader), name)
        return derived

    def literal_change(self, path, qualname, class_node, old, new):
        if type(old) is not type(new) or not isinstance(old, (ast.Assign, ast.AnnAssign)):
            raise UnsafeValue("not a class value assignment")
        old_targets = old.targets if isinstance(old, ast.Assign) else [old.target]
        new_targets = new.targets if isinstance(new, ast.Assign) else [new.target]
        if len(old_targets) != 1 or not isinstance(old_targets[0], ast.Name) or [dump(x) for x in old_targets] != [dump(x) for x in new_targets]:
            raise UnsafeValue("class binding changed")
        if isinstance(old, ast.AnnAssign) and dump(old.annotation) != dump(new.annotation):
            raise UnsafeValue("class annotation changed")
        field = old_targets[0].id
        if field.startswith("__") or class_node.decorator_list:
            raise UnsafeValue("special or decorated class semantics")
        old_value, new_value = ast.literal_eval(old.value), ast.literal_eval(new.value)
        if shape(old_value) != shape(new_value):
            raise UnsafeValue("literal type or container keys changed")
        # Class-local reads bypass attribute lookup and can eagerly derive
        # other fields; retain the broad fallback for those definitions.
        for statement in class_node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                eager = list(statement.decorator_list) + list(statement.args.defaults) + [node for node in statement.args.kw_defaults if node is not None]
                eager += [arg.annotation for arg in statement.args.posonlyargs + statement.args.args + statement.args.kwonlyargs if arg.annotation is not None]
                if statement.returns is not None:
                    eager.append(statement.returns)
            elif isinstance(statement, ast.ClassDef):
                eager = list(statement.bases) + list(statement.decorator_list) + [keyword.value for keyword in statement.keywords]
            else:
                eager = [statement]
            if any(isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == field for expression in eager for node in ast.walk(expression)):
                raise UnsafeValue("class-local eager dependency")
        classkey = path + "::" + qualname
        self.metaclasses_safe(classkey)
        module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        if any(module + "::" + qualname in metas for metas in self.metadata.get("metaclasses", {}).values()):
            raise UnsafeValue("changed class is itself a metaclass")
        derived = self.prove_imports(classkey, field, old_value, new_value)
        selected = set(self.indices(classkey + "::" + field))
        for name in derived | {"__dict__", "__mro__"}:
            selected.update(self.indices(classkey + "::" + name))
        return selected

    def normalize(self, path, before, after):
        selected = set()
        def walk(old_body, new_body, prefix=(), class_node=None):
            if len(old_body) != len(new_body):
                return new_body
            replacements = {}
            for index, (old, new) in enumerate(zip(old_body, new_body)):
                if dump(old) == dump(new):
                    continue
                if isinstance(old, ast.ClassDef) and isinstance(new, ast.ClassDef):
                    body = walk(old.body, new.body, prefix + (old.name,), old)
                    if body is not new.body:
                        replacement = copy.copy(new)
                        replacement.body = body
                        replacements[index] = replacement
                elif class_node is not None:
                    try:
                        consumers = self.literal_change(path, ".".join(prefix), class_node, old, new)
                    except (UnsafeValue, ValueError, TypeError, SyntaxError, AttributeError):
                        continue
                    replacements[index] = old
                    selected.update(consumers)
            return [replacements.get(index, node) for index, node in enumerate(new_body)] if replacements else new_body
        body = walk(before.body, after.body)
        normalized = copy.copy(after) if body is not after.body else after
        if body is not after.body:
            normalized.body = body
        return normalized, selected

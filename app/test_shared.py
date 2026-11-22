import ast
import unittest

from shared import SharedRefiner


PATH = "sqlglot/example.py"
KEY = PATH + "::C"


class SharedValues(unittest.TestCase):
    def metadata(self):
        return {"version": 2, "fields": {KEY + "::X": [0]}, "imports": {}, "metaclasses": {KEY: ["builtins::type", "builtins::object"]}}

    def refine(self, old, new, metadata=None):
        refiner = SharedRefiner({PATH: old}, metadata or self.metadata())
        return refiner.normalize(PATH, ast.parse(old), ast.parse(new))

    def test_plain_literal_consumers(self):
        old = "class C:\n X=1\n"
        result, selected = self.refine(old, old.replace("X=1", "X=2"))
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(old)))
        self.assertEqual(selected, {0})

    def test_type_and_key_changes_stay_broad(self):
        for old, new in [("class C:\n X=True\n", "class C:\n X=1\n"), ("class C:\n X={'a':1}\n", "class C:\n X={'b':1}\n")]:
            result, selected = self.refine(old, new)
            self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))
            self.assertEqual(selected, set())

    def test_unobserved_metaclass_stays_broad(self):
        old = "class C:\n X=1\n"
        metadata = self.metadata()
        metadata["metaclasses"] = {}
        new = old.replace("X=1", "X=2")
        result, _ = self.refine(old, new, metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_eager_value_use_stays_broad(self):
        old = "def init(cls):\n return 1/cls.X\nclass C:\n X=1\n"
        metadata = self.metadata()
        metadata["imports"] = {KEY + "::X": [[PATH, 1, "init"]]}
        new = old.replace("X=1", "X=0")
        result, _ = self.refine(old, new, metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_key_only_import_flow_is_safe(self):
        old = "def init(cls):\n d=cls.X\n if 'a' in d:\n  cls.Y=True\nclass C:\n X={'a':1}\n"
        metadata = self.metadata()
        metadata["imports"] = {KEY + "::X": [[PATH, 1, "init"]]}
        result, selected = self.refine(old, old.replace("'a':1", "'a':2"), metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(old)))
        self.assertEqual(selected, {0})

    def test_boolean_projection_adds_derived_readers(self):
        old = "def init(cls):\n cls.REQUIRED={k for k,v in cls.X.items() if v}\nclass C:\n X={'a':True,'b':False}\n"
        metadata = self.metadata()
        metadata["imports"] = {KEY + "::X": [[PATH, 1, "init"]]}
        metadata["fields"][KEY + "::REQUIRED"] = [1]
        result, selected = self.refine(old, old.replace("'b':False", "'b':True"), metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(old)))
        self.assertEqual(selected, {0, 1})

    def test_reflection_of_another_field_is_unchanged(self):
        old = "def init(cls):\n return cls.__dict__.get('Y')\nclass C:\n X=1\n"
        metadata = self.metadata()
        metadata["imports"] = {KEY + "::__dict__": [[PATH, 1, "init"]]}
        result, _ = self.refine(old, old.replace("X=1", "X=2"), metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(old)))

    def test_reflection_of_changed_field_stays_broad(self):
        old = "def init(cls):\n return cls.__dict__.get('X')\nclass C:\n X=1\n"
        metadata = self.metadata()
        metadata["imports"] = {KEY + "::__dict__": [[PATH, 1, "init"]]}
        new = old.replace("X=1", "X=2")
        result, _ = self.refine(old, new, metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_class_local_dependency_stays_broad(self):
        old = "class C:\n X=1\n Y=X+1\n"
        new = old.replace("X=1", "X=2")
        result, _ = self.refine(old, new)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_method_defaults_capture_class_values_eagerly(self):
        old = "class C:\n X=1\n def f(self,y=X): return y\n"
        new = old.replace("X=1", "X=2")
        result, _ = self.refine(old, new)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_decorated_and_special_fields_stay_broad(self):
        for old, new in [("@decorate\nclass C:\n X=1\n", "@decorate\nclass C:\n X=2\n"), ("class C:\n __slots__=['x']\n", "class C:\n __slots__=['y']\n")]:
            result, _ = self.refine(old, new)
            self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_opaque_metaclass_namespace_stays_broad(self):
        old = "class Meta(type):\n def __new__(meta,name,bases,attrs):\n  print(attrs['X'])\n  return super().__new__(meta,name,bases,attrs)\nclass C(metaclass=Meta):\n X=1\n"
        metadata = self.metadata()
        metadata["metaclasses"][KEY] = ["sqlglot.example::Meta", "builtins::type", "builtins::object"]
        new = old.replace("X=1", "X=2")
        result, _ = self.refine(old, new, metadata)
        self.assertEqual(ast.dump(result), ast.dump(ast.parse(new)))

    def test_normalization_preserves_input_trees_and_cache(self):
        old, new = "class C:\n X=1\n", "class C:\n X=2\n"
        before, after = ast.parse(old), ast.parse(new)
        snapshots = (ast.dump(before, include_attributes=True), ast.dump(after, include_attributes=True))
        refiner = SharedRefiner({PATH: old}, self.metadata())
        for _ in range(2):
            result, selected = refiner.normalize(PATH, before, after)
            self.assertEqual(ast.dump(result), ast.dump(before))
            self.assertEqual(selected, {0})
            self.assertEqual(snapshots, (ast.dump(before, include_attributes=True), ast.dump(after, include_attributes=True)))


if __name__ == "__main__":
    unittest.main()

import ast
import unittest

from elements import ElementRefiner


class DeferredElements(unittest.TestCase):
    def refiner(self, source, calls=()):
        return ElementRefiner({"sources": {"sqlglot/example.py": source}, "import_calls": {"calls": list(calls), "failed": []}})

    def test_lambda_body_is_deferred(self):
        old = "TABLE = {'x': lambda x: x+1}\n"
        new = "TABLE = {'x': lambda x: x+2}\n"
        normalized, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(new))
        self.assertEqual(ast.dump(normalized), ast.dump(ast.parse(old)))
        self.assertEqual(len(regions), 1)
        self.assertIsInstance(regions[0][1], ast.Lambda)

    def test_lambda_default_change_is_eager(self):
        old = "TABLE = lambda x=1: x\n"
        new = "TABLE = lambda x=2: x\n"
        normalized, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(new))
        self.assertEqual(regions, [])
        self.assertEqual(ast.dump(normalized), ast.dump(ast.parse(new)))

    def test_imported_lambda_cannot_be_refined(self):
        old = "TABLE = lambda x: x+1\n"
        new = "TABLE = lambda x: x+2\n"
        _, regions = self.refiner(old, [("sqlglot/example.py", 1, "<lambda>")]).normalize("sqlglot/example.py", ast.parse(old), ast.parse(new))
        self.assertEqual(regions, [])

    def test_pure_closure_factory(self):
        old = "def build(*args, **kwargs):\n def result(x):\n  return x(*args, **kwargs)\n return result\nTABLE = build(target=True)\n"
        new = old.replace("target=True", "target=False")
        normalized, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(new))
        self.assertEqual(ast.dump(normalized), ast.dump(ast.parse(old)))
        self.assertEqual(regions[0][1].name, "result")

    def test_factory_eager_statements_cannot_be_refined(self):
        old = "def build(x):\n if x: raise ValueError()\n return lambda: x\nTABLE = build(1)\n"
        _, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(old.replace("build(1)", "build(2)")))
        self.assertEqual(regions, [])

    def test_nested_defaults_cannot_be_refined(self):
        old = "def build(x):\n def result(y=1/x):\n  return y\n return result\nTABLE = build(1)\n"
        _, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(old.replace("build(1)", "build(0)")))
        self.assertEqual(regions, [])

    def test_class_factory_shadowing_cannot_be_refined(self):
        old = "def build(x):\n return lambda: x\nclass C:\n build=other\n TABLE=build(1)\n"
        _, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(old.replace("build(1)", "build(2)")))
        self.assertEqual(regions, [])

    def test_eager_constant_change_alongside_lambda_is_rejected(self):
        old = "TABLE = (1, lambda: 1)\n"
        new = "TABLE = (True, lambda: 2)\n"
        _, regions = self.refiner(old).normalize("sqlglot/example.py", ast.parse(old), ast.parse(new))
        self.assertEqual(regions, [])


if __name__ == "__main__":
    unittest.main()

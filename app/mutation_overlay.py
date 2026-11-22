"""Install development mutation sources before parent or spawned-worker imports."""
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

from patches import apply_patch, parse_patch


def install(root, case_path):
    root = Path(root).resolve()
    if any(name == 'sqlglot' or name.startswith('sqlglot.') for name in sys.modules):
        raise RuntimeError('mutation overlay must precede SQLGlot imports')
    case = json.loads(Path(case_path).read_text())
    modified = {patch.old: apply_patch((root/patch.old).read_text(encoding='utf-8'), patch) for patch in parse_patch(case['patch'])}
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split('.')[0] != 'sqlglot':
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is None or not spec.origin:
                return None
            relative = Path(spec.origin).relative_to(root).as_posix()
            if relative not in modified:
                return None
            origin = spec.origin
            class Loader(importlib.machinery.SourceFileLoader):
                def get_code(self, fullname):
                    print(f'Mutation overlay process {os.getpid()}: {relative}', file=sys.stderr)
                    return compile(modified[relative], origin, 'exec', dont_inherit=True)
            spec.loader = Loader(fullname, origin)
            return spec
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())


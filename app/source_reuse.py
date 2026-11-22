"""Share unchanged statement ASTs after the complete modified source compiles.

Source spans use UTF-8 byte columns, matching Python's AST locations. Decorator
lists need their own comparison because FunctionDef/ClassDef spans start at
the declaration rather than the first decorator. No baseline node is mutated.
"""
import ast


class SourceSpans:
    def __init__(self, source):
        self.raw = source.encode('utf-8')
        self.offsets = [0]
        for line in self.raw.splitlines(keepends=True):
            self.offsets.append(self.offsets[-1] + len(line))

    def segment(self, node):
        if not hasattr(node, 'end_lineno') or node.end_lineno is None:
            return None
        start = self.offsets[node.lineno-1] + node.col_offset
        end = self.offsets[node.end_lineno-1] + node.end_col_offset
        return self.raw[start:end]


def reuse_statements(before, after, old_spans, modified):
    new_spans = SourceSpans(modified)

    def visit(old, new):
        if old is new or type(old) is not type(new):
            return new
        if isinstance(old, ast.stmt):
            decorators_match = not hasattr(old, 'decorator_list') or ast.dump(ast.Module(body=old.decorator_list, type_ignores=[]), include_attributes=False) == ast.dump(ast.Module(body=new.decorator_list, type_ignores=[]), include_attributes=False)
            if decorators_match and old_spans.segment(old) == new_spans.segment(new):
                return old
        if isinstance(old, ast.AST):
            for field in old._fields:
                a, b = getattr(old, field, None), getattr(new, field, None)
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    replacement = visit(a, b)
                    if replacement is not b:
                        setattr(new, field, replacement)
                elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                    for n, (x, y) in enumerate(zip(a, b)):
                        if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                            b[n] = visit(x, y)
        return new

    return visit(before, after)

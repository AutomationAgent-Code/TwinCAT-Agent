"""Bounded local CONSTANT evaluation for fixed array shapes; no eval/COM."""
import ast
import re
from .integer_literals import integer_literal


def resolve_shapes(symbols):
    cache = {}

    def constant(name, chain):
        name = name.upper()
        if name in cache:
            return cache[name]
        entry = symbols.get(name, {})
        if name in chain or len(chain) >= 16 or not entry.get('constant'):
            return None
        value = number(entry.get('initializer', ''), (*chain, name))
        cache[name] = value
        return value

    def number(text, chain=()):
        if not text or len(text) > 512:
            return None
        literal = integer_literal(text.strip())
        if literal:
            return literal[0]
        try:
            tree = ast.parse(text.strip(), mode='eval')
            if sum(1 for _ in ast.walk(tree)) > 64:
                return None
            def visit(node):
                if isinstance(node, ast.Constant) and type(node.value) is int:
                    result = node.value
                elif isinstance(node, ast.Name):
                    result = constant(node.id, chain)
                elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                    value = visit(node.operand)
                    result = None if value is None else value if isinstance(node.op, ast.UAdd) else -value
                elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult)):
                    left, right = visit(node.left), visit(node.right)
                    result = None if left is None or right is None else (left + right if isinstance(node.op, ast.Add)
                             else left - right if isinstance(node.op, ast.Sub) else left * right)
                else:
                    return None
                return result if result is not None and -(2**63) <= result < 2**64 else None
            return visit(tree.body)
        except (SyntaxError, ValueError, RecursionError):
            return None

    for entry in symbols.values():
        def bounds(match):
            dims = []
            for dim in match[1].split(','):
                limits = dim.split('..')
                if len(limits) != 2:
                    return match[0]
                low, high = (number(v) for v in limits)
                if low is None or high is None or not -(2**31) <= low <= high < 2**31:
                    return match[0]
                dims.append(f'{low}..{high}')
            return 'ARRAY[' + ','.join(dims) + ']'
        entry['type'] = re.sub(r'ARRAY\s*\[([^\]]+)\]', bounds, entry['type'], flags=re.I)

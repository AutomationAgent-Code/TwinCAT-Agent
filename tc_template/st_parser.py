"""Bounded, offline ST statement parser. No COM, subprocess or source writes.

This is deliberately a parser, not a replacement TwinCAT compiler. Unsupported
extensions are reported separately from invalid syntax. Positions refer to the
original split-editor implementation, including comments and string literals.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class Token:
    value: str
    pos: int
    kind: str = 'symbol'


@dataclass
class Node:
    kind: str
    token: Token
    children: tuple = ()


class ParseError(ValueError):
    def __init__(self, token, message, unsupported=False):
        self.token, self.unsupported = token, unsupported
        super().__init__(message)


_LEX = re.compile(
    r"(?P<literal>(?:SINT|USINT|INT|UINT|DINT|UDINT|LINT|ULINT|BYTE|WORD|DWORD|LWORD)#[+-]?(?:(?:2|8|16)#)?[0-9A-F_]+"
    r"|(?:[A-Za-z_]\w*#)+(?:[A-Za-z0-9_]+(?:\.[0-9_]+)?(?::[0-9_.]+)*(?:-[0-9_:]+)*)"
    r"|(?:2|8|16)#[0-9A-Fa-f_]+|[0-9][0-9_]*(?:\.(?!\.)[0-9_]+)?(?:[eE][+-]?[0-9_]+)?)"
    r"|(?P<id>[A-Za-z_]\w*)|(?P<symbol>REF=|:=|=>|<=|>=|<>|\*\*|\.\.|S=|R=|[+*/=<>\-:;,.()\[\]^&])",
    re.I)
_RESERVED = set('IF THEN ELSIF ELSE END_IF CASE OF END_CASE FOR TO BY DO END_FOR WHILE END_WHILE REPEAT UNTIL END_REPEAT VAR END_VAR'.split())


def tokenize(source):
    if len(source) > 512_000:
        raise ParseError(Token('', 0), 'Implementation exceeds offline parser budget.', True)
    if '{' in source:
        from .conditional_compilation import preprocess_local
        source=preprocess_local(source)['source']
    tokens, i = [], 0
    while i < len(source):
        if source[i].isspace():
            i += 1
            continue
        if source.startswith('//', i):
            end = source.find('\n', i)
            i = len(source) if end < 0 else end + 1
            continue
        if source.startswith('(*', i):
            start, depth, i = i, 1, i + 2
            while i < len(source) and depth:
                pair = source[i:i + 2]
                if pair == '(*':
                    depth += 1
                elif pair == '*)':
                    depth -= 1
                i += 2 if pair in {'(*', '*)'} else 1
            if depth:
                raise ParseError(Token('', start), 'Unclosed comment.')
            continue
        if source[i] == '{':
            end = source.find('}', i + 1)
            if end < 0:
                raise ParseError(Token('', i), 'Unclosed pragma.')
            pragma = source[i:end + 1]
            if re.fullmatch(r'\{\s*(?:region\s+"[^"\r\n]*"|endregion)\s*\}', pragma, re.I):
                i = end + 1
                continue
            # Pragmas affect scope, constants and conditional compilation.
            raise ParseError(Token(source[i:end + 1], i), 'Pragma semantics require explicit support; not verified.', True)
        if source[i] in "'\"":
            start, quote, i = i, source[i], i + 1
            while i < len(source) and source[i] != quote:
                i += 2 if source[i] == '$' else 1
            if i >= len(source):
                raise ParseError(Token('', start), 'Unclosed string literal.')
            i += 1
            tokens.append(Token(source[start:i], start, 'string'))
            continue
        keyword_operator = re.match(r'(?:REF|S|R)=', source[i:], re.I)
        if keyword_operator:
            tokens.append(Token(keyword_operator[0], i))
            i += len(keyword_operator[0])
            continue
        match = _LEX.match(source, i)
        if not match:
            raise ParseError(Token(source[i], i), 'Unsupported token ' + source[i], True)
        tokens.append(Token(match[0], i, match.lastgroup))
        i = match.end()
        if len(tokens) > 50_000:
            raise ParseError(tokens[-1], 'Token budget exceeded.', True)
    tokens.append(Token('<EOF>', len(source)))
    return tokens


class Parser:
    _PREC = {'OR': 1, 'OR_ELSE': 1, 'XOR': 2, 'AND': 3, 'AND_THEN': 3, '&': 3,
             '=': 4, '<>': 4, '<': 5, '<=': 5, '>': 5, '>=': 5,
             '+': 6, '-': 6, '*': 7, '/': 7, 'MOD': 7, '**': 8}

    def __init__(self, source):
        self.tokens, self.i = tokenize(source), 0

    @property
    def tok(self):
        return self.tokens[self.i]

    def is_(self, value):
        return self.tok.value.upper() == value

    def take(self):
        token = self.tok
        if not self.is_('<EOF>'):
            self.i += 1
        return token

    def accept(self, value):
        if self.is_(value):
            return self.take()
        return None

    def expect(self, value):
        if not self.is_(value):
            raise ParseError(self.tok, f'Expected {value}, found {self.tok.value}.')
        return self.take()

    def identifier(self):
        if self.tok.kind != 'id' or self.tok.value.upper() in _RESERVED:
            raise ParseError(self.tok, 'Expected identifier.')
        return self.take()

    def expr(self, minimum=0):
        token = self.take()
        upper = token.value.upper()
        if upper in {'+', '-', 'NOT'}:
            node = Node('unary', token, (self.expr(8),))
        elif upper == '(':
            node = self.expr()
            self.expect(')')
        elif token.kind in {'literal', 'string'} or upper in {'TRUE', 'FALSE'}:
            node = Node('literal', token)
        elif token.kind == 'id' and upper not in _RESERVED:
            node = Node('name', token)
        else:
            raise ParseError(token, 'Expected expression, found ' + token.value)
        while True:
            if self.accept('.'):
                member = self.identifier()
                node = Node('member', member, (node,))
            elif self.accept('^'):
                node = Node('deref', token, (node,))
            elif self.accept('['):
                indices = [self.expr()]
                while self.accept(','):
                    indices.append(self.expr())
                self.expect(']')
                node = Node('index', node.token, (node, *indices))
            elif self.accept('('):
                args = []
                if not self.is_(')'):
                    while True:
                        if self.tok.kind == 'id' and self.tokens[self.i + 1].value in {':=', '=>'}:
                            name, op = self.take(), self.take()
                            args.append(Node('argument', name, (op.value, self.expr())))
                        else:
                            args.append(self.expr())
                        if not self.accept(','):
                            break
                self.expect(')')
                node = Node('call', node.token, (node, *args))
            else:
                op = self.tok.value.upper()
                # ExST permits assignments as expressions. Retain their AST
                # rather than rejecting valid TwinCAT syntax as generic ST.
                if minimum == 0 and op in {':=', 'REF=', 'S=', 'R='}:
                    operator = self.take()
                    if node.kind not in {'name', 'member', 'index', 'deref'}:
                        raise ParseError(node.token, 'Assignment target must be writable.')
                    node = Node('assign', operator, (node, self.expr()))
                    continue
                priority = self._PREC.get(op, -1)
                if priority < minimum:
                    break
                token = self.take()
                node = Node('binary', token, (node, self.expr(priority if op == '**' else priority + 1)))
        return node

    def block(self, stops=()):
        statements = []
        while not self.is_('<EOF>') and self.tok.value.upper() not in stops:
            if self.accept(';'):
                continue
            statements.append(self.statement())
        return tuple(statements)

    def statement(self):
        token = self.tok
        if self.accept('IF'):
            branches = []
            while True:
                condition = self.expr()
                self.expect('THEN')
                branches.append(Node('branch', token, (condition, *self.block({'ELSIF', 'ELSE', 'END_IF'}))))
                if not self.accept('ELSIF'):
                    break
            if self.accept('ELSE'):
                branches.extend(self.block({'END_IF'}))
            self.expect('END_IF')
            self.accept(';')  # TwinCAT allows omitted semicolon after block terminators.
            return Node('if', token, tuple(branches))
        if self.accept('WHILE'):
            condition = self.expr()
            self.expect('DO')
            body = self.block({'END_WHILE'})
            self.expect('END_WHILE')
            self.accept(';')
            return Node('while', token, (condition, *body))
        if self.accept('REPEAT'):
            body = self.block({'UNTIL'})
            self.expect('UNTIL')
            condition = self.expr()
            self.expect('END_REPEAT')
            self.accept(';')
            return Node('repeat', token, (condition, *body))
        if self.accept('FOR'):
            variable = Node('name', self.identifier())
            self.expect(':=')
            first = self.expr()
            self.expect('TO')
            last = self.expr()
            step = self.expr() if self.accept('BY') else Node('literal', Token('1', token.pos, 'literal'))
            self.expect('DO')
            body = self.block({'END_FOR'})
            self.expect('END_FOR')
            self.accept(';')
            return Node('for', token, (variable, first, last, step, *body))
        if self.accept('CASE'):
            selector = self.expr()
            self.expect('OF')
            branches = []
            while not self.is_('END_CASE') and not self.is_('ELSE'):
                labels = [self.expr()]
                if self.accept('..'):
                    labels.append(self.expr())
                while self.accept(','):
                    labels.append(self.expr())
                    if self.accept('..'):
                        labels.append(self.expr())
                self.expect(':')
                body = []
                while not self.is_('END_CASE') and not self.is_('ELSE'):
                    # A label is an expression/list/range followed by ':'.
                    saved = self.i
                    try:
                        self.expr()
                        while self.accept(',') or self.accept('..'):
                            self.expr()
                        next_label = self.is_(':')
                    except ParseError:
                        next_label = False
                    finally:
                        self.i = saved
                    if next_label:
                        break
                    if not self.accept(';'):
                        body.append(self.statement())
                branches.append(Node('case_branch', token, (tuple(labels), *body)))
            if self.accept('ELSE'):
                branches.extend(self.block({'END_CASE'}))
            self.expect('END_CASE')
            self.accept(';')
            return Node('case', token, (selector, *branches))
        if self.tok.value.upper() in {'RETURN', 'EXIT', 'CONTINUE'}:
            self.take()
            self.expect(';')
            return Node('control', token)
        node = self.expr()
        if self.tok.value.upper() in {':=', 'REF=', 'S=', 'R='}:
            op = self.take()
            if node.kind not in {'name', 'member', 'index', 'deref'}:
                raise ParseError(node.token, 'Assignment target must be writable.')
            node = Node('assign', op, (node, self.expr()))
        elif node.kind not in {'call', 'assign'}:
            raise ParseError(node.token, 'Expected assignment or call statement.')
        self.expect(';')
        return node


def parse_implementation(source):
    try:
        parser = Parser(source)
        nodes = parser.block()
        parser.expect('<EOF>')
        return {'status': 'parsed', 'nodes': nodes, 'findings': []}
    except (ParseError, RecursionError) as exc:
        if isinstance(exc, RecursionError):
            exc = ParseError(Token('', 0), 'Parser nesting budget exceeded.', True)
        return {'status': 'unsupported' if exc.unsupported else 'invalid', 'nodes': (),
                'findings': [{'rule': 'syntax-unsupported' if exc.unsupported else 'syntax-statement',
                              'severity': 'error', 'area': 'implementation',
                              'line': source.count('\n', 0, exc.token.pos) + 1,
                              'column': exc.token.pos - source.rfind('\n', 0, exc.token.pos),
                              'message': str(exc)}]}

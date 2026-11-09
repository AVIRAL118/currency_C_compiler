"""
parser.py - Phase 2: Syntax Analysis (hand-written Recursive Descent Parser)
for the Currency-C (.cur) language.

Consumes the token list from lexer.py and builds an Abstract Syntax Tree (AST).
Only SYNTAX is checked here. Semantic checks (symbol <-> declared currency code,
pointer rules for '->', array size > 0, file-call arity, ...) belong to Phase 3.

Grammar implemented (one parse_* method per rule):

    program        := { struct_def | statement } EOF
    struct_def     := 'struct' IDENT '{' { declaration } '}' ';'
    declaration    := type_spec declarator { ',' declarator } ';'
    declarator     := { '*' } IDENT [ '[' INT ']' ] [ '=' expression ]
    type_spec      := 'int' | 'float' | 'FILE' | 'struct' IDENT
                    | 'currency' '<' CURRENCY_CODE '>'

    statement      := declaration | block | if_stmt | while_stmt | input_stmt
                    | output_stmt | return_stmt | expr_stmt
    block          := '{' { statement } '}'
    if_stmt        := 'if' '(' expression ')' statement [ 'else' statement ]
    while_stmt     := 'while' '(' expression ')' statement
    input_stmt     := 'input' '(' lvalue ')' ';'
    output_stmt    := 'output' '(' expression ')' ';'
    return_stmt    := 'return' [ expression ] ';'
    expr_stmt      := ( assignment | expression ) ';'
    assignment     := lvalue '=' expression
    lvalue         := ( IDENT | '*' unary ) { '.' IDENT | '->' IDENT | '[' expression ']' }

    expression     := logic_or
    logic_or       := logic_and  { '||' logic_and }
    logic_and      := equality   { '&&' equality }
    equality       := relational { ('==' | '!=') relational }
    relational     := additive   { ('<' | '>' | '<=' | '>=') additive }
    additive       := term       { ('+' | '-') term }
    term           := unary      { ('*' | '/') unary }
    unary          := ('-' | '&' | '*') unary | postfix
    postfix        := primary    { '.' IDENT | '->' IDENT | '[' expression ']' }
    primary        := INT | FLOAT | STRING | currency_literal | file_call | IDENT
                    | '(' expression ')'
    currency_literal := CURRENCY_SYMBOL (INT | FLOAT)
    file_call      := FILE_FUNC '(' [ expression { ',' expression } ] ')'

Design notes:
  * Assignment is a STATEMENT, not an expression (a = b = 3 is an error).
  * Only built-in calls exist (input/output/file functions); foo(x) is an error.
  * Pointers: '*' binds to each DECLARATOR (int *a, b;  => a is int*, b is int).
    '*' is also unary dereference; '&' is unary address-of. Whether a '*', '->'
    or '&' is TYPE-correct is a Phase 3 question.
  * An assignment target is parsed as an ordinary expression first, then checked
    for being an lvalue. This avoids any backtracking.
  * Binary operators use precedence climbing over BINARY_LEVELS (one loop instead
    of one method per level).
  * Error handling: parse() raises ParseError on the FIRST syntax error (default).
    parse(recover=True) instead uses panic-mode recovery (skip to the next ';' or
    '}'), never raises ParseError, and collects every error in `Parser.errors`.
    A tree built with recover=True may be incomplete - do not generate code from
    it unless `errors` is empty.

Usage:
    python parser.py                    # parse the lexer's built-in sample
    python parser.py program.cur        # parse a file
    python parser.py program.cur --recover   # report all syntax errors, not just the first

    from parser import Parser
    ast = Parser.from_source(text).parse()
"""

import argparse
import sys
from dataclasses import dataclass, field, fields
from typing import List, NoReturn, Optional

from lexer import Lexer, LexerError, Token, TokenStream, TokenType, SAMPLE_SOURCE


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ParseError(Exception):
    """A syntax error, carrying the offending line number."""

    def __init__(self, message, line_number):
        super().__init__(f"Syntax error at line {line_number}: {message}")
        self.message = message
        self.line_number = line_number


# ---------------------------------------------------------------------------
# AST nodes - every node ends with `line_number` for Phase 3 error reporting
# ---------------------------------------------------------------------------
class Node:
    """Base class for all AST nodes."""


# ---- Program structure ----
@dataclass
class Program(Node):
    items: List[Node] = field(default_factory=list)     # StructDef | statements
    line_number: int = 1


@dataclass
class TypeSpec(Node):
    kind: str = ""                         # 'int' | 'float' | 'FILE' | 'struct' | 'currency'
    name: Optional[str] = None             # struct name      (kind == 'struct')
    currency_code: Optional[str] = None    # INR/USD/EUR/GBP  (kind == 'currency')
    line_number: int = 0


@dataclass
class Declarator(Node):
    name: str = ""
    array_size: Optional[int] = None       # None => not an array
    init: Optional[Node] = None            # initializer expression, if any
    pointer_level: int = 0                 # number of leading '*' (int **pp => 2)
    line_number: int = 0


@dataclass
class Declaration(Node):
    type_spec: Optional[TypeSpec] = None
    declarators: List[Declarator] = field(default_factory=list)
    line_number: int = 0


@dataclass
class StructDef(Node):
    name: str = ""
    members: List[Declaration] = field(default_factory=list)
    line_number: int = 0


# ---- Statements ----
@dataclass
class Block(Node):
    statements: List[Node] = field(default_factory=list)
    line_number: int = 0


@dataclass
class If(Node):
    condition: Optional[Node] = None
    then_branch: Optional[Node] = None
    else_branch: Optional[Node] = None
    line_number: int = 0


@dataclass
class While(Node):
    condition: Optional[Node] = None
    body: Optional[Node] = None
    line_number: int = 0


@dataclass
class Input(Node):
    target: Optional[Node] = None
    line_number: int = 0


@dataclass
class Output(Node):
    value: Optional[Node] = None
    line_number: int = 0


@dataclass
class Return(Node):
    value: Optional[Node] = None
    line_number: int = 0


@dataclass
class ExprStmt(Node):
    expr: Optional[Node] = None
    line_number: int = 0


@dataclass
class Assignment(Node):
    target: Optional[Node] = None          # Identifier | MemberAccess | Index
    value: Optional[Node] = None
    line_number: int = 0


# ---- Expressions ----
@dataclass
class BinaryOp(Node):
    op: str = ""
    left: Optional[Node] = None
    right: Optional[Node] = None
    line_number: int = 0


@dataclass
class UnaryOp(Node):
    op: str = ""                           # '-' | '&' (address-of) | '*' (dereference)
    operand: Optional[Node] = None
    line_number: int = 0


@dataclass
class IntLiteral(Node):
    value: int = 0
    line_number: int = 0


@dataclass
class FloatLiteral(Node):
    value: float = 0.0
    text: str = ""                         # exact lexeme, e.g. '50000.00'
    line_number: int = 0


@dataclass
class StringLiteral(Node):
    value: str = ""                        # exact lexeme INCLUDING the quotes, e.g. '"log.txt"'
    line_number: int = 0


@dataclass
class CurrencyLiteral(Node):
    symbol: str = ""                       # '$' | '₹' | '€' | '£'
    amount: Optional[Node] = None          # IntLiteral | FloatLiteral
    line_number: int = 0


@dataclass
class Identifier(Node):
    name: str = ""
    line_number: int = 0


@dataclass
class MemberAccess(Node):
    obj: Optional[Node] = None
    member: str = ""
    arrow: bool = False                    # False => '.', True => '->'
    line_number: int = 0


@dataclass
class Index(Node):
    obj: Optional[Node] = None
    index: Optional[Node] = None
    line_number: int = 0


@dataclass
class FileCall(Node):
    name: str = ""                         # fopen, fclose, ...
    args: List[Node] = field(default_factory=list)
    line_number: int = 0


# ---------------------------------------------------------------------------
# The Parser
# ---------------------------------------------------------------------------
TYPE_KEYWORDS = {"int", "float", "FILE", "currency", "struct"}

# Binary operator precedence, lowest -> highest. All are left-associative.
BINARY_LEVELS = [
    ("||",),
    ("&&",),
    ("==", "!="),
    ("<", ">", "<=", ">="),
    ("+", "-"),
    ("*", "/"),
]
BINARY_PREC = {op: prec for prec, ops in enumerate(BINARY_LEVELS, 1) for op in ops}

NUMBER_TOKENS = (TokenType.INT_LITERAL, TokenType.FLOAT_LITERAL)


class Parser:
    MAX_ERRORS = 20                         # recovery mode gives up after this many

    def __init__(self, tokens):
        self.stream = TokenStream(tokens)
        self.errors: List[ParseError] = []  # filled only by parse(recover=True)
        self._keyword_stmt = {              # keyword -> statement parser
            "if": self.parse_if, "while": self.parse_while,
            "input": self.parse_input, "output": self.parse_output,
            "return": self.parse_return,
        }

    @classmethod
    def from_source(cls, source):
        """Lex `source` and build a Parser over the resulting tokens."""
        return cls(Lexer(source).tokenize())

    # ----- low-level helpers ------------------------------------------------
    @staticmethod
    def _describe(tok: Token) -> str:
        return "end of input" if tok.type == TokenType.EOF else repr(tok.value)

    def _error(self, message, tok=None) -> NoReturn:
        tok = tok or self.stream.peek()
        raise ParseError(message, tok.line_number)

    def _expect(self, type_, value=None, what=None) -> Token:
        """Consume a matching token or raise a ParseError."""
        tok = self.stream.match(type_, value)
        if tok is None:
            wanted = what or (repr(value) if value is not None else type_.name)
            self._error(f"expected {wanted} but found {self._describe(self.stream.peek())}")
        return tok

    def _check_delim(self, value):
        return self.stream.check(TokenType.DELIMITER, value)

    def _check_op(self, value):
        return self.stream.check(TokenType.OPERATOR, value)

    # ----- program & declarations ------------------------------------------
    def parse(self, recover=False) -> Program:
        """Entry point: parse the whole token stream into a Program node.

        recover=False: raise ParseError at the first syntax error.
        recover=True : skip past each error, collect them in self.errors and
                       return whatever parsed cleanly."""
        self.errors = []
        items = []
        while not self.stream.check(TokenType.EOF):
            try:
                items.append(self._parse_item())
            except ParseError as err:
                if not recover:
                    raise
                self.errors.append(err)
                if len(self.errors) >= self.MAX_ERRORS:
                    break
                self._synchronize()
        return Program(items=items, line_number=1)

    def _parse_item(self) -> Node:
        return self.parse_struct_def() if self._at_struct_def() else self.parse_statement()

    def _synchronize(self):
        """Panic-mode recovery: discard tokens up to and including the next ';' or '}'.
        Always consumes at least one token (unless at EOF), so parsing cannot loop."""
        while not self.stream.check(TokenType.EOF):
            tok = self.stream.advance()
            if tok.type == TokenType.DELIMITER and tok.value in (";", "}"):
                return

    def _at_struct_def(self) -> bool:
        """'struct NAME {' is a definition; 'struct NAME x' is a declaration."""
        s = self.stream
        return (s.peek(0).type == TokenType.KEYWORD and s.peek(0).value == "struct"
                and s.peek(1).type == TokenType.IDENTIFIER
                and s.peek(2).type == TokenType.DELIMITER and s.peek(2).value == "{")

    def parse_struct_def(self) -> StructDef:
        start = self._expect(TokenType.KEYWORD, "struct")
        name = self._expect(TokenType.IDENTIFIER, what="a struct name")
        self._expect(TokenType.DELIMITER, "{")
        members = []
        while not self._check_delim("}") and not self.stream.check(TokenType.EOF):
            members.append(self.parse_declaration())
        self._expect(TokenType.DELIMITER, "}")
        self._expect(TokenType.DELIMITER, ";", what="';' after struct definition")
        return StructDef(name=name.value, members=members, line_number=start.line_number)

    def parse_type_spec(self) -> TypeSpec:
        tok = self.stream.peek()
        if tok.type != TokenType.KEYWORD or tok.value not in TYPE_KEYWORDS:
            self._error(f"expected a type but found {self._describe(tok)}")
        self.stream.advance()

        if tok.value in ("int", "float", "FILE"):
            return TypeSpec(kind=tok.value, line_number=tok.line_number)
        if tok.value == "struct":
            name = self._expect(TokenType.IDENTIFIER, what="a struct name")
            return TypeSpec(kind="struct", name=name.value, line_number=tok.line_number)
        # currency '<' CURRENCY_CODE '>'
        self._expect(TokenType.OPERATOR, "<", what="'<' after 'currency'")
        code = self._expect(TokenType.CURRENCY_CODE, what="a currency code (INR, USD, EUR, GBP)")
        self._expect(TokenType.OPERATOR, ">", what="'>' after currency code")
        return TypeSpec(kind="currency", currency_code=code.value, line_number=tok.line_number)

    def parse_declaration(self) -> Declaration:
        type_spec = self.parse_type_spec()
        declarators = [self.parse_declarator()]
        while self.stream.match(TokenType.DELIMITER, ","):
            declarators.append(self.parse_declarator())
        self._expect(TokenType.DELIMITER, ";", what="';' after declaration")
        return Declaration(type_spec=type_spec, declarators=declarators,
                           line_number=type_spec.line_number)

    def parse_declarator(self) -> Declarator:
        pointer_level = 0
        while self.stream.match(TokenType.OPERATOR, "*"):    # int *p;  int **pp;
            pointer_level += 1
        name = self._expect(TokenType.IDENTIFIER, what="a variable name")
        size = None
        if self.stream.match(TokenType.DELIMITER, "["):
            size_tok = self._expect(TokenType.INT_LITERAL, what="an integer array size")
            size = int(size_tok.value)
            self._expect(TokenType.DELIMITER, "]")
        init = None
        if self.stream.match(TokenType.OPERATOR, "="):
            init = self.parse_expression()
        return Declarator(name=name.value, array_size=size, init=init,
                          pointer_level=pointer_level, line_number=name.line_number)

    # ----- statements --------------------------------------------------------
    def parse_statement(self) -> Node:
        tok = self.stream.peek()
        if tok.type == TokenType.KEYWORD:
            if tok.value in TYPE_KEYWORDS:
                return self.parse_declaration()
            handler = self._keyword_stmt.get(tok.value)
            if handler is not None:
                return handler()
            if tok.value == "else":
                self._error("'else' without a matching 'if'")
        if tok.type == TokenType.DELIMITER and tok.value == "{":
            return self.parse_block()
        return self.parse_expr_stmt()

    def parse_block(self) -> Block:
        start = self._expect(TokenType.DELIMITER, "{")
        stmts = []
        while not self._check_delim("}") and not self.stream.check(TokenType.EOF):
            stmts.append(self.parse_statement())
        self._expect(TokenType.DELIMITER, "}", what="'}' to close the block")
        return Block(statements=stmts, line_number=start.line_number)

    def _parse_condition(self, keyword) -> Node:
        """'(' expression ')' used by if / while."""
        self._expect(TokenType.DELIMITER, "(", what=f"'(' after '{keyword}'")
        cond = self.parse_expression()
        if self._check_op("="):
            self._error("assignment is not allowed in a condition (did you mean '=='?)")
        self._expect(TokenType.DELIMITER, ")", what="')' after condition")
        return cond

    def parse_if(self) -> If:
        start = self._expect(TokenType.KEYWORD, "if")
        cond = self._parse_condition("if")
        then_branch = self.parse_statement()
        else_branch = None
        if self.stream.match(TokenType.KEYWORD, "else"):
            else_branch = self.parse_statement()      # 'else if' falls out naturally
        return If(condition=cond, then_branch=then_branch, else_branch=else_branch,
                  line_number=start.line_number)

    def parse_while(self) -> While:
        start = self._expect(TokenType.KEYWORD, "while")
        cond = self._parse_condition("while")
        body = self.parse_statement()
        return While(condition=cond, body=body, line_number=start.line_number)

    def parse_input(self) -> Input:
        start = self._expect(TokenType.KEYWORD, "input")
        self._expect(TokenType.DELIMITER, "(", what="'(' after 'input'")
        target = self.parse_expression()
        if not self._is_lvalue(target):
            self._error("input() needs a variable, member or array element", start)
        self._expect(TokenType.DELIMITER, ")")
        self._expect(TokenType.DELIMITER, ";", what="';' after input(...)")
        return Input(target=target, line_number=start.line_number)

    def parse_output(self) -> Output:
        start = self._expect(TokenType.KEYWORD, "output")
        self._expect(TokenType.DELIMITER, "(", what="'(' after 'output'")
        value = self.parse_expression()
        self._expect(TokenType.DELIMITER, ")")
        self._expect(TokenType.DELIMITER, ";", what="';' after output(...)")
        return Output(value=value, line_number=start.line_number)

    def parse_return(self) -> Return:
        start = self._expect(TokenType.KEYWORD, "return")
        value = None
        if not self._check_delim(";"):
            value = self.parse_expression()
        self._expect(TokenType.DELIMITER, ";", what="';' after return")
        return Return(value=value, line_number=start.line_number)

    def parse_expr_stmt(self) -> Node:
        """Parse an expression; if '=' follows, turn it into an Assignment."""
        first = self.stream.peek()
        expr = self.parse_expression()

        if self._check_op("="):
            eq = self.stream.advance()
            if not self._is_lvalue(expr):
                self._error("left side of '=' is not assignable", eq)
            value = self.parse_expression()
            if self._check_op("="):
                self._error("chained assignment is not supported")
            self._expect(TokenType.DELIMITER, ";", what="';' after assignment")
            return Assignment(target=expr, value=value, line_number=first.line_number)

        self._expect(TokenType.DELIMITER, ";", what="';' after expression")
        return ExprStmt(expr=expr, line_number=first.line_number)

    @staticmethod
    def _is_lvalue(node) -> bool:
        """Walk down '.', '->' and '[]' to the root; it must be a variable or a '*' deref."""
        while isinstance(node, (MemberAccess, Index)):
            node = node.obj
        return isinstance(node, Identifier) or (isinstance(node, UnaryOp) and node.op == "*")

    # ----- expressions -------------------------------------------------------
    def parse_expression(self) -> Node:
        return self._parse_binary(1)

    def _parse_binary(self, min_prec) -> Node:
        """Precedence climbing: consume operators whose precedence is >= min_prec.
        Left-associative, because the right operand only takes tighter operators."""
        left = self.parse_unary()
        while True:
            tok = self.stream.peek()
            prec = BINARY_PREC.get(tok.value) if tok.type == TokenType.OPERATOR else None
            if prec is None or prec < min_prec:
                return left
            self.stream.advance()
            right = self._parse_binary(prec + 1)
            left = BinaryOp(op=tok.value, left=left, right=right, line_number=tok.line_number)

    def parse_unary(self) -> Node:
        tok = self.stream.peek()
        if tok.type == TokenType.OPERATOR and tok.value in ("-", "&", "*"):
            self.stream.advance()
            return UnaryOp(op=tok.value, operand=self.parse_unary(), line_number=tok.line_number)
        return self.parse_postfix()

    def parse_postfix(self) -> Node:
        node = self.parse_primary()
        while True:
            tok = self.stream.peek()
            if tok.type == TokenType.OPERATOR and tok.value in (".", "->"):
                self.stream.advance()
                member = self._expect(TokenType.IDENTIFIER, what="a member name")
                node = MemberAccess(obj=node, member=member.value,
                                    arrow=(tok.value == "->"), line_number=tok.line_number)
            elif tok.type == TokenType.DELIMITER and tok.value == "[":
                self.stream.advance()
                idx = self.parse_expression()
                self._expect(TokenType.DELIMITER, "]")
                node = Index(obj=node, index=idx, line_number=tok.line_number)
            else:
                return node

    @staticmethod
    def _number_node(tok: Token) -> Node:
        """INT_LITERAL / FLOAT_LITERAL token -> IntLiteral / FloatLiteral."""
        if tok.type == TokenType.INT_LITERAL:
            return IntLiteral(value=int(tok.value), line_number=tok.line_number)
        return FloatLiteral(value=float(tok.value), text=tok.value, line_number=tok.line_number)

    def parse_primary(self) -> Node:
        tok = self.stream.peek()

        if tok.type in NUMBER_TOKENS:
            self.stream.advance()
            return self._number_node(tok)

        if tok.type == TokenType.STRING_LITERAL:
            self.stream.advance()
            return StringLiteral(value=tok.value, line_number=tok.line_number)

        if tok.type == TokenType.CURRENCY_SYMBOL:
            self.stream.advance()
            amount = self.stream.peek()
            if amount.type not in NUMBER_TOKENS:
                self._error(f"expected a number after currency symbol {tok.value!r} "
                            f"but found {self._describe(amount)}")
            self.stream.advance()
            return CurrencyLiteral(symbol=tok.value, amount=self._number_node(amount),
                                   line_number=tok.line_number)

        if tok.type == TokenType.FILE_FUNCTION:
            return self.parse_file_call()

        if tok.type == TokenType.IDENTIFIER:
            self.stream.advance()
            if self._check_delim("("):
                self._error(f"cannot call {tok.value!r}: only built-in functions are supported", tok)
            return Identifier(name=tok.value, line_number=tok.line_number)

        if tok.type == TokenType.DELIMITER and tok.value == "(":
            self.stream.advance()
            expr = self.parse_expression()
            self._expect(TokenType.DELIMITER, ")", what="')' to close the expression")
            return expr

        self._error(f"unexpected {self._describe(tok)} in expression")

    def parse_file_call(self) -> FileCall:
        name = self._expect(TokenType.FILE_FUNCTION)
        self._expect(TokenType.DELIMITER, "(", what=f"'(' after '{name.value}'")
        args = []
        if not self._check_delim(")"):
            args.append(self.parse_expression())
            while self.stream.match(TokenType.DELIMITER, ","):
                args.append(self.parse_expression())
        self._expect(TokenType.DELIMITER, ")", what=f"')' to close {name.value}(...)")
        return FileCall(name=name.value, args=args, line_number=name.line_number)


# ---------------------------------------------------------------------------
# AST pretty-printer (for the demo and for debugging in later phases)
# ---------------------------------------------------------------------------
def dump(node, indent=0, label=""):
    """Print an AST as an indented tree. Each node shows its source line (@L)."""
    pad = "  " * indent
    tag = f"{label}: " if label else ""

    if isinstance(node, list):
        print(f"{pad}{tag}[{len(node)}]")
        for item in node:
            dump(item, indent + 1)
        return
    if not isinstance(node, Node):
        print(f"{pad}{tag}{node!r}")
        return

    scalars, children = [], []
    for f in fields(node):
        if f.name == "line_number":
            continue
        value = getattr(node, f.name)
        if isinstance(value, (Node, list)):
            children.append((f.name, value))
        elif value is not None and not (f.name == "pointer_level" and value == 0):
            scalars.append(f"{f.name}={value!r}")

    head = type(node).__name__ + (f"({', '.join(scalars)})" if scalars else "")
    print(f"{pad}{tag}{head}  @L{node.line_number}")
    for name, value in children:
        dump(value, indent + 1, name)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
BAD_SNIPPETS = [
    "currency<USD> x = ;",           # missing expression
    "int a; a = b = 3;",             # chained assignment
    "if (x = 3) { }",                # assignment in condition
    "foo(x);",                       # user-defined call
    "int v[n];",                     # non-literal array size
    "currency<JPY> y;",              # unknown currency code
    "5 = x;",                        # not assignable
    "int x = 1\nint y = 2;",         # missing ';' (reported on the NEXT token's line)
    "int *;",                        # pointer with no name
    "int x = *;",                    # dereference with no operand
    "&x = 3;",                       # address-of is not assignable
    "else { }",                      # dangling else
    "{ int z = 1;",                  # unterminated block
]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Currency-C parser (prints the AST)")
    ap.add_argument("path", nargs="?", help="Currency-C source file (default: lexer sample)")
    ap.add_argument("--recover", action="store_true",
                    help="keep parsing after a syntax error and report all of them")
    args = ap.parse_args(argv)

    if args.path:
        try:
            source = Lexer.from_file(args.path).source
        except OSError as err:
            print(err, file=sys.stderr)
            return 1
        print(f"Parsing file: {args.path}\n")
    else:
        source = SAMPLE_SOURCE
        print("Parsing the lexer's built-in sample...\n")

    try:
        parser = Parser.from_source(source)
        program = parser.parse(recover=args.recover)
    except (LexerError, ParseError) as err:
        print(err, file=sys.stderr)
        return 1

    dump(program)

    if parser.errors:
        print(f"\n{len(parser.errors)} syntax error(s):", file=sys.stderr)
        for err in parser.errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    if args.path is None:       # only show error demos for the built-in run
        print("\n--- Error demonstrations ---")
        for snippet in BAD_SNIPPETS:
            try:
                Parser.from_source(snippet).parse()
                print(f"(no error?)  {snippet!r}")
            except (LexerError, ParseError) as err:
                print(f"{snippet!r:<34} -> {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
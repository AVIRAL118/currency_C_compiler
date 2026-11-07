"""
semantic.py - Phase 3 (part 2): Semantic Analyzer for the Currency-C language.

Walks the Phase 2 AST (parser.py), fills the SymbolTable, infers the Type of
every expression and reports errors as:

    Semantic Error - Line N: <message>

Usage:
    python semantic.py              # analyze the built-in sample
    python semantic.py program.cur  # analyze a file
"""

import sys

from parser import (Program, StructDef, Declaration, Block, If, While, Input, Output,
                    Return, ExprStmt, Assignment, BinaryOp, UnaryOp, IntLiteral,
                    FloatLiteral, CurrencyLiteral, Identifier, MemberAccess, Index,
                    FileCall)
from symbol_table import (Type, Symbol, SymbolTable, RedeclarationError,
                          CURRENCY_SYMBOLS)

ERROR_TYPE = Type("error")          # poison type: suppresses cascading errors
INT = Type("int")
FLOAT = Type("float")
FILE_PTR = Type("FILE", pointer_level=1)

ARITH_VERB = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide"}
COMPARE_OPS = ("<", ">", "<=", ">=", "==", "!=")
LOGICAL_OPS = ("&&", "||")

# Built-in file functions -> expected argument count.
# Extend with the FILE_FUNCTION names from your lexer.
FILE_FUNCS = {"fopen": 2, "fclose": 1}


class Diagnostic:
    def __init__(self, severity, line, message):
        self.severity, self.line, self.message = severity, line, message

    def __str__(self):
        return f"Semantic {self.severity} - Line {self.line}: {self.message}"


class SemanticAnalyzer:
    def __init__(self):
        self.table = SymbolTable()
        self.diagnostics = []
        self._expr = {
            Identifier: self.expr_identifier, MemberAccess: self.expr_member,
            Index: self.expr_index, BinaryOp: self.expr_binary,
            UnaryOp: self.expr_unary, IntLiteral: lambda n: INT,
            FloatLiteral: lambda n: FLOAT, CurrencyLiteral: self.expr_currency,
            FileCall: self.expr_filecall,
        }

    # ------------------------------------------------------------ reporting
    def error(self, line, msg):
        self.diagnostics.append(Diagnostic("Error", line, msg))
        return ERROR_TYPE

    def warn(self, line, msg):
        self.diagnostics.append(Diagnostic("Warning", line, msg))

    @property
    def errors(self):
        return [d for d in self.diagnostics if d.severity == "Error"]

    def report(self):
        for d in sorted(self.diagnostics, key=lambda d: d.line):
            print(d)
        n = len(self.errors)
        print(f"\n{n} semantic error(s), {len(self.diagnostics) - n} warning(s).")
        return n

    def analyze(self, program):
        self.visit(program)
        return self.errors

    # ------------------------------------------------------------ statements
    def visit(self, node):
        if isinstance(node, Program):
            for item in node.items:
                self.visit(item)
        elif isinstance(node, StructDef):
            self.visit_struct(node)
        elif isinstance(node, Declaration):
            self.visit_declaration(node)
        elif isinstance(node, Block):
            self.table.enter_scope()
            for s in node.statements:
                self.visit(s)
            self.table.exit_scope()
        elif isinstance(node, If):
            self.check_condition(node.condition)
            self.visit(node.then_branch)
            if node.else_branch is not None:
                self.visit(node.else_branch)
        elif isinstance(node, While):
            self.check_condition(node.condition)
            self.visit(node.body)
        elif isinstance(node, Assignment):
            self.visit_assignment(node)
        elif isinstance(node, Input):
            t = self.visit_expression(node.target)
            if not (t.is_error or t.is_numeric or t.is_currency):
                self.error(node.line_number, f"input() cannot read into a value of type {t}")
        elif isinstance(node, Output):
            t = self.visit_expression(node.value)
            if not t.is_error and (t.is_struct or t.base_type == "FILE"):
                self.error(node.line_number, f"output() cannot print a value of type {t}")
        elif isinstance(node, Return):
            if node.value is not None:
                self.visit_expression(node.value)
        elif isinstance(node, ExprStmt):
            self.visit_expression(node.expr)

    def check_condition(self, cond):
        t = self.visit_expression(cond)
        if not (t.is_error or t.is_numeric or t.is_pointer or t.is_currency):
            self.error(cond.line_number, f"Condition must be a scalar value, got {t}")

    # ------------------------------------------------------------ declarations
    def base_type(self, spec):
        """TypeSpec -> Type (pointer_level 0). Verifies that a struct type exists."""
        if spec.kind == "struct":
            if self.table.lookup_struct(spec.name) is None:
                return self.error(spec.line_number, f"Unknown struct '{spec.name}'")
            return Type("struct", struct_name=spec.name)
        if spec.kind == "currency":
            return Type("currency", currency_code=spec.currency_code)
        return Type(spec.kind)

    def declarator_type(self, base, d):
        """Apply '*' levels and array size. An array decays to a pointer to its element."""
        if base.is_error:
            return ERROR_TYPE
        level = d.pointer_level
        if d.array_size is not None:
            if d.array_size <= 0:
                self.error(d.line_number, f"Array '{d.name}' must have a size greater than 0")
            level += 1
        return Type(base.base_type, base.currency_code, base.struct_name, level)

    def visit_declaration(self, node):
        base = self.base_type(node.type_spec)
        for d in node.declarators:
            t = self.declarator_type(base, d)
            try:
                self.table.declare(d.name, t, d.line_number)
            except RedeclarationError as e:
                self.error(d.line_number, f"Redeclaration of '{d.name}' "
                                          f"(previously declared at line {e.previous.line_number})")
            if d.init is not None:
                it = self.visit_expression(d.init)
                if d.array_size is not None:
                    self.error(d.line_number, f"Array initializers are not supported ('{d.name}')")
                else:
                    self.check_assign(t, it, d.line_number, d.init)

    def visit_struct(self, node):
        members = {}
        try:
            self.table.declare_struct(node.name, members, node.line_number)  # first: allows self-pointers
        except RedeclarationError as e:
            self.error(node.line_number, f"Redefinition of struct '{node.name}' "
                                         f"(previously defined at line {e.previous.line_number})")
        for decl in node.members:
            base = self.base_type(decl.type_spec)
            for d in decl.declarators:
                t = self.declarator_type(base, d)
                if d.init is not None:
                    self.error(d.line_number, f"Struct member '{d.name}' cannot have an initializer")
                if t.is_struct and t.struct_name == node.name:
                    self.error(d.line_number, f"struct {node.name} cannot contain itself; use a pointer")
                if d.name in members:
                    self.error(d.line_number, f"Duplicate member '{d.name}' in struct '{node.name}'")
                else:
                    members[d.name] = Symbol(d.name, t, d.line_number)

    # ------------------------------------------------------------ assignment
    def visit_assignment(self, node):
        lt = self.visit_expression(node.target)
        rt = self.visit_expression(node.value)
        self.check_assign(lt, rt, node.line_number, node.value)

    def check_assign(self, dst, src, line, rhs=None):
        if dst.is_error or src.is_error or dst == src:
            return
        if dst.is_pointer and isinstance(rhs, IntLiteral) and rhs.value == 0:
            return                                           # null pointer
        if dst.is_currency:
            if src.is_currency:
                self.error(line, f"Cannot assign {src} to {dst}: currency codes differ")
            elif src.is_numeric:
                self.error(line, f"Cannot assign {src} to {dst} without an explicit "
                                 f"conversion or currency literal")
            else:
                self.error(line, f"Cannot assign {src} to {dst}")
        elif src.is_currency and dst.is_numeric:
            self.error(line, f"Cannot assign {src} to {dst}: currency values are not plain numbers")
        elif dst.is_numeric and src.is_numeric:
            if src.is_float and not dst.is_float:
                self.warn(line, f"Implicit conversion from {src} to {dst} may lose data")
        elif dst.is_pointer and src.is_pointer:
            self.error(line, f"Incompatible pointer types: cannot assign {src} to {dst}")
        else:
            self.error(line, f"Incompatible types: cannot assign {src} to {dst}")

    # ------------------------------------------------------------ expressions
    def visit_expression(self, node):
        """Returns the Type of the expression (ERROR_TYPE after a reported error)."""
        handler = self._expr.get(type(node))
        if handler is None:
            self.warn(getattr(node, "line_number", 0),
                      f"Unhandled expression node '{type(node).__name__}'")
            return ERROR_TYPE
        return handler(node)

    def expr_identifier(self, node):
        sym = self.table.lookup(node.name)
        if sym is None:
            return self.error(node.line_number, f"Undeclared identifier '{node.name}'")
        return sym.type

    def expr_currency(self, node):
        code = CURRENCY_SYMBOLS.get(node.symbol)
        if code is None:
            return self.error(node.line_number, f"Unknown currency symbol '{node.symbol}'")
        return Type("currency", currency_code=code)

    def expr_member(self, node):
        line = node.line_number
        op = "->" if node.arrow else "."
        t = self.visit_expression(node.obj)
        if t.is_error:
            return ERROR_TYPE
        if t.base_type != "struct":
            return self.error(line, f"Cannot use '{op}' on non-struct type {t}")
        if not node.arrow and t.pointer_level != 0:
            return self.error(line, f"'.' applied to pointer type {t}; use '->'")
        if node.arrow and t.pointer_level != 1:
            return self.error(line, f"'->' requires a pointer to struct, got {t}; use '.'")
        sym = self.table.lookup_member(t.struct_name, node.member)
        if sym is None:
            return self.error(line, f"struct {t.struct_name} has no member '{node.member}'")
        return sym.type

    def expr_index(self, node):
        line = node.line_number
        t = self.visit_expression(node.obj)
        it = self.visit_expression(node.index)
        if not it.is_error and not (it.is_numeric and not it.is_float):
            self.error(line, f"Array index must be an integer, got {it}")
        if t.is_error:
            return ERROR_TYPE
        if not t.is_pointer:
            return self.error(line, f"Cannot index non-array type {t}")
        return t.deref()

    def expr_unary(self, node):
        line = node.line_number
        t = self.visit_expression(node.operand)
        if t.is_error:
            return ERROR_TYPE
        if node.op == "&":
            o = node.operand
            if not (isinstance(o, (Identifier, MemberAccess, Index))
                    or (isinstance(o, UnaryOp) and o.op == "*")):
                return self.error(line, "Cannot take the address of a non-lvalue expression")
            return t.pointer_to()
        if node.op == "*":
            if t.pointer_level == 0:
                return self.error(line, f"Cannot dereference non-pointer type {t}")
            return t.deref()
        if node.op == "-":
            if t.is_numeric or t.is_currency:
                return t
            return self.error(line, f"Operator '-' cannot be applied to {t}")
        return self.error(line, f"Unknown unary operator '{node.op}'")

    def expr_filecall(self, node):
        line = node.line_number
        args = [self.visit_expression(a) for a in node.args]
        want = FILE_FUNCS.get(node.name)
        if want is not None and len(args) != want:
            self.error(line, f"{node.name}() expects {want} argument(s), got {len(args)}")
        elif node.name == "fclose" and not args[0].is_error and args[0] != FILE_PTR:
            self.error(line, f"fclose() expects FILE*, got {args[0]}")
        return FILE_PTR if node.name == "fopen" else INT

    # ------------------------------------------------------------ binary rules
    def expr_binary(self, node):
        lt = self.visit_expression(node.left)
        rt = self.visit_expression(node.right)
        if node.op == "/" and isinstance(node.right, IntLiteral) and node.right.value == 0:
            self.error(node.line_number, "Division by zero")
        return self.binary_result(node.op, lt, rt, node.line_number)

    def binary_result(self, op, lt, rt, line):
        if lt.is_error or rt.is_error:
            return ERROR_TYPE
        scalar = lambda t: t.is_numeric or t.is_currency or t.is_pointer

        if op in LOGICAL_OPS:
            for t in (lt, rt):
                if not scalar(t):
                    return self.error(line, f"Operator '{op}' cannot be applied to {t}")
            return INT

        if op in COMPARE_OPS:
            if lt.is_currency and rt.is_currency:
                if lt.currency_code != rt.currency_code:
                    return self.error(line, f"Cannot compare {lt} and {rt}")
                return INT
            if (lt.is_currency and rt.is_numeric) or (lt.is_numeric and rt.is_currency):
                self.warn(line, f"Comparing {lt} with {rt}: plain number treated as currency")
                return INT
            if scalar(lt) and scalar(rt) and not (lt.is_currency or rt.is_currency):
                return INT
            return self.error(line, f"Cannot compare {lt} and {rt}")

        verb = ARITH_VERB[op]
        lc, rc = lt.is_currency, rt.is_currency

        # ---- currency rules
        if lc or rc:
            cur = lt if lc else rt
            other = rt if lc else lt
            if op in ("+", "-"):
                if lc and rc:
                    if lt.currency_code != rt.currency_code:
                        if op == "-":
                            return self.error(line, f"Cannot subtract {rt} from {lt}")
                        return self.error(line, f"Cannot add {lt} and {rt}")
                    return lt                                   # cur<X> +/- cur<X> -> cur<X>
                if other.is_numeric:
                    self.warn(line, f"Mixing {lt} with {rt} in '{op}': plain number treated as {cur}")
                    return cur
            elif op == "*":
                if lc and rc:
                    return self.error(line, f"Cannot multiply two currency values ({lt} * {rt})")
                if other.is_numeric:
                    return cur                                  # cur * int/float -> cur
            elif op == "/":
                if lc and rc:
                    if lt.currency_code != rt.currency_code:
                        return self.error(line, f"Cannot divide {lt} by {rt}")
                    return FLOAT                                # cur / cur -> float
                if lc and rt.is_numeric:
                    return lt                                   # cur / int/float -> cur
            return self.error(line, f"Cannot {verb} {lt} and {rt}")

        # ---- pointer arithmetic
        if lt.is_pointer or rt.is_pointer:
            if op == "+" and lt.is_pointer and rt.is_numeric and not rt.is_float:
                return lt
            if op == "+" and rt.is_pointer and lt.is_numeric and not lt.is_float:
                return rt
            if op == "-" and lt.is_pointer and rt.is_numeric and not rt.is_float:
                return lt
            if op == "-" and lt.is_pointer and lt == rt:
                return INT
            return self.error(line, f"Cannot {verb} {lt} and {rt}")

        # ---- plain numbers
        if lt.is_numeric and rt.is_numeric:
            return FLOAT if (lt.is_float or rt.is_float) else INT
        return self.error(line, f"Cannot {verb} {lt} and {rt}")


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
SAMPLE = """\
struct Account {
    currency<INR> balance;
    int id;
};
currency<INR> price = ₹500;
currency<USD> fee = $20;
currency<INR> total;
struct Account acc;
struct Account *p = &acc;
p->balance = price * 2;
total = price + fee;
total = 99.5;
acc.missing = price;
output(total);
"""


def main():
    from lexer import Lexer, LexerError
    from parser import Parser, ParseError

    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as fh:
            source = fh.read()
        print(f"Analyzing file: {sys.argv[1]}\n")
    else:
        source = SAMPLE
        print("=== Source ===")
        for i, text in enumerate(source.splitlines(), 1):
            print(f"{i:>2} | {text}")
        print()

    try:
        program = Parser.from_source(source).parse()
    except (LexerError, ParseError) as err:
        print(err, file=sys.stderr)
        sys.exit(1)

    print("=== Semantic Analysis ===")
    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)
    sys.exit(1 if analyzer.report() else 0)


if __name__ == "__main__":
    main()
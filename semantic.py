"""
semantic.py - Phase 3 (part 2): Semantic Analyzer for the Currency-C language.

Walks the Phase 2 AST (parser.py), fills the SymbolTable, infers the Type of
every expression and reports diagnostics as:

    Semantic Error - Line N: <message>
    Semantic Warning - Line N: <message>

Usage:
    python semantic.py              # analyze the built-in sample
    python semantic.py program.cur  # analyze a file
    python semantic.py program.cur --tables   # also print the symbol and structure tables
    python semantic.py program.cur --types    # also print the type inferred for every expression
"""

import argparse
import re
import sys

from lexer import LexerError
from parser import (Parser, ParseError, Program, StructDef, Declaration, Block, If,
                    While, Input, Output, Return, ExprStmt, Assignment, BinaryOp,
                    UnaryOp, IntLiteral, FloatLiteral, StringLiteral, CurrencyLiteral,
                    Identifier, MemberAccess, Index, FileCall)
from symbol_table import (Type, Symbol, SymbolTable, RedeclarationError,
                          CURRENCY_SYMBOLS)

ERROR_TYPE = Type("error")          # poison type: suppresses cascading errors
INT = Type("int")
FLOAT = Type("float")
FILE_PTR = Type("FILE", pointer_level=1)
STRING = Type("string")             # type of a string literal (only valid as a file-call argument)

ARITH_VERB = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide"}
COMPARE_OPS = ("<", ">", "<=", ">=", "==", "!=")
LOGICAL_OPS = ("&&", "||")

# Built-in file functions -> (min_args, max_args); max None = variadic.
# fread/fwrite use the standard C order: (ptr, size, count, FILE*).
FILE_ARITY = {"fopen": (2, 2), "fclose": (1, 1), "fprintf": (2, None),
              "fscanf": (2, None), "fread": (4, 4), "fwrite": (4, 4)}
STREAM_ARG = {"fclose": 0, "fprintf": 0, "fscanf": 0, "fread": 3, "fwrite": 3}  # index of the FILE*
FOPEN_MODE = re.compile(r"^[rwa][+b]{0,2}$")                 # r w a r+ wb w+b ...

# One printf/scanf conversion: %[flags][*][width][.prec][length]conv
FORMAT_SPEC = re.compile(r"%([-+ #0]*)(\*?)(\d*)(?:\.(\d+))?(hh|h|ll|l|L|z|j|t)?([a-zA-Z%])")
CONV_KIND = {**dict.fromkeys("diouxXc", "int"),
             **dict.fromkeys("fFeEgGaA", "float"),     # currency is a float in the generated C
             "s": "string", "p": "pointer"}
KIND_TEXT = {"int": "an int", "float": "a float or currency",
             "string": "a string", "pointer": "a pointer"}


def _is_int_zero(node):
    return isinstance(node, IntLiteral) and node.value == 0


def _is_zero_literal(node):
    """True for the literals 0 and 0.0 (used for the division-by-zero check)."""
    if isinstance(node, FloatLiteral):
        try:
            return float(node.text) == 0.0
        except ValueError:
            return False
    return _is_int_zero(node)


def _is_scalar(t):
    return t.is_numeric or t.is_currency or t.is_pointer


class Diagnostic:
    def __init__(self, category, severity, line, message):
        self.category = category    # "Lexical" | "Syntax" | "Semantic"
        self.severity = severity    # "Error" | "Warning"
        self.line = line
        self.message = message

    def __str__(self):
        return f"{self.category} {self.severity} - Line {self.line}: {self.message}"


class SemanticAnalyzer:
    def __init__(self):
        self.table = SymbolTable()
        self.diagnostics = []
        self.declarations_count = 0
        self.expressions_analyzed_count = 0
        # id(node) -> (node, Type). Keyed by id so AST nodes need not be hashable,
        # and so re-visiting a node (CodeGen does) overwrites instead of growing.
        self.expression_types = {}

        self._stmt = {
            Program: self.visit_program, StructDef: self.visit_struct,
            Declaration: self.visit_declaration, Block: self.visit_block,
            If: self.visit_if, While: self.visit_while,
            Assignment: self.visit_assignment, Input: self.visit_input,
            Output: self.visit_output, Return: self.visit_return,
            ExprStmt: self.visit_exprstmt,
        }
        self._expr = {
            Identifier: self.expr_identifier, MemberAccess: self.expr_member,
            Index: self.expr_index, BinaryOp: self.expr_binary,
            UnaryOp: self.expr_unary, IntLiteral: lambda n: INT,
            FloatLiteral: lambda n: FLOAT, StringLiteral: lambda n: STRING,
            CurrencyLiteral: self.expr_currency,
            FileCall: self.expr_filecall,
        }

    # ------------------------------------------------------------ reporting
    def error(self, line, msg):
        self.diagnostics.append(Diagnostic("Semantic", "Error", line, msg))
        return ERROR_TYPE

    def warn(self, line, msg):
        self.diagnostics.append(Diagnostic("Semantic", "Warning", line, msg))

    @property
    def errors(self):
        return [d for d in self.diagnostics if d.severity == "Error"]

    @property
    def warnings(self):
        return [d for d in self.diagnostics if d.severity == "Warning"]

    def report(self, show_types=False):
        """Print diagnostics (by line) and a summary; returns the error count."""
        for d in sorted(self.diagnostics, key=lambda d: d.line):
            print(d)
        n_err = len(self.errors)

        print("\n=== Semantic Analysis Report ===")
        print(f"  Declarations analyzed: {self.declarations_count}")
        print(f"  Expressions analyzed:  {self.expressions_analyzed_count}")
        print(f"  Warnings:              {len(self.warnings)}")
        print(f"  Errors:                {n_err}")

        if show_types:
            print("\n=== Expression Type Information ===")
            rows = [(getattr(n, "line_number", 0), type(n).__name__, t)
                    for n, t in self.expression_types.values()]
            for line, kind, t in sorted(rows, key=lambda r: r[0]):   # key: never compare Types
                print(f"  {kind} @L{line} -> {t}")
        return n_err

    def analyze(self, program):
        self.visit(program)
        return self.errors

    # ------------------------------------------------------------ statements
    def visit(self, node):
        handler = self._stmt.get(type(node))
        if handler is not None:
            handler(node)

    def visit_program(self, node):
        for item in node.items:
            self.visit(item)

    def visit_block(self, node):
        self.table.enter_scope()
        for s in node.statements:
            self.visit(s)
        self.table.exit_scope()

    def visit_if(self, node):
        self.check_condition(node.condition)
        self.visit(node.then_branch)
        if node.else_branch is not None:
            self.visit(node.else_branch)

    def visit_while(self, node):
        self.check_condition(node.condition)
        self.visit(node.body)

    def visit_input(self, node):
        t = self.visit_expression(node.target)
        if not (t.is_error or t.is_numeric or t.is_currency):
            self.error(node.line_number, f"input() cannot read into a value of type {t}")

    def visit_output(self, node):
        t = self.visit_expression(node.value)
        if not t.is_error and (t.is_struct or t.base_type in ("FILE", "string")):
            self.error(node.line_number, f"output() cannot print a value of type {t}")

    def visit_return(self, node):
        if node.value is not None:
            self.visit_expression(node.value)

    def visit_exprstmt(self, node):
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
        self.declarations_count += len(node.declarators)
        base = self.base_type(node.type_spec)
        for d in node.declarators:
            t = self.declarator_type(base, d)
            try:
                self.table.declare(d.name, t, d.line_number, array_size=d.array_size)
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
        self.declarations_count += 1
        members = {}
        try:
            self.table.declare_struct(node.name, members, node.line_number)  # first: allows self-pointers
        except RedeclarationError as e:
            self.error(node.line_number, f"Redefinition of struct '{node.name}' "
                                         f"(previously defined at line {e.previous.line_number})")
        for decl in node.members:
            base = self.base_type(decl.type_spec)
            for d in decl.declarators:
                self.declarations_count += 1
                t = self.declarator_type(base, d)
                if d.init is not None:
                    self.error(d.line_number, f"Struct member '{d.name}' cannot have an initializer")
                if t.is_struct and t.struct_name == node.name:
                    self.error(d.line_number, f"struct {node.name} cannot contain itself; use a pointer")
                if d.name in members:
                    self.error(d.line_number, f"Duplicate member '{d.name}' in struct '{node.name}'")
                else:
                    members[d.name] = Symbol(d.name, t, d.line_number, array_size=d.array_size)

    # ------------------------------------------------------------ assignment
    def visit_assignment(self, node):
        lt = self.visit_expression(node.target)
        rt = self.visit_expression(node.value)
        self.check_assign(lt, rt, node.line_number, node.value)

    def check_assign(self, dst, src, line, rhs=None):
        if dst.is_error or src.is_error or dst == src:
            return
        if dst.is_pointer and _is_int_zero(rhs):
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
        self.expressions_analyzed_count += 1
        handler = self._expr.get(type(node))
        if handler is None:
            self.warn(getattr(node, "line_number", 0),
                      f"Unhandled expression node '{type(node).__name__}'")
            t = ERROR_TYPE
        else:
            t = handler(node)
        self.expression_types[id(node)] = (node, t)
        return t

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
        line, name = node.line_number, node.name
        args = [self.visit_expression(a) for a in node.args]

        if name in ("fileoutput", "fileinput"):           # tokenized, not implemented
            self.error(line, f"{name}() is not supported yet; use fprintf/fscanf")
            return ERROR_TYPE

        result = FILE_PTR if name == "fopen" else INT
        lo, hi = FILE_ARITY.get(name, (0, None))
        if len(args) < lo or (hi is not None and len(args) > hi):
            want = str(lo) if lo == hi else f"at least {lo}"
            self.error(line, f"{name}() expects {want} argument(s), got {len(args)}")
            return result

        # -- hard rule: the stream must be a FILE*
        pos = STREAM_ARG.get(name)
        if pos is not None and not args[pos].is_error and args[pos] != FILE_PTR:
            self.error(line, f"{name}() expects FILE* as argument {pos + 1}, got {args[pos]}")

        if name == "fopen":
            for i, t in enumerate(args, 1):
                if not t.is_error and t.base_type != "string":
                    self.error(line, f"fopen() expects a string literal as argument {i}, got {t}")
            mode = node.args[1]
            if isinstance(mode, StringLiteral) and not FOPEN_MODE.match(mode.value[1:-1]):
                self.warn(line, f"fopen(): unusual mode {mode.value}")

        elif name in ("fprintf", "fscanf"):
            fmt = node.args[1]
            rest = args[2:]
            if not isinstance(fmt, StringLiteral):
                if not args[1].is_error:
                    self.error(line, f"{name}() format must be a string literal")
            else:
                scanning = name == "fscanf"
                ok = True
                if scanning:                              # scanf needs addresses
                    for i, t in enumerate(rest, 1):
                        if not t.is_error and not t.is_pointer:
                            ok = False
                            self.error(line, f"fscanf() argument {i} after the format must be "
                                             f"an address (use &), got {t}")
                if ok:
                    self.check_format(name, line, fmt.value[1:-1], rest, scanning)

        elif name in ("fread", "fwrite"):
            if not args[0].is_error and not args[0].is_pointer:
                self.error(line, f"{name}() argument 1 must be a pointer or array, got {args[0]}")
            for i in (1, 2):
                t = args[i]
                if not t.is_error and not (t.is_numeric and not t.is_float):
                    self.error(line, f"{name}() argument {i + 1} must be an integer, got {t}")
        return result

    # ------------------------------------------------ format-string validation
    @staticmethod
    def parse_format(fmt):
        """Return ([(conv, length_modifier)], has_star, [problem text])."""
        convs, problems, star = [], [], False
        i = 0
        while True:
            i = fmt.find("%", i)
            if i < 0:
                break
            m = FORMAT_SPEC.match(fmt, i)
            if not m:
                problems.append("incomplete or invalid conversion in format string")
                i += 1
                continue
            i = m.end()
            _, has_star, _, _, length, conv = m.groups()
            if conv == "%":
                continue                                  # literal percent sign
            if has_star:
                star = True                               # '*' changes the argument layout
            if conv not in CONV_KIND and conv != "n":
                problems.append(f"unknown conversion '%{conv}'")
                continue
            if conv != "n":
                convs.append((conv, length))
        return convs, star, problems

    @staticmethod
    def _value_kind(value):
        """Classify a Type into the conversion kinds used by CONV_KIND."""
        if value.is_pointer:
            return "pointer"
        if value.is_float or value.is_currency:
            return "float"
        if value.is_numeric:
            return "int"
        if value.base_type == "string":
            return "string"
        return "other"

    def check_format(self, name, line, fmt, arg_types, scanning):
        """Basic format-specifier validation. Warnings only: real C allows far more
        than we model, so a doubtful format must never reject a valid program."""
        convs, star, problems = self.parse_format(fmt)
        for p in problems:
            self.warn(line, f"{name}(): {p}")
        if star:
            return
        if len(arg_types) < len(convs):
            self.warn(line, f"{name}() format expects {len(convs)} argument(s) after it, "
                            f"but {len(arg_types)} given")
        elif len(arg_types) > len(convs):
            self.warn(line, f"{name}(): {len(arg_types) - len(convs)} extra argument(s) "
                            f"not used by the format")
        for i, ((conv, length), t) in enumerate(zip(convs, arg_types), 1):
            if t.is_error or length:                      # %ld, %lf, ...: not modelled, skip
                continue
            value = t.deref() if scanning else t          # scanf: check what the pointer points to
            kind, want = self._value_kind(value), CONV_KIND[conv]
            if kind != want:
                what = f"points to {value}" if scanning else f"is {t}"
                self.warn(line, f"{name}(): argument {i} for '%{conv}' {what} "
                                f"(expects {KIND_TEXT[want]})")

    # ------------------------------------------------------------ binary rules
    def expr_binary(self, node):
        lt = self.visit_expression(node.left)
        rt = self.visit_expression(node.right)
        if node.op == "/" and _is_zero_literal(node.right):
            self.error(node.line_number, "Division by zero")
        return self.binary_result(node.op, lt, rt, node.line_number)

    def binary_result(self, op, lt, rt, line):
        if lt.is_error or rt.is_error:
            return ERROR_TYPE

        if op in LOGICAL_OPS:
            for t in (lt, rt):
                if not _is_scalar(t):
                    return self.error(line, f"Operator '{op}' cannot be applied to {t}")
            return INT

        if op in COMPARE_OPS:
            if lt.is_currency and rt.is_currency:
                if lt.currency_code != rt.currency_code:
                    return self.error(line, f"Cannot compare {lt} and {rt}")
                return INT
            if _is_scalar(lt) and _is_scalar(rt) and not (lt.is_currency or rt.is_currency):
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
                # currency +/- int/float is NOT allowed: falls through to the error below
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Currency-C semantic analyzer")
    ap.add_argument("path", nargs="?", help="Currency-C source file (default: built-in sample)")
    ap.add_argument("--tables", action="store_true",
                    help="also print the symbol and structure tables")
    ap.add_argument("--types", action="store_true",
                    help="also print the type inferred for every expression")
    args = ap.parse_args(argv)

    if args.path:
        try:
            with open(args.path, encoding="utf-8") as fh:
                source = fh.read()
        except OSError as err:
            print(err, file=sys.stderr)
            return 1
        print(f"Analyzing file: {args.path}\n")
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
        return 1

    print("=== Semantic Analysis ===")
    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)
    n_errors = analyzer.report(show_types=args.types)
    if args.tables:
        print("\n" + analyzer.table.symbol_table_text())
        print("\n" + analyzer.table.structure_table_text())
    return 1 if n_errors else 0


if __name__ == "__main__":
    sys.exit(main())
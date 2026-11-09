"""
codegen.py - Phase 4: Target C Code Generation for the Currency-C (.cur) language.

Walks the validated AST (parser.py) with a Visitor and emits standard C.

Translation rules
-----------------
  types      currency<XXX>  -> float          (pointers keep their '*': float *p)
             int / float / FILE / struct X    -> unchanged
  literals   currency<INR> x = ₹500;          -> float x = 500.00f;
  output(e)  int expression                   -> printf("%d\\n", e);
             float expression                 -> printf("%.2f\\n", e);
             currency<XXX> expression         -> printf("₹%.2f\\n", e);   (symbol of its code)
             pointer expression               -> printf("%p\\n", (void *)e);
  input(t)   int target                       -> scanf("%d", &t);
             float / currency target          -> scanf("%f", &t);
  file ops   fopen fclose fprintf fscanf fread fwrite -> passed through unchanged

Program layout
--------------
A Currency-C program is a flat list of struct definitions and statements, with no
function definitions, so the generator produces:

    #include <stdio.h>
    struct definitions            (hoisted to file scope, in source order)
    int main(void) {
        ...every other top-level statement...
        return 0;
    }

Everything else goes inside main() because C does not allow statements (or
non-constant initializers) at file scope.

Type information
----------------
The semantic analyzer returns expression types but does not annotate the AST, and
output()/input() need those types to choose a printf/scanf format. CodeGen
therefore drives an internal SemanticAnalyzer in lock-step (declarations and
scopes), purely as a type oracle. Its diagnostics are ignored; run the analyzer
yourself first (see compile_source) and only generate code when it reports no errors.

Usage:
    python codegen.py                  # translate sample.cur (or a built-in sample)
    python codegen.py program.cur      # translate a file, writes output.c
    python codegen.py prog.cur -o x.c  # choose the output file name
    python codegen.py --plain          # plain printf("%.2f\\n", x) output, no labels/prompts
    python codegen.py --currency-output code   # print 'INR 12.50' instead of '₹12.50' (or: none)
    python codegen.py prog.cur --dump-ast   # print the validated AST and exit (no output.c)
    python codegen.py -                # read the Currency-C source from stdin

    from codegen import CodeGen
    c_source = CodeGen().generate(ast, "output.c")
"""

from __future__ import annotations

import argparse
import os
import sys

from lexer import Lexer, LexerError
from parser import (Parser, ParseError, StructDef, Block, If, Output, Return,
                    BinaryOp, UnaryOp, IntLiteral, Identifier, MemberAccess,
                    Index, FileCall, dump)
from semantic import SemanticAnalyzer
from symbol_table import CURRENCY_SYMBOLS


class CodeGenError(Exception):
    """Raised when the AST cannot be translated (normally a skipped semantic check)."""

    def __init__(self, message, line_number=0):
        super().__init__(f"Code generation error at line {line_number}: {message}")
        self.line_number = line_number


# Binary operator precedence (higher binds tighter), mirroring C and the parser.
BINARY_PREC = {"||": 1, "&&": 2, "==": 3, "!=": 3,
               "<": 4, ">": 4, "<=": 4, ">=": 4,
               "+": 5, "-": 5, "*": 6, "/": 6}
UNARY_PREC = 7
POSTFIX_PREC = 8
ATOM_PREC = 9

# How output() of a currency value is prefixed: its symbol (₹12.50), its code (INR 12.50), or nothing.
CURRENCY_OUTPUT_MODES = ("symbol", "code", "none")
CODE_TO_SYMBOL = {code: sym for sym, code in CURRENCY_SYMBOLS.items()}

# Unary operators that must not be glued to an identical leading character
# ('--x' would be a decrement, '&&x' a logical-and token).
_SELF_CLASHING_UNARY = ("-", "&")


def _c_escape(text):
    """Escape `text` for use inside a C string literal that is also a printf format."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


class CodeGen:
    INDENT = "    "

    def __init__(self, labels=True, program_name: str | None = None,
                 currency_output="symbol"):
        # labels=True : output(x) prints "x = 12.50" and input(x) shows a prompt
        # labels=False: plain format, printf("%.2f\\n", x) with no banner or prompts
        # currency_output: 'symbol' (₹12.50) | 'code' (INR 12.50) | 'none' (12.50)
        if currency_output not in CURRENCY_OUTPUT_MODES:
            raise ValueError(f"currency_output must be one of {CURRENCY_OUTPUT_MODES}")
        self.labels = labels
        self.currency_output = currency_output
        self.program_name = program_name
        self._header_name = program_name
        self._reset()

    def _reset(self):
        self.lines = []
        self.level = 0
        self.ctrl_depth = 0                     # nesting of while/if bodies (output indent)
        self.analyzer = SemanticAnalyzer()      # type oracle (see module docstring)

    # ------------------------------------------------------------------ API
    def translate(self, program, program_name: str | None = None):
        """AST -> C source text (does not touch the filesystem)."""
        self._reset()
        self._header_name = program_name if program_name is not None else self.program_name
        self.visit(program)
        return "\n".join(self.lines) + "\n"

    def generate(self, program_ast, filename="output.c", program_name: str | None = None):
        """Translate the AST, write it to `filename`, and return the C source."""
        code = self.translate(program_ast, program_name)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(code)
        return code

    # -------------------------------------------------------------- helpers
    def emit(self, text=""):
        self.lines.append(f"{self.INDENT * self.level}{text}" if text else "")

    def visit(self, node):
        """Statement-level visitor dispatch: visit_<NodeClass>."""
        method = getattr(self, f"visit_{type(node).__name__}", None)
        if method is None:
            raise CodeGenError(f"no rule for statement '{type(node).__name__}'",
                               getattr(node, "line_number", 0))
        return method(node)

    def _type_of(self, node):
        t = self.analyzer.visit_expression(node)
        if t.is_error:
            raise CodeGenError("expression has no valid type (run the semantic "
                               "analyzer first)", node.line_number)
        return t

    @property
    def _scopes(self):
        return self.analyzer.table

    def _emit_body(self, stmt):
        """Emit `stmt` as the contents of an already-opened '{'.
        Non-block statements are wrapped, which avoids dangling-else surprises."""
        self.level += 1
        self._scopes.enter_scope()
        for s in (stmt.statements if isinstance(stmt, Block) else [stmt]):
            self.visit(s)
        self._scopes.exit_scope()
        self.level -= 1

    def _emit_branch(self, stmt):
        """Emit a control-flow body one output-indent level deeper."""
        self.ctrl_depth += 1
        self._emit_body(stmt)
        self.ctrl_depth -= 1

    # ------------------------------------------------------------ top level
    def visit_Program(self, node):
        structs = [i for i in node.items if isinstance(i, StructDef)]
        body = [i for i in node.items if not isinstance(i, StructDef)]

        if self._header_name:
            safe = self._header_name.replace("*/", "* /")     # keep the comment closed
            self.emit(f"/* Generated from {safe} */")
        else:
            self.emit("/* Generated by the Currency-C compiler (Phase 4) */")
        self.emit("#include <stdio.h>")
        self.emit()
        for s in structs:
            self.visit(s)
            self.emit()

        self.emit("int main(void) {")
        self.level += 1
        if self.labels:                         # banner only in the labelled mode
            self.emit('printf("── Currency-C run ──\\n");')
        prev = None
        for item in body:
            self.visit(item)
            if self.labels and isinstance(item, Output) and isinstance(prev, Output):
                self.emit('printf("\\n");')          # runtime blank line (labelled mode only)
            prev = item
        if not isinstance(prev, Return):
            self.emit("return 0;")
        self.level -= 1
        self.emit("}")

    def visit_StructDef(self, node):
        self.analyzer.visit(node)               # register the struct for type lookups
        self.emit(f"struct {node.name} {{")
        self.level += 1
        for member in node.members:
            self.emit(self._declaration_text(member))
        self.level -= 1
        self.emit("};")

    # --------------------------------------------------------- declarations
    @staticmethod
    def _type_name(spec):
        if spec.kind == "currency":
            return "float"                      # currency<INR/USD/EUR/GBP> -> float
        if spec.kind == "struct":
            return f"struct {spec.name}"
        return spec.kind                        # int | float | FILE

    def _declarator_text(self, d):
        text = "*" * d.pointer_level + d.name
        if d.array_size is not None:
            text += f"[{d.array_size}]"
        if d.init is not None:
            text += f" = {self.expr(d.init)}"
        return text

    def _declaration_text(self, node):
        decls = ", ".join(self._declarator_text(d) for d in node.declarators)
        return f"{self._type_name(node.type_spec)} {decls};"

    def visit_Declaration(self, node):
        self.analyzer.visit(node)               # declare names in the type oracle
        self.emit(self._declaration_text(node))

    # ----------------------------------------------------------- statements
    def visit_Block(self, node):
        self.emit("{")
        self._emit_body(node)
        self.emit("}")

    def visit_If(self, node):
        self.emit(f"if ({self.expr(node.condition)}) {{")
        self._emit_branch(node.then_branch)
        other = node.else_branch
        while isinstance(other, If):            # flatten 'else if' chains
            self.emit(f"}} else if ({self.expr(other.condition)}) {{")
            self._emit_branch(other.then_branch)
            other = other.else_branch
        if other is not None:
            self.emit("} else {")
            self._emit_branch(other)
        self.emit("}")

    def visit_While(self, node):
        self.emit(f"while ({self.expr(node.condition)}) {{")
        self._emit_branch(node.body)
        self.emit("}")

    def visit_Assignment(self, node):
        self.emit(f"{self.expr(node.target)} = {self.expr(node.value)};")

    def visit_ExprStmt(self, node):
        self.emit(f"{self.expr(node.expr)};")

    def visit_Return(self, node):
        # Top-level code lives in main(), which must return an int.
        value = self.expr(node.value) if node.value is not None else "0"
        self.emit(f"return {value};")

    def _currency_prefix(self, t):
        """Text printed before a currency value: '₹', 'INR ' or ''."""
        if not t.is_currency:
            return ""
        if self.currency_output == "symbol":
            return CODE_TO_SYMBOL.get(t.currency_code, "")
        if self.currency_output == "code":
            return f"{t.currency_code} "
        return ""

    def visit_Output(self, node):
        t = self._type_of(node.value)
        if t.is_pointer:
            fmt, arg = "%p", f"(void *){self.expr(node.value, UNARY_PREC)}"
        elif t.is_currency or t.is_float:
            fmt, arg = f"{self._currency_prefix(t)}%.2f", self.expr(node.value)
        elif t.is_numeric:                      # int
            fmt, arg = "%d", self.expr(node.value)
        else:
            raise CodeGenError(f"cannot output a value of type {t}", node.line_number)

        label = self._lvalue_label(node.value) if self.labels else None
        if label is not None:                   # e.g. printf("  count = %d\n", count);
            label = _c_escape(" " * self.ctrl_depth + label)
            self.emit(f'printf("{label} = {fmt}\\n", {arg});')
        else:                                   # not a plain lvalue: unlabelled form
            self.emit(f'printf("{fmt}\\n", {arg});')

    def _lvalue_label(self, node):
        """Readable name of an lvalue for prompts, e.g. 'count', 'acc.balance',
        'accPtr->balance', 'values[i - 1]'. Returns None unless `node` is an
        Identifier, or a MemberAccess / Index chain rooted at an Identifier."""
        if isinstance(node, Identifier):
            return node.name
        if isinstance(node, MemberAccess):
            base = self._lvalue_label(node.obj)
            if base is None:
                return None
            return f"{base}{'->' if node.arrow else '.'}{node.member}"
        if isinstance(node, Index):
            base = self._lvalue_label(node.obj)
            if base is None:
                return None
            return f"{base}[{self.expr(node.index)}]"
        return None

    def visit_Input(self, node):
        t = self._type_of(node.target)
        if t.is_currency or t.is_float:
            fmt = "%f"
        elif t.is_numeric:
            fmt = "%d"
        else:
            raise CodeGenError(f"cannot input into a value of type {t}", node.line_number)

        label = self._lvalue_label(node.target) if self.labels else None
        if label is not None:                   # e.g. printf("acc.balance: ");
            self.emit(f'printf("{_c_escape(label)}: ");')
            self.emit("fflush(stdout);")
        self.emit(f'scanf("{fmt}", &{self.expr(node.target, POSTFIX_PREC)});')

    # ---------------------------------------------------------- expressions
    @staticmethod
    def _prec(node):
        if isinstance(node, BinaryOp):
            return BINARY_PREC[node.op]
        if isinstance(node, UnaryOp):
            return UNARY_PREC
        if isinstance(node, (MemberAccess, Index, FileCall)):
            return POSTFIX_PREC
        return ATOM_PREC

    def expr(self, node, min_prec=0):
        """Expression visitor: returns C text, parenthesised only when the
        surrounding context binds tighter than the node itself (the parser
        discards source parentheses, so they must be rebuilt from the tree)."""
        method = getattr(self, f"expr_{type(node).__name__}", None)
        if method is None:
            raise CodeGenError(f"no rule for expression '{type(node).__name__}'",
                               getattr(node, "line_number", 0))
        text = method(node)
        return f"({text})" if self._prec(node) < min_prec else text

    def expr_IntLiteral(self, node):
        return str(node.value)

    def expr_FloatLiteral(self, node):
        return node.text

    def expr_CurrencyLiteral(self, node):
        # The symbol only carries type information; the value is the bare number,
        # with an 'f' suffix so it is a float constant like the variable it initialises.
        amount = node.amount
        if isinstance(amount, IntLiteral):
            return f"{amount.value}.00f"        # ₹500 -> 500.00f
        return f"{amount.text}f"                # ₹50000.00 -> 50000.00f

    def expr_StringLiteral(self, node):
        return node.value                       # already a valid C literal, quotes included

    def expr_Identifier(self, node):
        return node.name

    def expr_BinaryOp(self, node):
        p = BINARY_PREC[node.op]
        left = self.expr(node.left, p)          # left-associative:
        right = self.expr(node.right, p + 1)    # equal precedence on the right needs ()
        return f"{left} {node.op} {right}"

    def expr_UnaryOp(self, node):
        operand = self.expr(node.operand, UNARY_PREC)
        if node.op in _SELF_CLASHING_UNARY and operand.startswith(node.op):
            operand = f"({operand})"            # never emit '--x' or '&&x'
        return f"{node.op}{operand}"

    def expr_MemberAccess(self, node):
        return f"{self.expr(node.obj, POSTFIX_PREC)}{'->' if node.arrow else '.'}{node.member}"

    def expr_Index(self, node):
        return f"{self.expr(node.obj, POSTFIX_PREC)}[{self.expr(node.index)}]"

    def expr_FileCall(self, node):
        args = ", ".join(self.expr(a) for a in node.args)
        return f"{node.name}({args})"           # fopen/fclose/fprintf/... pass through


# ---------------------------------------------------------------------------
# Pipeline: Lexer -> Parser -> Semantic Analyzer -> CodeGen
# ---------------------------------------------------------------------------
FALLBACK_SAMPLE = """\
struct Account {
    int id;
    currency<INR> balance;
};

currency<INR> price = ₹500;
currency<INR> total;
int count = 3;
struct Account acc;

acc.id = 1;
acc.balance = price * 2;
total = acc.balance + ₹250.75;
output(total);
while (count > 0) {
    output(count);
    count = count - 1;
}
"""


def analyze_source(source):
    """Lex, parse and analyze `source`.

    Returns (program, analyzer), or None if lexing/parsing failed (the error is
    printed to stderr). Semantic diagnostics are reported but not fatal here;
    callers check `analyzer.errors`."""
    try:
        program = Parser.from_source(source).parse()
    except (LexerError, ParseError) as err:
        print(err, file=sys.stderr)
        return None

    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)
    if analyzer.diagnostics:
        analyzer.report()
    return program, analyzer


def compile_source(source, out_path="output.c", labels=True, program_name=None,
                   currency_output="symbol"):
    """Run the whole pipeline on `source`; returns the C code, or None on errors."""
    result = analyze_source(source)
    if result is None:
        return None
    program, analyzer = result
    if analyzer.errors:
        print("Semantic errors found; no code generated.", file=sys.stderr)
        return None
    return CodeGen(labels=labels, currency_output=currency_output).generate(
        program, out_path, program_name)


def read_source(path):
    """Read Currency-C source from `path` ('-' = stdin). Returns (source, display_name)."""
    if path == "-":
        return sys.stdin.buffer.read().decode("utf-8"), "<stdin>"
    return Lexer.from_file(path).source, os.path.basename(path)


def dump_ast(source):
    """Debug hook: parse and analyze `source`, print the AST tree, write nothing."""
    result = analyze_source(source)
    if result is None:
        return 1
    program, analyzer = result
    dump(program)                               # parser.dump prints the tree itself
    return 1 if analyzer.errors else 0          # diagnostics shown, tree still dumped


def main(argv=None):
    ap = argparse.ArgumentParser(description="Currency-C -> C code generator")
    ap.add_argument("path", nargs="?", help="Currency-C source file (default: sample.cur)")
    ap.add_argument("-o", "--output", default="output.c",
                    help="output C file (default: output.c)")
    ap.add_argument("--plain", action="store_true",
                    help="plain printf output: no banner, labels or input prompts")
    ap.add_argument("--currency-output", choices=CURRENCY_OUTPUT_MODES, default="symbol",
                    help="how output() prints currency values: symbol (default), code or none")
    ap.add_argument("--dump-ast", action="store_true",
                    help="print the validated AST and exit without generating C")
    args = ap.parse_args(argv)

    if args.dump_ast and not args.path:
        ap.error("--dump-ast requires a .cur file argument")

    path = args.path
    if path is None:
        default = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.cur")
        path = default if os.path.exists(default) else None

    try:
        if path is None:
            source, name, shown = FALLBACK_SAMPLE, "<built-in sample>", \
                "built-in sample (sample.cur not found)"
        else:
            source, name = read_source(path)
            shown = name if path == "-" else path
    except OSError as err:                      # an explicit bad path is an error,
        print(err, file=sys.stderr)             # not a silent switch to the sample
        return 1

    if args.dump_ast:
        return dump_ast(source)

    print(f"// Source: {shown}")
    print("// ---------------- Currency-C ----------------")
    print(source)

    code = compile_source(source, args.output, not args.plain, name, args.currency_output)
    if code is None:
        return 1

    print(f"// ---------------- Generated C (written to {args.output}) ----------------")
    print(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
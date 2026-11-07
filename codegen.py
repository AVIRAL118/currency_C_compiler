"""
codegen.py - Phase 4: Target C Code Generation for the Currency-C (.cur) language.

Walks the validated AST (parser.py) with a Visitor and emits standard C.

Translation rules
-----------------
  types      currency<XXX>  -> float          (pointers keep their '*': float *p)
             int / float / FILE / struct X    -> unchanged
  literals   currency<INR> x = ₹500;          -> float x = 500.00;
  output(e)  int expression                   -> printf("%d\\n", e);
             float / currency expression      -> printf("%.2f\\n", e);
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
yourself first (see __main__) and only generate code when it reports no errors.

Usage:
    python codegen.py                  # translate sample.cur (or a built-in sample)
    python codegen.py program.cur      # translate a file, writes output.c
    python codegen.py --plain          # plain printf("%.2f\\n", x) output, no labels/prompts
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
from parser import (Parser, ParseError, Program, StructDef, Declaration, Block, If,
                    While, Input, Output, Return, ExprStmt, Assignment, BinaryOp,
                    UnaryOp, IntLiteral, FloatLiteral, CurrencyLiteral, Identifier,
                    MemberAccess, Index, FileCall, dump)
from semantic import SemanticAnalyzer


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


class CodeGen:
    INDENT = "    "

    def __init__(self, labels=True, program_name: str | None = None):
        # labels=True : output(x) prints "x = 12.50" and input(x) shows a prompt
        # labels=False: plain format, printf("%.2f\\n", x) with no prompts
        self.labels = labels
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
        self.emit('printf("── Currency-C run ──\\n");')
        for idx, item in enumerate(body):
            self.visit(item)
            if isinstance(item, Output) and idx > 0 and isinstance(body[idx - 1], Output):
                self.emit('printf("\\n");')          # runtime blank line
        if not body or not isinstance(body[-1], Return):
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
        self.ctrl_depth += 1
        self._emit_body(node.then_branch)
        self.ctrl_depth -= 1
        other = node.else_branch
        while isinstance(other, If):            # flatten 'else if' chains
            self.emit(f"}} else if ({self.expr(other.condition)}) {{")
            self.ctrl_depth += 1
            self._emit_body(other.then_branch)
            self.ctrl_depth -= 1
            other = other.else_branch
        if other is not None:
            self.emit("} else {")
            self.ctrl_depth += 1
            self._emit_body(other)
            self.ctrl_depth -= 1
        self.emit("}")

    def visit_While(self, node):
        self.emit(f"while ({self.expr(node.condition)}) {{")
        self.ctrl_depth += 1
        self._emit_body(node.body)
        self.ctrl_depth -= 1
        self.emit("}")

    def visit_Assignment(self, node):
        self.emit(f"{self.expr(node.target)} = {self.expr(node.value)};")

    def visit_ExprStmt(self, node):
        self.emit(f"{self.expr(node.expr)};")

    def visit_Return(self, node):
        # Top-level code lives in main(), which must return an int.
        value = self.expr(node.value) if node.value is not None else "0"
        self.emit(f"return {value};")

    def visit_Output(self, node):
        t = self._type_of(node.value)
        value = self.expr(node.value)
        if t.is_pointer:
            fmt, arg = "%p", f"(void *){self.expr(node.value, UNARY_PREC)}"
        elif t.is_currency or t.is_float:
            fmt, arg = "%.2f", value
        elif t.is_numeric:                      # int
            fmt, arg = "%d", value
        else:
            raise CodeGenError(f"cannot output a value of type {t}", node.line_number)

        label = self._lvalue_label(node.value) if self.labels else None
        if label is not None:                   # e.g. printf("  count = %d\n", count);
            label = " " * self.ctrl_depth + label
            label = label.replace("\\", "\\\\").replace('"', '\\"')
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
            label = label.replace("\\", "\\\\").replace('"', '\\"')
            self.emit(f'printf("{label}: ");')
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
        # The symbol only carries type information; the value is the bare number.
        amount = node.amount
        if isinstance(amount, IntLiteral):
            return f"{amount.value}.00"         # ₹500 -> 500.00
        return amount.text                      # ₹50000.00 -> 50000.00

    def expr_Identifier(self, node):
        return node.name

    def expr_BinaryOp(self, node):
        p = BINARY_PREC[node.op]
        left = self.expr(node.left, p)          # left-associative:
        right = self.expr(node.right, p + 1)    # equal precedence on the right needs ()
        return f"{left} {node.op} {right}"

    def expr_UnaryOp(self, node):
        operand = self.expr(node.operand, UNARY_PREC)
        if node.op in "-&" and operand.startswith(node.op):
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
# Demo: Lexer -> Parser -> Semantic Analyzer -> CodeGen
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


def compile_source(source, out_path="output.c", labels=True, program_name=None):
    """Run the whole pipeline on `source`; returns the C code, or None on errors."""
    try:
        program = Parser.from_source(source).parse()
    except (LexerError, ParseError) as err:
        print(err, file=sys.stderr)
        return None

    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)
    if analyzer.diagnostics:
        analyzer.report()
    if analyzer.errors:
        print("Semantic errors found; no code generated.", file=sys.stderr)
        return None

    return CodeGen(labels=labels).generate(program, out_path, program_name)


def dump_ast(path):
    """Debug hook: lex, parse and analyze `path`, print the AST tree, write nothing."""
    try:
        source = Lexer.from_file(path).source
        program = Parser.from_source(source).parse()
    except OSError as err:
        print(err, file=sys.stderr)
        return 1
    except (LexerError, ParseError) as err:
        print(err, file=sys.stderr)
        return 1

    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)
    if analyzer.diagnostics:
        analyzer.report()                       # shown, but the tree is still dumped
    dump(program)                               # parser.dump prints the tree itself
    return 1 if analyzer.errors else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Currency-C -> C code generator")
    ap.add_argument("path", nargs="?", help="Currency-C source file (default: sample.cur)")
    ap.add_argument("--plain", action="store_true",
                    help="plain printf output: no labels or input prompts")
    ap.add_argument("--dump-ast", action="store_true",
                    help="print the validated AST and exit without generating C")
    args = ap.parse_args(argv)

    if args.dump_ast:
        if not args.path:
            ap.error("--dump-ast requires a .cur file argument")
        return dump_ast(args.path)

    labels = not args.plain
    if args.path:
        path = args.path
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.cur")

    if path == "-":
        source = sys.stdin.buffer.read().decode("utf-8")
        program_name = "<stdin>"
        print("// Source: <stdin>")
    elif os.path.exists(path):
        source = Lexer.from_file(path).source
        program_name = os.path.basename(path)
        print(f"// Source: {path}")
    else:
        source = FALLBACK_SAMPLE
        program_name = "<built-in sample>"
        print("// Source: built-in sample (sample.cur not found)")

    print("// ---------------- Currency-C ----------------")
    print(source)

    code = compile_source(source, "output.c", labels, program_name)
    if code is None:
        return 1

    print("// ---------------- Generated C (written to output.c) ----------------")
    print(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
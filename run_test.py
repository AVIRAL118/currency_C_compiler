"""
run_tests.py - end-to-end tests for the Currency-C compiler (spec section 21).

  VALID   programs must pass lexer + parser + semantic analysis, generate C,
          compile with gcc, run, and print the expected values.
  INVALID programs must be rejected with the expected diagnostic text.
  TABLES  programs are checked on the symbol / structure table rows (spec 6.2, 6.3).
  CODEGEN programs are checked on the generated C text (currency symbol rules).
  WARNING programs are accepted, but must raise the expected warning
          (used for checks that must never reject valid C, e.g. format specifiers).
  KNOWN GAPS are checks the compiler does not make yet; they are reported as
          XFAIL (expected failure) and flip to XPASS once implemented.

Usage:
    python run_tests.py            # run everything
    python run_tests.py -v         # also show diagnostics / program output
Exit code is 0 only when there are no FAIL results.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from textwrap import dedent

from lexer import LexerError
from parser import Parser, ParseError
from semantic import SemanticAnalyzer
from codegen import CodeGen

ACCOUNT = """
struct Account {
    int accountNo;
    currency<INR> balance;
    float interestRate;
};
"""

# --------------------------------------------------------------------------
# VALID programs: (name, source, stdin, substrings expected in program output)
# --------------------------------------------------------------------------
VALID = [
    ("single currency declaration",
     "currency<INR> a = ₹100;\noutput(a);", "", ["₹100.00"]),
    ("multiple currency declarations",
     "currency<INR> a = ₹1.50, b = ₹2.25, c;\nc = a + b;\noutput(c);", "", ["3.75"]),
    ("currency add / subtract",
     "currency<USD> a = $500;\ncurrency<USD> b = $250;\ncurrency<USD> c;\n"
     "c = a + b - $50;\noutput(c);", "", ["700.00"]),
    ("currency * int, int * currency, currency * float",
     "currency<EUR> a = €10;\ncurrency<EUR> b;\ncurrency<EUR> c;\ncurrency<EUR> d;\n"
     "b = a * 3;\nc = 2 * a;\nd = a * 1.5;\noutput(b);\noutput(c);\noutput(d);",
     "", ["30.00", "20.00", "15.00"]),
    ("currency / int, / float, / currency -> float",
     "currency<GBP> a = £90;\ncurrency<GBP> b = £30;\nfloat r;\ncurrency<GBP> h;\n"
     "currency<GBP> q;\nr = a / b;\nh = a / 4;\nq = a / 2.5;\n"
     "output(r);\noutput(h);\noutput(q);", "", ["3.00", "22.50", "36.00"]),
    ("nested arithmetic and precedence",
     "currency<INR> g = ₹1000;\ncurrency<INR> d = ₹100;\ncurrency<INR> t = ₹50;\n"
     "currency<INR> f;\nf = (g - d) + t;\noutput(f);\nf = g - d * 2;\noutput(f);\n"
     "f = (g - d) * 2;\noutput(f);", "", ["950.00", "800.00", "1800.00"]),
    ("same-currency comparison and if",
     "currency<INR> a = ₹5;\ncurrency<INR> b = ₹3;\nif (a > b) { output(a); }",
     "", ["5.00"]),
    ("console input / output",
     "currency<INR> a;\nint n;\ninput(a);\ninput(n);\noutput(a);\noutput(n);",
     "42.5\n7\n", ["42.50", "7"]),
    ("struct declaration and member access",
     ACCOUNT + "struct Account a;\na.accountNo = 1001;\na.balance = ₹50000.00;\n"
     "a.interestRate = 0.06;\noutput(a.accountNo);\noutput(a.balance);\n"
     "output(a.interestRate);", "", ["1001", "50000.00", "0.06"]),
    ("pointer to currency (address-of, dereference)",
     "currency<INR> amount;\ncurrency<INR> *ptr;\nptr = &amount;\n"
     "*ptr = ₹2500.00;\noutput(*ptr);\noutput(amount);", "", ["2500.00"]),
    ("pointer to struct and -> access",
     ACCOUNT + "struct Account acc;\nstruct Account *p;\np = &acc;\n"
     "p->balance = ₹10000.00;\np->accountNo = 7;\noutput(p->balance);\n"
     "output(acc.accountNo);", "", ["10000.00", "7"]),
    ("file open / write / close / open / read / close",
     ACCOUNT + "struct Account acc;\nFILE *fp;\nint n;\nfloat x;\n"
     "acc.accountNo = 1001;\nacc.balance = ₹250.50;\n"
     'fp = fopen("t_account.txt", "w");\n'
     'fprintf(fp, "%d %.2f\\n", acc.accountNo, acc.balance);\nfclose(fp);\n'
     'fp = fopen("t_account.txt", "r");\n'
     'fscanf(fp, "%d %f", &n, &x);\nfclose(fp);\noutput(n);\noutput(x);',
     "", ["1001", "250.50"]),
    ("spec 22 demonstration program",
     ACCOUNT + "struct Account account;\nstruct Account *ptr;\ncurrency<INR> interest;\n"
     "FILE *fp;\naccount.accountNo = 1001;\naccount.balance = ₹50000.00;\n"
     "account.interestRate = 0.06;\nptr = &account;\n"
     "interest = ptr->balance * ptr->interestRate;\n"
     "ptr->balance = ptr->balance + interest;\n"
     'fp = fopen("account.txt", "w");\n'
     'fprintf(fp, "%d %.2f\\n", ptr->accountNo, ptr->balance);\n'
     "fclose(fp);\noutput(ptr->balance);", "", ["53000"]),
    ("printf flags, width and %% are accepted silently",
     ACCOUNT + "struct Account acc;\nFILE *fp;\nacc.accountNo = 5;\nacc.balance = ₹9.50;\n"
     'fp = fopen("t_flags.txt", "w");\n'
     'fprintf(fp, "%5.2f%% %-4d|\\n", acc.balance, acc.accountNo);\nfclose(fp);\n'
     "output(acc.accountNo);", "", ["5"]),
    ("binary fwrite / fread round trip",
     "FILE *fp;\nint n = 1234;\nint m = 0;\n"
     'fp = fopen("t_bin.dat", "wb");\nfwrite(&n, 4, 1, fp);\nfclose(fp);\n'
     'fp = fopen("t_bin.dat", "rb");\nfread(&m, 4, 1, fp);\nfclose(fp);\noutput(m);',
     "", ["1234"]),
    ("struct member named _line (old internal-name clash)",
     "struct S { int _line; };\nstruct S s;\ns._line = 4;\noutput(s._line);", "", ["4"]),
    ("while / if / else (extension)",
     "int i = 0;\nint s = 0;\nwhile (i < 3) { s = s + i; i = i + 1; }\n"
     "if (s == 3) { output(s); } else { output(i); }", "", ["3"]),
]

# --------------------------------------------------------------------------
# INVALID programs: (name, source, substring expected in the diagnostics)
# --------------------------------------------------------------------------
CUR = "currency<INR> a = ₹1;\n"
INVALID = [
    ("undeclared variable", "total = 5;", "Undeclared identifier 'total'"),
    ("redeclared identifier", "int a;\nint a;", "Redeclaration of 'a'"),
    ("currency symbol / code mismatch", "currency<INR> a = $5;", "currency codes differ"),
    ("add different currencies",
     CUR + "currency<USD> b = $1;\na = a + b;", "Cannot add currency<INR> and currency<USD>"),
    ("currency + float", CUR + "float f = 1.5;\na = a + f;", "Cannot add currency<INR> and float"),
    ("currency - int", CUR + "a = a - 2;", "Cannot subtract currency<INR> and int"),
    ("float assigned to currency", "currency<INR> a;\na = 0.5;", "Cannot assign float to currency<INR>"),
    ("currency * currency", CUR + "currency<INR> b = ₹2;\nb = a * b;", "Cannot multiply two currency"),
    ("int / currency", CUR + "float f;\nf = 1 / a;", "Cannot divide int and currency<INR>"),
    ("currency compared with plain number", CUR + "if (a > 5) { }", "Cannot compare currency<INR> and int"),
    ("malformed currency literal", "currency<INR> a = ₹;", "expected a number after currency symbol"),
    ("unknown struct member", ACCOUNT + "struct Account acc;\nacc.limit = 1;", "has no member 'limit'"),
    ("'.' used on a pointer",
     ACCOUNT + "struct Account *p;\np.accountNo = 1;", "use '->'"),
    ("'->' used on a struct",
     ACCOUNT + "struct Account acc;\nacc->accountNo = 1;", "requires a pointer to struct"),
    ("pointer type mismatch",
     "currency<INR> *p;\ncurrency<USD> d = $1;\np = &d;", "Incompatible pointer types"),
    ("dereference of a non-pointer", "int x;\n*x = 1;", "Cannot dereference non-pointer"),
    ("address of a non-lvalue", "int *p;\np = &5;", "non-lvalue"),
    ("unknown struct", "struct Nope n;", "Unknown struct 'Nope'"),
    ("file operation on non-FILE pointer", "int x;\nfclose(x);", "fclose() expects FILE*"),
    ("malformed input statement", "input(5);", "input() needs a variable"),
    ("malformed output statement", "output(;", "unexpected ';' in expression"),
    ("syntax error in arithmetic", "int x;\nx = 1 + ;", "unexpected ';' in expression"),
    ("missing semicolon", "int x = 1\nint y;", "expected ';' after declaration"),
    ("illegal character", "int x = 5 @;", "unexpected character"),
    ("unterminated string", 'FILE *f;\nf = fopen("a.txt);', "unterminated string literal"),
    ("string assigned to int", 'int x;\nx = "hi";', "cannot assign string to int"),
    ("output of a string", 'output("hi");', "cannot print a value of type string"),
    ("fprintf on a non-FILE value", 'int x;\nfprintf(x, "%d", 1);', "fprintf() expects FILE*"),
    ("fscanf on a non-FILE value", 'int x;\nint n;\nfscanf(x, "%d", &n);', "fscanf() expects FILE*"),
    ("fopen with the wrong argument count", 'FILE *f;\nf = fopen("a.txt");', "fopen() expects 2 argument(s), got 1"),
    ("fopen with a non-string argument", 'FILE *f;\nint x;\nf = fopen(x, "w");', "fopen() expects a string literal"),
    ("fprintf with no format", "FILE *f;\nfprintf(f);", "fprintf() expects at least 2"),
    ("fprintf with a non-literal format", "FILE *f;\nint x;\nfprintf(f, x);", "format must be a string literal"),
    ("fscanf into a value instead of an address", 'FILE *f;\nint n;\nfscanf(f, "%d", n);', "must be an address"),
    ("fread on a non-FILE stream", "int a[3];\nint x;\nfread(a, 4, 3, x);", "fread() expects FILE* as argument 4"),
    ("fwrite with a non-integer count", "FILE *f;\nint n;\nfwrite(&n, 4, 1.5, f);", "argument 3 must be an integer"),
    ("missing member '_line' is an error, not a crash",
     "struct S { int x; };\nstruct S s;\ns._line = 1;", "has no member '_line'"),
    ("unsupported fileoutput", "FILE *f;\nint x;\nfileoutput(f, x);", "not supported yet"),
]

# --------------------------------------------------------------------------
# KNOWN GAPS: should be rejected eventually but are not checked yet
# --------------------------------------------------------------------------
KNOWN_GAPS = []        # (name, source, expected substring) - none at the moment

# --------------------------------------------------------------------------
# TABLES: (name, source, expected symbol rows or None, expected structure rows or None,
#          substrings expected in the rendered symbol table text)
# --------------------------------------------------------------------------
R = lambda *cells: tuple(cells)
TABLES = [
    ("spec 6.2 / 6.3 rows",
     "currency<INR> basic;\nfloat taxRate;\nint quantity;\n"
     "struct Account { int accountNo; currency<INR> balance; float interestRate; };\n"
     "struct Account *ptr;\nFILE *fp;",
     [R("basic", "variable", "currency", "INR", "0", "-", "float", "local / 1"),
      R("taxRate", "variable", "float", "-", "0", "-", "float", "local / 2"),
      R("quantity", "variable", "int", "-", "0", "-", "int", "local / 3"),
      R("ptr", "pointer", "struct", "-", "1", "Account", "struct Account *", "local / 5"),
      R("fp", "file pointer", "FILE", "-", "1", "-", "FILE *", "local / 6")],
     [R("Account", "accountNo", "int", "-", "0", "int"),
      R("Account", "balance", "currency", "INR", "0", "float"),
      R("Account", "interestRate", "float", "-", "0", "float")],
     ["Symbol Table", "Identifier", "Scope / Line", "file pointer"]),
    ("nested scopes survive after the block closes; arrays; currency pointer",
     "int a = 1;\n{\n    int b = 2;\n    {\n        int c = 3;\n    }\n}\n"
     "int v[3];\ncurrency<USD> *cp;",
     [R("a", "variable", "int", "-", "0", "-", "int", "local / 1"),
      R("b", "variable", "int", "-", "0", "-", "int", "block 1 / 3"),
      R("c", "variable", "int", "-", "0", "-", "int", "block 2 / 5"),
      R("v", "array", "int", "-", "0", "-", "int[3]", "local / 8"),
      R("cp", "pointer", "currency", "USD", "1", "-", "float *", "local / 9")],
     [], []),
    ("self-referential struct (pointer member)",
     "struct Node { int v; struct Node *next; };", [],
     [R("Node", "v", "int", "-", "0", "int"),
      R("Node", "next", "struct", "-", "1", "struct Node *")], []),
    ("a redeclaration is recorded once",
     "int a;\nint a;",
     [R("a", "variable", "int", "-", "0", "-", "int", "local / 1")], [], []),
    ("symbols with an unresolved type are left out",
     "struct Nope n;\nint ok;",
     [R("ok", "variable", "int", "-", "0", "-", "int", "local / 2")], [], []),
    ("empty program renders '(empty)'", "", [], [], ["Symbol Table", "(empty)"]),
]

# --------------------------------------------------------------------------
# CODEGEN: (name, source, CodeGen options, must contain, must NOT contain)
# --------------------------------------------------------------------------
PLAIN = {"labels": False}
FOUR = ("currency<INR> a = ₹1;\ncurrency<USD> b = $1;\ncurrency<EUR> c = €1;\n"
        "currency<GBP> d = £1;\noutput(a);\noutput(b);\noutput(c);\noutput(d);")
PTRS = (ACCOUNT + "struct Account acc;\nstruct Account *p;\ncurrency<INR> m = ₹4;\n"
        "currency<INR> *q;\np = &acc;\nq = &m;\noutput(*q);\noutput(p->balance);\n"
        "output(m * 2);\noutput(m + m);")
CODEGEN = [
    ("each currency prints its own symbol", FOUR, PLAIN,
     ['printf("₹%.2f\\n", a)', 'printf("$%.2f\\n", b)',
      'printf("€%.2f\\n", c)', 'printf("£%.2f\\n", d)'], []),
    ("symbol follows the expression type (*p, ->, arithmetic)", PTRS, PLAIN,
     ['printf("₹%.2f\\n", *q)', 'printf("₹%.2f\\n", p->balance)',
      'printf("₹%.2f\\n", m * 2)', 'printf("₹%.2f\\n", m + m)'], []),
    ("currency / currency and plain numbers get no symbol",
     "currency<INR> a = ₹6;\ncurrency<INR> b = ₹3;\nfloat r = 1.5;\nint n = 2;\n"
     "output(a / b);\noutput(r);\noutput(n);", PLAIN,
     ['printf("%.2f\\n", a / b)', 'printf("%.2f\\n", r)', 'printf("%d\\n", n)'], ["₹"]),
    ("symbol appears in labelled output", "currency<INR> total = ₹5;\noutput(total);",
     {"labels": True}, ['total = ₹%.2f'], []),
    ("currency literals get an f suffix, plain floats do not",
     "currency<INR> s = ₹50000.00;\ncurrency<INR> t = ₹500;\nfloat r = 0.06;",
     PLAIN, ["50000.00f", "500.00f", "0.06;"], ["0.06f"]),
    ("--currency-output code", FOUR, {"labels": False, "currency_output": "code"},
     ['"INR %.2f\\n"', '"USD %.2f\\n"', '"EUR %.2f\\n"', '"GBP %.2f\\n"'], ["₹", "$%"]),
    ("--currency-output none", FOUR, {"labels": False, "currency_output": "none"},
     ['printf("%.2f\\n", a)'], ["₹", "INR", "€", "£"]),
]

# --------------------------------------------------------------------------
# WARNING programs: accepted, but must raise the expected warning
# --------------------------------------------------------------------------
WARNINGS = [
    ("%d given a float", 'FILE *f;\nfprintf(f, "%d", 1.5);', "expects an int"),
    ("%f given an int", 'FILE *f;\nint n;\nfprintf(f, "%f", n);', "expects a float or currency"),
    ("too few arguments for the format", 'FILE *f;\nfprintf(f, "%d %d", 1);', "format expects 2"),
    ("fscanf %d into a float variable", 'FILE *f;\nfloat x;\nfscanf(f, "%d", &x);', "expects an int"),
    ("unknown conversion", 'FILE *f;\nfprintf(f, "%q");', "unknown conversion"),
    ("unusual fopen mode", 'FILE *f;\nf = fopen("a.txt", "x");', "unusual mode"),
]


# --------------------------------------------------------------------------
def front_end(source):
    """Lex + parse + analyze. Returns (program or None, [errors], [warnings])."""
    try:
        program = Parser.from_source(source).parse()
    except (LexerError, ParseError) as err:
        return None, [str(err)], []
    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)
    errors = [str(d) for d in analyzer.diagnostics if d.severity == "Error"]
    warnings = [str(d) for d in analyzer.diagnostics if d.severity == "Warning"]
    return program, errors, warnings


def run_valid(name, source, stdin, expected, workdir, have_gcc, verbose):
    program, errors, warnings = front_end(source)
    if errors:
        return "FAIL", "unexpected error: " + errors[0]
    if warnings:                                  # valid programs must not trigger false alarms
        return "FAIL", "unexpected warning: " + warnings[0]
    code = CodeGen(labels=False).translate(program, name)
    if not have_gcc:
        return "PASS", "C generated (gcc not found: compile/run skipped)"
    stem = os.path.join(workdir, "prog")
    with open(stem + ".c", "w", encoding="utf-8") as fh:
        fh.write(code)
    cc = subprocess.run(["gcc", "-Wall", "-o", stem, stem + ".c"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if cc.returncode != 0:
        return "FAIL", "gcc failed: " + cc.stderr.strip().splitlines()[0]
    run = subprocess.run([stem], input=stdin, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=workdir, timeout=10)
    if run.returncode != 0:
        return "FAIL", f"program exited with {run.returncode}"
    missing = [e for e in expected if e not in run.stdout]
    if missing:
        return "FAIL", f"output missing {missing}; got {run.stdout.strip()!r}"
    return "PASS", run.stdout.strip().splitlines()[-1] if verbose else ""


def run_invalid(source, expected):
    _, errors, _ = front_end(source)
    if not errors:
        return "FAIL", "accepted an invalid program"
    if not any(expected in e for e in errors):
        return "FAIL", f"expected {expected!r}; got {errors[0]!r}"
    return "PASS", errors[0] if VERBOSE else ""


def run_tables(source, sym_rows, struct_rows, text_has):
    try:
        program = Parser.from_source(source).parse()
    except (LexerError, ParseError) as err:
        return "FAIL", f"did not parse: {err}"
    analyzer = SemanticAnalyzer()
    analyzer.analyze(program)                   # diagnostics are irrelevant here
    table = analyzer.table
    if sym_rows is not None and table.symbol_rows() != sym_rows:
        return "FAIL", f"symbol rows differ:\n            got      {table.symbol_rows()}\n            expected {sym_rows}"
    if struct_rows is not None and table.structure_rows() != struct_rows:
        return "FAIL", f"structure rows differ:\n            got      {table.structure_rows()}\n            expected {struct_rows}"
    text = table.symbol_table_text()
    missing = [x for x in text_has if x not in text]
    if missing:
        return "FAIL", f"rendered table is missing {missing}"
    return "PASS", ""


def run_codegen(name, source, options, must, must_not):
    program, errors, _ = front_end(source)
    if errors:
        return "FAIL", "unexpected error: " + errors[0]
    code = CodeGen(**options).translate(program, name)
    missing = [m for m in must if m not in code]
    if missing:
        return "FAIL", f"generated C is missing {missing}"
    present = [m for m in must_not if m in code]
    if present:
        return "FAIL", f"generated C must not contain {present}"
    return "PASS", ""


def run_warning(source, expected):
    _, errors, warnings = front_end(source)
    if errors:
        return "FAIL", "should be accepted, got error: " + errors[0]
    if not any(expected in w for w in warnings):
        return "FAIL", f"expected a warning with {expected!r}; got {warnings!r}"
    return "PASS", warnings[0] if VERBOSE else ""


def main():
    global VERBOSE
    VERBOSE = "-v" in sys.argv
    have_gcc = shutil.which("gcc") is not None
    counts = {"PASS": 0, "FAIL": 0, "XFAIL": 0, "XPASS": 0}

    def show(group, name, status, note):
        counts[status] += 1
        line = f"  {status:<5} {group:<8} {name}"
        print(line + (f"\n          -> {note}" if note and (status != "PASS" or VERBOSE) else ""))

    with tempfile.TemporaryDirectory() as workdir:
        print(f"gcc: {'found' if have_gcc else 'NOT found (compile/run skipped)'}\n")
        for name, src, stdin, expected in VALID:
            show("valid", name, *run_valid(name, dedent(src), stdin, expected,
                                           workdir, have_gcc, VERBOSE))
        for name, src, expected in INVALID:
            show("invalid", name, *run_invalid(dedent(src), expected))
        for name, src, sym_rows, struct_rows, text_has in TABLES:
            show("tables", name, *run_tables(dedent(src), sym_rows, struct_rows, text_has))
        for name, src, options, must, must_not in CODEGEN:
            show("codegen", name, *run_codegen(name, dedent(src), options, must, must_not))
        for name, src, expected in WARNINGS:
            show("warning", name, *run_warning(dedent(src), expected))
        for name, src, expected in KNOWN_GAPS:
            status, note = run_invalid(dedent(src), expected)
            show("gap", name, "XPASS" if status == "PASS" else "XFAIL",
                 "now detected - move to INVALID" if status == "PASS" else "not checked yet")

    total = sum(counts.values())
    print(f"\n{total} tests: {counts['PASS']} passed, {counts['FAIL']} failed, "
          f"{counts['XFAIL']} known gaps, {counts['XPASS']} newly passing")
    return 1 if counts["FAIL"] else 0


VERBOSE = False

if __name__ == "__main__":
    sys.exit(main())
# Currency-C Compiler

A compiler front-end written in Python from scratch (no ANTLR, PLY or other parser generators) for **Currency-C**, a C-like language with a built-in `currency<CODE>` type. The compiler checks currency safety at compile time, for example it rejects `INR + USD`, and translates valid programs into standard C.

```text
Source (.cur) → Lexer → Parser → Semantic Analyzer → C Code Generator → output.c
                tokens    AST     symbol tables +      standard C,
                                  type checking        compiled with gcc
```

## Highlights

- **Four hand-written phases:** regex-based lexer, recursive-descent parser, semantic analyzer, visitor-based code generator (~2,400 lines of Python, standard library only).
- **Currency-aware type system:** each currency value carries its code (`INR`, `USD`, `EUR`, `GBP`). Mixed-currency arithmetic, `currency + float` and float-to-currency assignment are compile-time errors.
- **Structs, pointers, arrays and file I/O** (`fopen`, `fclose`, `fprintf`, `fscanf`, `fread`, `fwrite`) are checked with the same type system.
- **Diagnostics:** lexical, syntax and semantic errors with line numbers. The parser has an optional panic-mode recovery mode that reports several syntax errors in one run.
- **Reports:** symbol table, structure table, per-expression type information and a semantic summary.
- **74 end-to-end tests:** valid programs are compiled with GCC and executed, and invalid programs must produce the expected diagnostic.

## Example

Currency-C source (`ex.cur`):

```c
currency<INR> salary = ₹50000;
currency<INR> bonus = ₹2500;
currency<INR> total;

total = salary + bonus;
output(total);
```

Generated C (`python codegen.py ex.cur --plain`):

```c
#include <stdio.h>

int main(void) {
    float salary = 50000.00f;
    float bonus = 2500.00f;
    float total;
    total = salary + bonus;
    printf("₹%.2f\n", total);
    return 0;
}
```

Running the compiled program prints `₹52500.00`.

Invalid programs are rejected before any C is generated:

```c
currency<INR> india = ₹100;
currency<USD> dollar = $5;
india = india + dollar;     // different currencies
float f = 1.5;
india = india + f;          // plain number added to a currency
total = 5;                  // undeclared
```

```text
Semantic Error - Line 3: Cannot add currency<INR> and currency<USD>
Semantic Error - Line 5: Cannot add currency<INR> and float
Semantic Error - Line 6: Undeclared identifier 'total'

=== Semantic Analysis Report ===
  Declarations analyzed: 3
  Expressions analyzed:  13
  Warnings:              0
  Errors:                3
```

## Compilation Pipeline

| Phase | File | What it does |
|---|---|---|
| 1. Lexical analysis | `lexer.py` | Regex master pattern producing typed tokens with line and column. Handles `//` and `/* */` comments, string literals, multi-character operators (`->`, `==`, `<=`, `&&`, ...), currency codes and symbols (`₹ $ € £`). |
| 2. Syntax analysis | `parser.py` | Recursive-descent parser with six binary precedence levels plus unary and postfix operators. Builds an AST of dataclasses. Optional panic-mode recovery (skips to the next `;` or `}`, gives up after 20 errors). |
| 3. Semantic analysis | `semantic.py`, `symbol_table.py` | Scoped symbol table, structure table, expression type inference, currency rules, pointer and member-access checks, file-call and format-string validation. |
| 4. Code generation | `codegen.py` | Visitor over the validated AST. Currency becomes `float`, output formats are chosen from each expression's type, and the C source is written to `output.c`. |

## Language Features

| Category | Supported |
|---|---|
| Types | `int`, `float`, `currency<INR\|USD\|EUR\|GBP>`, `struct Name`, `FILE *` |
| Literals | `10`, `0.18`, `₹500.00`, `$250.50`, `€175.75`, `£90.00`, `"text"` |
| Declarations | Multiple declarators (`int a, b = 2;`), pointers (`int *p;`), structs, arrays |
| Expressions | `+ - * /`, comparison, `== !=`, `&& \|\|`, unary `-` `&` `*`, `.` and `->` member access, indexing |
| Statements | Assignment, `if`/`else`, `while`, blocks, `return`, `input()`, `output()` |
| File operations | `fopen`, `fclose`, `fprintf`, `fscanf`, `fread`, `fwrite` (standard C argument order) |

`fileoutput()` and `fileinput()` are reserved words but report "not supported yet".

## Currency Type Rules

`C` means the same currency code on both sides.

| Expression | Result |
|---|---|
| `currency<C> + currency<C>`, `-` | `currency<C>` |
| `currency<C> * int/float`, `int/float * currency<C>` | `currency<C>` |
| `currency<C> / int/float` | `currency<C>` |
| `currency<C> / currency<C>` | `float` (a ratio) |
| `int`/`float` arithmetic | `int` or `float` as in C |
| `INR + USD` (different codes) | **Error** |
| `currency + int/float`, `currency - int/float` | **Error** |
| `currency * currency`, `int / currency` | **Error** |
| Assigning a plain number to a currency variable | **Error** |
| `₹100` assigned to a `currency<USD>` variable | **Error** (symbol and code differ) |
| Comparing a currency with a plain number | **Error** |
| `*ptr = ...` or `ptr = &x` with different currency codes | **Error** (pointer type mismatch) |

## Getting Started

**Requirements:** Python 3.10+. GCC is optional, needed only to compile and run the generated C and the full test suite.

```bash
git clone https://github.com/YOUR_USERNAME/currency-c-compiler.git
cd currency-c-compiler

python codegen.py sample.cur        # compile a program to output.c
gcc output.c -o program             # compile the generated C
./program                           # Windows: .\program.exe
```

### Command reference

| Command | Purpose |
|---|---|
| `python lexer.py [file.cur]` | Print the token stream |
| `python parser.py [file.cur]` | Print the AST |
| `python parser.py file.cur --recover` | Report every syntax error (panic-mode recovery) instead of stopping at the first |
| `python semantic.py [file.cur]` | Semantic analysis and summary report |
| `python semantic.py file.cur --tables` | Also print the symbol and structure tables |
| `python semantic.py file.cur --types` | Also print the type inferred for every expression |
| `python codegen.py file.cur` | Full compile to `output.c` (skipped if there are errors) |
| `python codegen.py file.cur -o out.c` | Choose the output file |
| `python codegen.py file.cur --plain` | No banner, labels or input prompts in the generated C |
| `python codegen.py file.cur --currency-output symbol\|code\|none` | How `output()` prints currency: `₹500.00` (default), `INR 500.00`, or `500.00` |
| `python codegen.py file.cur --dump-ast` | Print the validated AST and exit |

### Symbol and structure tables

Excerpt of `python semantic.py file.cur --tables` for a larger program:

```text
Symbol Table
Identifier | Category     | Source Type | Currency | Pointer Level | Structure | Target Type      | Scope / Line
-----------+--------------+-------------+----------+---------------+-----------+------------------+-------------
salary     | variable     | currency    | INR      | 0             | -         | float            | local / 1
ptr        | pointer      | struct      | -        | 1             | Account   | struct Account * | local / 10
fp         | file pointer | FILE        | -        | 1             | -         | FILE *           | local / 11
```

Top-level declarations are labelled `local`, nested blocks are numbered (`block 1`, `block 2`, ...). Symbols stay in the table after their block closes.

### Windows consoles

`output()` prints currency symbols such as `₹`, which legacy `cmd` consoles may show as garbage. Run `chcp 65001` first, or use `--currency-output code`.

## Error Handling

| Class | Example | Behaviour |
|---|---|---|
| Lexical | illegal character, unterminated string or comment | Stops at the first error, with line and column |
| Syntax | missing `;`, malformed expression | Stops at the first error; `--recover` collects up to 20 |
| Semantic | undeclared name, currency mismatch, bad member access | Analysis continues after each error so all of them are reported |
| Warning | doubtful `printf`/`scanf` format specifiers | Reported, never blocks compilation |

Code generation is skipped when any error is found. Format-string problems are warnings, not errors, because real C allows more than the compiler models. Valid programs must never be rejected for them.

## Testing

```bash
python run_test.py          # all tests
python run_test.py -v       # include diagnostics and program output
```

74 tests, with the exit code non-zero if any fail:

| Group | Tests | What is checked |
|---|---|---|
| Valid | 17 | Compiled with GCC, executed, and the program output is verified (currency arithmetic, structs, pointers, file write/read, binary I/O, the full demonstration program) |
| Invalid | 38 | Each invalid program must be rejected with the expected diagnostic text (type, currency, pointer, member, file-call, lexical and syntax errors) |
| Warnings | 6 | Doubtful formats are accepted with the expected warning |
| Tables | 6 | Exact symbol and structure table rows, including nested scopes and arrays |
| Codegen | 7 | Generated C text: currency symbols, `f` literal suffix, output modes |

If GCC is not installed, the compile-and-run step is skipped and the generated C text is still checked.

## Project Structure

```text
currency-c-compiler/
├── lexer.py            # Phase 1: tokens
├── parser.py           # Phase 2: AST and syntax errors
├── semantic.py         # Phase 3: type checking, diagnostics, reports
├── symbol_table.py     # Types, symbols, scopes, symbol and structure tables
├── codegen.py          # Phase 4: C code generation and CLI
├── run_test.py         # end-to-end test suite
├── sample.cur          # sample Currency-C program
├── demo.cur            # small demonstration program
├── README.md
├── LICENSE
└── .gitignore
```

`output.c` and compiled binaries are generated and are not part of the source.

## Design Notes and Limitations

- **Currency is a compile-time property.** In the generated C it is a plain `float`, so currency codes are enforced by the compiler, not at runtime. Floats are a teaching simplification. Integer minor units or decimal arithmetic would be needed for real financial accuracy.
- **Restricted language.** There are no function definitions or user-defined function calls, and no unions, enums, macros or dynamic memory.
- **Extensions beyond the core spec:** arrays, multi-level pointers, `if`/`else`/`while`/`return`, comparison and logical operators.
- **No automatic currency conversion.** Mixing currencies is an error. There is no conversion feature.
- **Lexical errors stop at the first error;** only the parser has a recovery mode.

## Possible Future Work

- Explicit currency conversion operation
- Integer minor-unit or decimal representation for exact money arithmetic
- `fileoutput()` and `fileinput()` simplified file operations
- Function definitions and calls
- Error recovery in the lexer, and recovery wired into the main compile command

## License

MIT. See [LICENSE](LICENSE).

"""
lexer.py - Phase 1: Lexical Analysis for the Currency-C (.cur) language.

A hand-written lexer built on Python's `re` module (no PLY / ANTLR).
It converts raw source text into a list of Token objects, which the
Phase 2 Recursive Descent Parser will consume.

Usage:
    python lexer.py                 # runs the built-in demo
    python lexer.py program.cur     # lexes a source file

    from lexer import Lexer, TokenType
    tokens = Lexer.from_file("program.cur").tokenize()
"""

import re
import sys
from enum import Enum, auto


# ---------------------------------------------------------------------------
# Token categories
# ---------------------------------------------------------------------------
class TokenType(Enum):
    KEYWORD         = auto()   # currency, int, float, struct, FILE, ...
    CURRENCY_CODE   = auto()   # INR, USD, EUR, GBP
    CURRENCY_SYMBOL = auto()   # $, ₹, €, £
    IDENTIFIER      = auto()   # salary, accountNo, ...
    INT_LITERAL     = auto()   # 10
    FLOAT_LITERAL   = auto()   # 50000.00
    OPERATOR        = auto()   # = + - * / & < > . -> == != <= >= && ||
    DELIMITER       = auto()   # ( ) { } [ ] ; ,
    FILE_FUNCTION   = auto()   # fopen, fclose, fprintf, fscanf, fread, fwrite
    EOF             = auto()   # end-of-input marker (handy for the parser)


# ---------------------------------------------------------------------------
# Language definition tables
# ---------------------------------------------------------------------------
KEYWORDS = {
    "currency", "int", "float", "struct", "FILE",
    "input", "output", "if", "else", "while", "return",
}
CURRENCY_CODES = {"INR", "USD", "EUR", "GBP"}
FILE_FUNCTIONS = {"fopen", "fclose", "fprintf", "fscanf", "fread", "fwrite"}

# Words that look like identifiers but get a special category.
# (Identifiers are matched first, then reclassified by this lookup.)
RESERVED_WORDS = {
    **{w: TokenType.KEYWORD for w in KEYWORDS},
    **{w: TokenType.CURRENCY_CODE for w in CURRENCY_CODES},
    **{w: TokenType.FILE_FUNCTION for w in FILE_FUNCTIONS},
}


# ---------------------------------------------------------------------------
# Token and error classes
# ---------------------------------------------------------------------------
class Token:
    """A single lexical token."""

    __slots__ = ("type", "value", "line_number", "column")

    def __init__(self, type_, value, line_number, column=0):
        self.type = type_
        self.value = value              # the exact lexeme (always a string)
        self.line_number = line_number  # 1-based
        self.column = column            # 1-based, useful for error messages

    def __repr__(self):
        return (f"Token({self.type.name}, {self.value!r}, "
                f"line={self.line_number})")


class LexerError(Exception):
    """Raised on an illegal character or unterminated comment."""

    def __init__(self, message, line, column):
        super().__init__(f"Lexical error at line {line}, col {column}: {message}")
        self.line = line
        self.column = column


# ---------------------------------------------------------------------------
# The Lexer
# ---------------------------------------------------------------------------
class Lexer:
    # Each entry is (group_name, regex). They are joined into ONE master
    # regex, so order matters: the first alternative that matches wins.
    _TOKEN_SPEC = [
        ("BLOCK_COMMENT", r"/\*.*?\*/"),               # /* ... */ (may span lines)
        ("BAD_COMMENT",   r"/\*"),                     # '/*' with no closing '*/'
        ("LINE_COMMENT",  r"//[^\n]*"),                # // ... end of line
        ("FLOAT",         r"\d+\.\d+"),                # 50000.00  (before INT!)
        ("INT",           r"\d+"),                     # 10
        ("WORD",          r"[A-Za-z_][A-Za-z0-9_]*"),  # identifier / keyword / code / file fn
        # Two-character operators: MUST come before the single-character
        # group so that '<=' is not split into '<' '=' (same for '->', etc.)
        ("MULTI_OP",      r"->|==|!=|<=|>=|&&|\|\|"),
        ("OPERATOR",      r"[=+\-*/&<>.]"),
        ("DELIMITER",     r"[(){}\[\];,]"),
        ("CURRENCY_SYM",  r"[$₹€£]"),
        ("NEWLINE",       r"\n"),
        ("SKIP",          r"[ \t\r\f\v]+"),            # insignificant whitespace
        ("MISMATCH",      r"."),                       # anything else = error
    ]
    _MASTER_RE = re.compile(
        "|".join(f"(?P<{name}>{pattern})" for name, pattern in _TOKEN_SPEC),
        re.DOTALL,   # lets '.' in BLOCK_COMMENT cross newlines
    )

    def __init__(self, source):
        self.source = source

    @classmethod
    def from_file(cls, path):
        """Build a Lexer from a .cur source file."""
        with open(path, "r", encoding="utf-8") as f:
            return cls(f.read())

    def tokenize(self):
        """Scan the whole source and return a list of Tokens (ending in EOF)."""
        tokens = []
        line = 1
        line_start = 0   # index in source where the current line begins

        for match in self._MASTER_RE.finditer(self.source):
            kind = match.lastgroup
            text = match.group()
            column = match.start() - line_start + 1

            if kind == "NEWLINE":
                line += 1
                line_start = match.end()

            elif kind == "SKIP" or kind == "LINE_COMMENT":
                pass

            elif kind == "BLOCK_COMMENT":
                # Keep line numbers accurate across multi-line comments.
                newlines = text.count("\n")
                if newlines:
                    line += newlines
                    line_start = match.start() + text.rfind("\n") + 1

            elif kind == "BAD_COMMENT":
                raise LexerError("unterminated block comment", line, column)

            elif kind == "FLOAT":
                tokens.append(Token(TokenType.FLOAT_LITERAL, text, line, column))

            elif kind == "INT":
                tokens.append(Token(TokenType.INT_LITERAL, text, line, column))

            elif kind == "WORD":
                # Case-sensitive lookup: 'FILE' is a keyword, 'file' is not.
                ttype = RESERVED_WORDS.get(text, TokenType.IDENTIFIER)
                tokens.append(Token(ttype, text, line, column))

            elif kind in ("MULTI_OP", "OPERATOR"):
                tokens.append(Token(TokenType.OPERATOR, text, line, column))

            elif kind == "DELIMITER":
                tokens.append(Token(TokenType.DELIMITER, text, line, column))

            elif kind == "CURRENCY_SYM":
                tokens.append(Token(TokenType.CURRENCY_SYMBOL, text, line, column))

            elif kind == "MISMATCH":
                raise LexerError(f"unexpected character {text!r}", line, column)

        tokens.append(Token(TokenType.EOF, "", line, len(self.source) - line_start + 1))
        return tokens


# ---------------------------------------------------------------------------
# Convenience wrapper for the Phase 2 Recursive Descent Parser
# ---------------------------------------------------------------------------
class TokenStream:
    """Cursor over a token list with the usual parser helpers."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset=0):
        """Look ahead without consuming (clamps at EOF)."""
        i = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[i]

    def advance(self):
        """Consume and return the current token."""
        tok = self.peek()
        if tok.type != TokenType.EOF:
            self.pos += 1
        return tok

    def check(self, type_, value=None):
        """True if the current token matches the type (and value, if given)."""
        tok = self.peek()
        return tok.type == type_ and (value is None or tok.value == value)

    def match(self, type_, value=None):
        """Consume the current token only if it matches; return it or None."""
        if self.check(type_, value):
            return self.advance()
        return None

    def expect(self, type_, value=None):
        """Consume a matching token or raise a SyntaxError with the line number."""
        tok = self.match(type_, value)
        if tok is None:
            cur = self.peek()
            wanted = value if value is not None else type_.name
            raise SyntaxError(
                f"Line {cur.line_number}: expected {wanted!r}, got {cur.value!r}"
            )
        return tok


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
SAMPLE_SOURCE = """\
// Bank account demo - exercises every token category
/* Multi-line comment:
   line numbers must stay correct after this */

struct Account {
    int accountNo;
    currency<INR> balance;
};

FILE fp;
int count = 10;
float rate = 0.05;
currency<USD> bonus = $100;
currency<INR> salary = ₹50000.00;
currency<EUR> fee = €12.50;
currency<GBP> tax = £7.25;
int values[3];
int *p = &count;
currency<INR> *ptr;
struct Account acct, *acctPtr;

fp = fopen(log, 1);
input(salary);
*p = *p + 1;

while (count > 0 && salary != tax) {
    salary = salary + bonus * 2 - fee / 4;
    count = count - 1;
}
if (count == 0) { output(count); }

if (salary <= tax || bonus >= fee) {
    output(salary);
} else {
    fprintf(fp, salary, &count);
    fscanf(fp, &count);
    fread(fp, values, 3);
    fwrite(fp, values, 3);
}

acct.balance = salary;
acctPtr->balance = bonus;
fclose(fp);
return 0;
"""


def main():
    if len(sys.argv) > 1:
        source = Lexer.from_file(sys.argv[1]).source
        print(f"Lexing file: {sys.argv[1]}\n")
    else:
        source = SAMPLE_SOURCE
        print("Lexing built-in sample:\n")
        print(source)

    try:
        tokens = Lexer(source).tokenize()
    except LexerError as err:
        print(err, file=sys.stderr)
        sys.exit(1)

    print(f"{'LINE':<6}{'TYPE':<18}VALUE")
    print("-" * 44)
    for tok in tokens:
        print(f"{tok.line_number:<6}{tok.type.name:<18}{tok.value!r}")
    print(f"\nTotal tokens (incl. EOF): {len(tokens)}")


if __name__ == "__main__":
    main()
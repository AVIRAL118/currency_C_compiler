"""
symbol_table.py - Symbol Table and Type definitions for the Currency-C language.
"""


class RedeclarationError(Exception):
    def __init__(self, previous_symbol):
        super().__init__(f"redeclaration (previously declared at line "
                         f"{getattr(previous_symbol, 'line_number', '?')})")
        self.previous = previous_symbol


# Map literal symbols to their underlying currency codes
CURRENCY_SYMBOLS = {
    '₹': 'INR',
    '$': 'USD',
    '€': 'EUR',
    '£': 'GBP'
}


class Type:
    def __init__(self, base_type, currency_code=None, struct_name=None, pointer_level=0):
        self.base_type = base_type
        self.currency_code = currency_code
        self.struct_name = struct_name
        self.pointer_level = pointer_level

    @property
    def is_error(self):
        return self.base_type == "error"

    @property
    def is_numeric(self):
        return self.base_type in ("int", "float") and self.pointer_level == 0

    @property
    def is_float(self):
        return self.base_type == "float" and self.pointer_level == 0

    @property
    def is_pointer(self):
        return self.pointer_level > 0

    @property
    def is_currency(self):
        return self.base_type == "currency" and self.pointer_level == 0

    @property
    def is_struct(self):
        return self.base_type == "struct" and self.pointer_level == 0

    @property
    def c_type(self):
        """The C type this Currency-C type is translated to (currency -> float)."""
        if self.is_error:
            return "error"
        if self.base_type == "currency":
            base = "float"                       # currency metadata is compile-time only
        elif self.base_type == "struct":
            base = f"struct {self.struct_name}"
        else:
            base = self.base_type
        return f"{base} {'*' * self.pointer_level}" if self.pointer_level else base

    def pointer_to(self):
        return Type(self.base_type, self.currency_code, self.struct_name, self.pointer_level + 1)

    def deref(self):
        return Type(self.base_type, self.currency_code, self.struct_name, self.pointer_level - 1)

    def _key(self):
        return (self.base_type, self.currency_code, self.struct_name, self.pointer_level)

    def __eq__(self, other):
        return isinstance(other, Type) and self._key() == other._key()

    def __hash__(self):                          # defining __eq__ alone makes Type unhashable
        return hash(self._key())

    def __str__(self):
        if self.is_error:
            return "error"
        # Test base_type (not is_currency/is_struct, which are False for pointers) so
        # 'currency<INR>*' and 'struct Account*' keep their code / name in messages.
        if self.base_type == "currency":
            base = f"currency<{self.currency_code}>"
        elif self.base_type == "struct":
            base = f"struct {self.struct_name}"
        else:
            base = self.base_type
        return base + "*" * self.pointer_level

    def __repr__(self):
        return f"Type({self})"


class Symbol:
    def __init__(self, name, type_obj, line_number, array_size=None, scope_level=0):
        self.name = name
        self.type = type_obj
        self.line_number = line_number
        self.array_size = array_size             # None => not an array
        self.scope_level = scope_level           # 0 = top level, n = n-th nested block

    # ---- derived fields used by the symbol / structure table printouts (spec 6)
    @property
    def declared_pointer_level(self):
        """Number of '*' in the declaration (an array's decay to a pointer is not counted)."""
        return self.type.pointer_level - (1 if self.array_size is not None else 0)

    @property
    def category(self):
        if self.array_size is not None:
            return "array"
        if self.type.base_type == "FILE" and self.type.pointer_level > 0:
            return "file pointer"
        return "pointer" if self.type.pointer_level > 0 else "variable"

    @property
    def target_type(self):
        """The C declaration type, e.g. 'float', 'struct Account *', 'int[3]'."""
        if self.array_size is None:
            return self.type.c_type
        element = Type(self.type.base_type, self.type.currency_code,
                       self.type.struct_name, self.declared_pointer_level)
        return f"{element.c_type}[{self.array_size}]"

    @property
    def scope_label(self):
        # The spec's tables call top-level declarations "local"; nested blocks are numbered.
        return "local" if self.scope_level == 0 else f"block {self.scope_level}"


class SymbolTable:
    def __init__(self):
        self.scopes = [{}]
        self.structs = {}              # struct name -> {member name -> Symbol}
        self.struct_lines = {}         # struct name -> line of its definition
        self.history = []              # every symbol ever declared, in order (scopes are popped)

    def enter_scope(self):
        self.scopes.append({})

    def exit_scope(self):
        self.scopes.pop()

    def declare(self, name, type_obj, line_number, array_size=None):
        current_scope = self.scopes[-1]
        if name in current_scope:
            raise RedeclarationError(current_scope[name])
        sym = Symbol(name, type_obj, line_number, array_size, scope_level=len(self.scopes) - 1)
        current_scope[name] = sym
        self.history.append(sym)
        return sym

    def lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def declare_struct(self, name, members, line_number):
        if name in self.structs:
            raise RedeclarationError(Symbol(name, None, self.struct_lines[name]))

        self.structs[name] = members
        self.struct_lines[name] = line_number

    def lookup_struct(self, name):
        return self.structs.get(name)

    def lookup_member(self, struct_name, member_name):
        struct_members = self.lookup_struct(struct_name)
        if struct_members and member_name in struct_members:
            return struct_members[member_name]
        return None

    # ------------------------------------------------------------------
    # Table printouts (spec 6.2 and 6.3). *_rows() return plain strings so
    # they can be tested; *_text() render them as aligned ASCII tables.
    # ------------------------------------------------------------------
    SYMBOL_HEADERS = ("Identifier", "Category", "Source Type", "Currency",
                      "Pointer Level", "Structure", "Target Type", "Scope / Line")
    STRUCT_HEADERS = ("Structure", "Member", "Source Type", "Currency",
                      "Pointer Level", "Target Type")

    def symbol_rows(self):
        """One row per declared identifier, in declaration order (all scopes).
        Symbols whose type could not be resolved (already reported) are left out."""
        rows = []
        for s in self.history:
            if s.type.is_error:
                continue
            t = s.type
            rows.append((s.name, s.category, t.base_type, t.currency_code or "-",
                         str(s.declared_pointer_level), t.struct_name or "-",
                         s.target_type, f"{s.scope_label} / {s.line_number}"))
        return rows

    def structure_rows(self):
        """One row per struct member, structs in definition order."""
        rows = []
        for struct_name, members in self.structs.items():
            for m in members.values():
                if m.type.is_error:
                    continue
                rows.append((struct_name, m.name, m.type.base_type,
                             m.type.currency_code or "-", str(m.declared_pointer_level),
                             m.target_type))
        return rows

    @staticmethod
    def _render(title, headers, rows):
        if not rows:
            return f"{title}\n  (empty)"
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

        def line(cells):
            return " | ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

        rule = "-+-".join("-" * w for w in widths)
        return "\n".join([title, line(headers), rule] + [line(r) for r in rows])

    def symbol_table_text(self):
        return self._render("Symbol Table", self.SYMBOL_HEADERS, self.symbol_rows())

    def structure_table_text(self):
        return self._render("Structure Table", self.STRUCT_HEADERS, self.structure_rows())
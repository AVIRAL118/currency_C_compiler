"""
symbol_table.py - Symbol Table and Type definitions for the Currency-C language.
"""

class RedeclarationError(Exception):
    def __init__(self, previous_symbol):
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

    def pointer_to(self):
        return Type(self.base_type, self.currency_code, self.struct_name, self.pointer_level + 1)

    def deref(self):
        return Type(self.base_type, self.currency_code, self.struct_name, self.pointer_level - 1)

    def __eq__(self, other):
        if not isinstance(other, Type):
            return False
        return (self.base_type == other.base_type and
                self.currency_code == other.currency_code and
                self.struct_name == other.struct_name and
                self.pointer_level == other.pointer_level)

    def __str__(self):
        if self.is_error:
            return "error"
        
        ptr = "*" * self.pointer_level
        if self.is_currency:
            return f"currency<{self.currency_code}>{ptr}"
        if self.is_struct:
            return f"struct {self.struct_name}{ptr}"
        return f"{self.base_type}{ptr}"


class Symbol:
    def __init__(self, name, type_obj, line_number):
        self.name = name
        self.type = type_obj
        self.line_number = line_number


class SymbolTable:
    def __init__(self):
        self.scopes = [{}]
        self.structs = {}

    def enter_scope(self):
        self.scopes.append({})

    def exit_scope(self):
        self.scopes.pop()

    def declare(self, name, type_obj, line_number):
        current_scope = self.scopes[-1]
        if name in current_scope:
            raise RedeclarationError(current_scope[name])
        sym = Symbol(name, type_obj, line_number)
        current_scope[name] = sym
        return sym

    def lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return None

    def declare_struct(self, name, members, line_number):
        if name in self.structs:
            prev_line = self.structs[name]['_line']
            raise RedeclarationError(Symbol(name, None, prev_line))
        
        self.structs[name] = members
        self.structs[name]['_line'] = line_number

    def lookup_struct(self, name):
        return self.structs.get(name)

    def lookup_member(self, struct_name, member_name):
        struct_members = self.lookup_struct(struct_name)
        if struct_members and member_name in struct_members:
            return struct_members[member_name]
        return None
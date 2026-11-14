"""Incremental decoding of one top-level string field from streamed JSON text."""

_HEX = frozenset('0123456789abcdefABCDEF')
_ESCAPES = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}


class AnswerFieldDecoder:
    """Feed streamed JSON text; each call returns only the field text decoded since the last.

    Only the named key of the outermost object counts; same-named keys in nested values
    and key-like text inside strings are ignored. Decoding stops at the end of the value,
    or at the first malformed escape, so a draft never shows text the parser rejects.
    """

    def __init__(self, field):
        self.field = field
        self.stack = []
        self.in_string = False
        self.string_role = None
        self.escape = None
        self.key = []
        self.last_key = None
        self.expect_key = False
        self.target_next = False
        self.high_surrogate = None
        self.done = False

    def feed(self, text):
        out = []
        for character in text:
            if self.done:
                break
            if self.in_string:
                self._string_character(character, out)
            else:
                self._structural_character(character)
        return ''.join(out)

    def _structural_character(self, character):
        top = len(self.stack) == 1 and self.stack[0] == '{'
        if character == '"':
            self.in_string = True
            if top and self.expect_key:
                self.string_role = 'key'
                self.key = []
            elif top and self.target_next:
                self.string_role = 'target'
            else:
                self.string_role = 'other'
            self.target_next = False
        elif character in '{[':
            self.stack.append(character)
            self.expect_key = character == '{' and len(self.stack) == 1
            self.target_next = False
        elif character in '}]':
            if self.stack:
                self.stack.pop()
            if len(self.stack) == 1 and self.stack[0] == '{':
                self.expect_key = False
        elif character == ',' and top:
            self.expect_key = True
            self.target_next = False
        elif character == ':' and top and self.last_key is not None:
            self.target_next = self.last_key == self.field
            self.last_key = None
        elif not character.isspace():
            self.target_next = False

    def _string_character(self, character, out):
        if self.string_role != 'target':
            if self.escape is not None:
                self.escape = None
                if self.string_role == 'key':
                    self.key.append(character)
            elif character == '\\':
                self.escape = ''
            elif character == '"':
                self.in_string = False
                if self.string_role == 'key':
                    self.last_key = ''.join(self.key)
                    self.expect_key = False
            elif self.string_role == 'key':
                self.key.append(character)
            return
        if self.escape is None:
            if character == '\\':
                self.escape = ''
            elif character == '"':
                self._flush_surrogate(out)
                self.in_string = False
                self.done = True
            else:
                self._flush_surrogate(out)
                out.append(character)
            return
        self.escape += character
        if self.escape[0] != 'u':
            value = _ESCAPES.get(self.escape)
            self.escape = None
            if value is None:
                self.done = True
                return
            self._flush_surrogate(out)
            out.append(value)
            return
        if len(self.escape) < 5:
            return
        digits, self.escape = self.escape[1:], None
        if not all(digit in _HEX for digit in digits):
            self.done = True
            return
        code = int(digits, 16)
        if 0xD800 <= code <= 0xDBFF:
            self._flush_surrogate(out)
            self.high_surrogate = code
        elif 0xDC00 <= code <= 0xDFFF and self.high_surrogate is not None:
            out.append(chr(0x10000 + ((self.high_surrogate - 0xD800) << 10) + (code - 0xDC00)))
            self.high_surrogate = None
        else:
            self._flush_surrogate(out)
            out.append('\ufffd' if 0xDC00 <= code <= 0xDFFF else chr(code))

    def _flush_surrogate(self, out):
        if self.high_surrogate is not None:
            out.append('\ufffd')
            self.high_surrogate = None

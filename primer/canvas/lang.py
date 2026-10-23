"""Canvas Lang: the one-statement-per-line shorthand agents use to draw on a canvas.

This module is syntax only (design 8.4, research report d.5). It turns program
text into statements, and values back into canonical literals. It knows nothing
about element types, properties or limits: ``primer.canvas.catalog`` and
``primer.canvas.state`` give a statement its meaning. No I/O.

One statement per line (only a triple-quoted string may span lines)::

    id = Type(args)        create or replace the element id
    id.prop = value        set one property (null removes it)
    id += [ids]            append children to a container
    del id, id             remove elements and their subtrees
    clear                  remove everything but an empty root
    # comment              a comment, outside strings

Values are double- or single-quoted strings with JSON escapes, triple-quoted
strings taken verbatim, numbers, ``true``, ``false``, ``null``, lists
(``[a, b]``; a bare id inside a list is a :class:`Ref`) and, so that every value
an Html element can report round-trips, JSON-style objects (``{"k": 1}``).
``...`` as a whole argument or property value is :data:`KEEP`: keep the current
value. The decisions in the Pivot P5 plan (Decision 4) are the ones above.

The parser is hand-written: a splitter that cuts the program into logical lines
while respecting strings and comments, then a recursive-descent parser over one
line. It never applies a regular expression to a whole statement.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

IDENT_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
ROOT_ID = "root"

#: Words that mean a value in the language. An element cannot be named by one,
#: or ``[true]`` would be a boolean and the element could never be referenced.
RESERVED_IDS = frozenset({"true", "false", "null"})

#: Deepest nesting of lists and objects a program may use.
MAX_DEPTH = 32
#: Syntax errors reported before the rest are only counted.
MAX_ERRORS = 50
#: Most statements (or JSON ops) one batch may hold.
MAX_BATCH_STATEMENTS = 2000
#: Most digits in an integer a program may write; a longer one is "number is out of range".
MAX_INT_DIGITS = 15
#: The same limit as a magnitude: the largest integer, and negated the smallest, a program may write.
MAX_INT = 10**MAX_INT_DIGITS - 1
#: The same limit for a float VALUE (``state.check_json_value``): a float of this magnitude or more is refused. Postgres JSONB stores a number
#: as a numeric and reads a float of magnitude 1e16 or more back as an int, so a stored value past this would not round-trip (#790 review).
MAX_FLOAT = float(10**MAX_INT_DIGITS)

_CLIP = 32

_ID_RULE = "a lowercase letter, then lowercase letters, digits or _, at most 32"
_PROP_RULE = "a lowercase letter, then lowercase letters, digits or _"

# Exact messages (the plan's list). Everything else this module can say is
# built by the _Stmt methods below, in the same "expected X, found: Y" voice.
MSG_UNTERMINATED = "unterminated string"
MSG_BARE_ID = 'a bare id is only allowed inside a children list; quote text as "..."'
MSG_POSITIONAL_AFTER_NAMED = "positional argument after a named one"
MSG_NOT_A_STATEMENT = (
    "not a statement: expected id = Type(...), id.prop = value, id += [ids], "
    "del ids or clear"
)
MSG_TRAILING = "unexpected text after the statement: "
MSG_KEEP = "... keeps a current value and must be a whole argument"

MSG_MISSING_PAREN = "missing ) to close the arguments; each statement must fit on one line"
MSG_MISSING_BRACKET = "missing ] to close the list; each statement must fit on one line"
MSG_MISSING_BRACE = "missing } to close the object; each statement must fit on one line"
MSG_APPEND = "+= takes a list of element ids written without quotes: id += [a, b]"
MSG_TOO_DEEP = f"lists and objects are nested more than {MAX_DEPTH} levels deep"
MSG_NUMBER_RANGE = "number is out of range"
_ESCAPE_HINT = 'write \\\\ for a backslash, or use a """...""" string, which is taken as it is'

_WORD_RE = re.compile(r"[A-Za-z0-9_\-]+")
_TYPE_RE = re.compile(r"[A-Z][A-Za-z]*")
_PROP_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_NUM_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_NEXT_SPECIAL = re.compile(r"[\"'#\n]")
_STR_STOPS = {
    '"': re.compile(r'["\\\n]'),
    "'": re.compile(r"['\\\n]"),
}
_ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_HEX = frozenset("0123456789abcdefABCDEF")
_DIGITS = frozenset("0123456789")
_ALPHA_ = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")


class Keep:
    """The ``...`` value: keep whatever the element has now. A singleton."""

    __slots__ = ()
    _instance: Keep | None = None

    def __new__(cls) -> Keep:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "..."

    def __copy__(self) -> Keep:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Keep:
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        return (Keep, ())


KEEP = Keep()


@dataclass(frozen=True)
class Ref:
    """A bare id inside a list: a reference to another element."""

    id: str


@dataclass(frozen=True)
class Assign:
    """``id = Type(args)``: create or replace an element."""

    line: int
    id: str
    type: str
    args: tuple[Any, ...]
    kwargs: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class SetProp:
    """``id.prop = value``: set one property; ``null`` removes it."""

    line: int
    id: str
    prop: str
    value: Any


@dataclass(frozen=True)
class Append:
    """``id += [ids]``: append children to a container."""

    line: int
    id: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class Delete:
    """``del ids``: remove elements and their subtrees."""

    line: int
    ids: tuple[str, ...]


@dataclass(frozen=True)
class Clear:
    """``clear``: remove everything but an empty root."""

    line: int


Statement = Assign | SetProp | Append | Delete | Clear


@dataclass(frozen=True)
class LangError:
    """One problem, with the line (or op) it is on and, often, what was expected."""

    line: int
    message: str
    expected: str | None = None
    unit: Literal["line", "op"] = "line"

    def render(self) -> str:
        text = f"{self.unit} {self.line}: {self.message}"
        if self.expected:
            text += f" ({self.expected})"
        return text


class CanvasLangError(ValueError):
    """A program with syntax errors. ``errors`` lists every one of them."""

    def __init__(self, errors: list[LangError]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(e.render() for e in self.errors))

    def __reduce__(self) -> tuple[Any, ...]:
        return (CanvasLangError, (self.errors,))


def clip(word: str) -> str:
    """``word`` cut to a few characters: a message says which text the agent sent, it does not echo all of it."""
    return word if len(word) <= _CLIP else word[:_CLIP] + "..."


def id_problem(word: str) -> str | None:
    """Why ``word`` cannot be an element id, or None when it can."""
    if not isinstance(word, str):
        return f"{clip(repr(word))} is not an element id ({_ID_RULE})"
    if word in RESERVED_IDS:
        return f"'{word}' is reserved (it means a value); pick another element id"
    if IDENT_RE.match(word) is None:
        return f"'{clip(word)}' is not an element id ({_ID_RULE})"
    return None


# --- splitting a program into logical lines -----------------------------------


def _triple_close(text: str, start: int) -> int | None:
    """Index just after the closing triple quote of the string opened at ``start``.

    The closing delimiter is the last three quotes of the first run of three or
    more, so a body may end with a quote character.
    """
    j = text.find('"""', start + 3)
    if j < 0:
        return None
    n = len(text)
    while j + 3 < n and text[j + 3] == '"':
        j += 1
    return j + 3


def _skip_quoted(text: str, start: int, quote: str) -> int:
    """Index after the single-line string opened at ``start`` (or where it breaks off)."""
    n = len(text)
    stop = _STR_STOPS[quote]
    j = start + 1
    while True:
        m = stop.search(text, j)
        if m is None:
            return n
        k = m.start()
        c = text[k]
        if c == quote:
            return k + 1
        if c == "\n":
            return k
        if k + 1 >= n or text[k + 1] == "\n":
            return min(k + 1, n)
        j = k + 2


def _logical_end(text: str, pos: int) -> int:
    """Index of the newline that ends the statement starting at ``pos`` (or len)."""
    n = len(text)
    i = pos
    while True:
        m = _NEXT_SPECIAL.search(text, i)
        if m is None:
            return n
        i = m.start()
        c = text[i]
        if c == "\n":
            return i
        if c == "#":
            j = text.find("\n", i)
            return n if j < 0 else j
        if c == '"' and text.startswith('"""', i):
            end = _triple_close(text, i)
            if end is None:
                return n
            i = end
            continue
        i = _skip_quoted(text, i, c)


# --- parsing one logical line -------------------------------------------------


class _Fail(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Stmt:
    """Recursive-descent parser over one logical line."""

    __slots__ = ("i", "line", "n", "s")

    def __init__(self, s: str, line: int) -> None:
        self.s = s
        self.n = len(s)
        self.i = 0
        self.line = line

    # tokens

    def _ws(self) -> None:
        """Skip spaces; a comment swallows the rest of the line."""
        s, n, i = self.s, self.n, self.i
        while i < n:
            c = s[i]
            if c == "#":
                i = n
                break
            if c == "\n" or not c.isspace():
                break
            i += 1
        self.i = i

    def _peek(self) -> str:
        return self.s[self.i] if self.i < self.n else ""

    def _snippet(self) -> str:
        return self.s[self.i : self.i + 20].replace("\n", " ")

    def _found(self) -> str:
        return f", found: {self._snippet()}" if self.i < self.n else ""

    def _id(self, word: str) -> str:
        problem = id_problem(word)
        if problem:
            raise _Fail(problem)
        return word

    def _end(self) -> None:
        self._ws()
        if self.i < self.n:
            raise _Fail(MSG_TRAILING + self._snippet())

    # statements

    def parse(self) -> Statement:
        s = self.s
        m = _WORD_RE.match(s, self.i)
        word = m.group() if m else ""
        if m:
            self.i = m.end()
        after_word = self.i
        self._ws()
        spaced = self.i > after_word
        c = self._peek()
        if word == "clear" and c == "":
            return Clear(self.line)
        if word == "del" and spaced and c not in ("", "=", ".", "+"):
            return self._delete()
        if word and c == "=" and not s.startswith("==", self.i):
            self._id(word)
            self.i += 1
            return self._assign(word)
        if word and c == ".":
            self._id(word)
            self.i += 1
            return self._set_prop(word)
        if word and s.startswith("+=", self.i):
            self._id(word)
            self.i += 2
            return self._append(word)
        if word == "clear":
            raise _Fail(MSG_TRAILING + self._snippet())
        raise _Fail(MSG_NOT_A_STATEMENT)

    def _assign(self, id_: str) -> Assign:
        self._ws()
        m = _TYPE_RE.match(self.s, self.i)
        if m is None:
            raise _Fail(MSG_NOT_A_STATEMENT)
        type_name = m.group()
        self.i = m.end()
        self._ws()
        if self._peek() != "(":
            raise _Fail(MSG_NOT_A_STATEMENT)
        self.i += 1
        args, kwargs = self._args()
        self._end()
        return Assign(self.line, id_, type_name, tuple(args), tuple(kwargs))

    def _set_prop(self, id_: str) -> SetProp:
        m = _WORD_RE.match(self.s, self.i)
        if m is None:
            raise _Fail(MSG_NOT_A_STATEMENT)
        prop = m.group()
        if _PROP_RE.match(prop) is None:
            raise _Fail(f"'{clip(prop)}' is not a property name ({_PROP_RULE})")
        self.i = m.end()
        self._ws()
        if self._peek() != "=" or self.s.startswith("==", self.i):
            raise _Fail(MSG_NOT_A_STATEMENT)
        self.i += 1
        value = self._value(allow_keep=True)
        self._end()
        return SetProp(self.line, id_, prop, value)

    def _append(self, id_: str) -> Append:
        self._ws()
        if self._peek() != "[":
            raise _Fail(MSG_APPEND)
        items = self._list(1)
        if not all(isinstance(item, Ref) for item in items):
            raise _Fail(MSG_APPEND)
        self._end()
        return Append(self.line, id_, tuple(item.id for item in items))

    def _delete(self) -> Delete:
        ids: list[str] = []
        while True:
            self._ws()
            m = _WORD_RE.match(self.s, self.i)
            if m is None:
                raise _Fail("expected an element id" + self._found())
            self.i = m.end()
            ids.append(self._id(m.group()))
            self._ws()
            if self._peek() == ",":
                self.i += 1
                continue
            break
        self._end()
        return Delete(self.line, tuple(ids))

    # arguments

    def _args(self) -> tuple[list[Any], list[tuple[str, Any]]]:
        args: list[Any] = []
        kwargs: list[tuple[str, Any]] = []
        self._ws()
        if self._peek() == ")":
            self.i += 1
            return args, kwargs
        while True:
            self._ws()
            if self.i >= self.n:
                raise _Fail(MSG_MISSING_PAREN)
            name = self._named()
            if name is not None:
                kwargs.append((name, self._value(allow_keep=True)))
            else:
                if kwargs:
                    raise _Fail(MSG_POSITIONAL_AFTER_NAMED)
                args.append(self._value(allow_keep=True))
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                self._ws()
                if self._peek() == ")":
                    self.i += 1
                    return args, kwargs
                continue
            if c == ")":
                self.i += 1
                return args, kwargs
            if c == "":
                raise _Fail(MSG_MISSING_PAREN)
            raise _Fail("expected , or ) after the argument" + self._found())

    def _named(self) -> str | None:
        """Consume ``name =`` and return the name, or leave the position alone."""
        m = _WORD_RE.match(self.s, self.i)
        if m is None:
            return None
        s, n = self.s, self.n
        j = m.end()
        while j < n and s[j].isspace() and s[j] != "\n":
            j += 1
        if j >= n or s[j] != "=" or s.startswith("==", j):
            return None
        name = m.group()
        if _PROP_RE.match(name) is None:
            raise _Fail(f"'{clip(name)}' is not a property name ({_PROP_RULE})")
        self.i = j + 1
        return name

    # values

    def _value(self, *, allow_keep: bool, in_list: bool = False, depth: int = 0) -> Any:
        self._ws()
        s = self.s
        c = self._peek()
        if c == "":
            raise _Fail("expected a value")
        if c == '"' or c == "'":
            return self._string()
        if c == "[":
            return self._list(depth + 1)
        if c == "{":
            return self._object(depth + 1)
        if c in _DIGITS or (c == "-" and self.i + 1 < self.n and s[self.i + 1] in _DIGITS):
            return self._number()
        if s.startswith("...", self.i):
            if not allow_keep:
                raise _Fail(MSG_KEEP)
            self.i += 3
            self._ws()
            if self._peek() not in ("", ",", ")"):
                raise _Fail(MSG_KEEP)
            return KEEP
        if c in _ALPHA_:
            m = _WORD_RE.match(s, self.i)
            assert m is not None
            word = m.group()
            self.i = m.end()
            if word == "true":
                return True
            if word == "false":
                return False
            if word == "null":
                return None
            j = self.i
            while j < self.n and s[j].isspace() and s[j] != "\n":
                j += 1
            if j < self.n and s[j] == "(":
                raise _Fail(
                    f"elements cannot be nested; give the inner {word} its own line "
                    f"(id = {word}(...)) and list its id in the parent's children"
                )
            if in_list:
                return Ref(self._id(word))
            raise _Fail(MSG_BARE_ID)
        raise _Fail("expected a value" + self._found())

    def _number(self) -> int | float:
        m = _NUM_RE.match(self.s, self.i)
        assert m is not None
        text = m.group()
        self.i = m.end()
        if m.group(1) is None and m.group(2) is None:
            if len(text.lstrip("-")) > MAX_INT_DIGITS:
                raise _Fail(MSG_NUMBER_RANGE)
            return int(text)
        number = float(text)
        if not math.isfinite(number):
            raise _Fail(MSG_NUMBER_RANGE)
        return number

    def _string(self) -> str:
        s, n = self.s, self.n
        start = self.i
        quote = s[start]
        if quote == '"' and s.startswith('"""', start):
            end = _triple_close(s, start)
            if end is None:
                raise _Fail(MSG_UNTERMINATED)
            self.i = end
            return s[start + 3 : end - 3]
        stop = _STR_STOPS[quote]
        parts: list[str] = []
        j = start + 1
        while True:
            m = stop.search(s, j)
            if m is None:
                raise _Fail(MSG_UNTERMINATED)
            k = m.start()
            c = s[k]
            parts.append(s[j:k])
            if c == quote:
                self.i = k + 1
                return "".join(parts)
            if c == "\n" or k + 1 >= n or s[k + 1] == "\n":
                raise _Fail(MSG_UNTERMINATED)
            escape = s[k + 1]
            if escape in _ESCAPES:
                parts.append(_ESCAPES[escape])
                j = k + 2
            elif escape == "u":
                j = self._unicode_escape(k, parts)
            else:
                raise _Fail(self._bad_escape(f"\\{escape}"))

    def _bad_escape(self, text: str) -> str:
        return f"invalid escape {text} in a string; {_ESCAPE_HINT}"

    def _unicode_escape(self, k: int, parts: list[str]) -> int:
        """Decode ``\\uXXXX`` (and a surrogate pair) at ``k``; return the next index."""
        s = self.s
        digits = s[k + 2 : k + 6]
        if len(digits) != 4 or not all(h in _HEX for h in digits):
            raise _Fail(self._bad_escape("\\u" + digits))
        code = int(digits, 16)
        j = k + 6
        if 0xD800 <= code <= 0xDBFF:
            low = s[j + 2 : j + 6] if s.startswith("\\u", j) else ""
            if len(low) == 4 and all(h in _HEX for h in low) and 0xDC00 <= int(low, 16) <= 0xDFFF:
                code = 0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00)
                j += 6
            else:
                raise _Fail(self._bad_escape("\\u" + digits))
        elif 0xDC00 <= code <= 0xDFFF:
            raise _Fail(self._bad_escape("\\u" + digits))
        parts.append(chr(code))
        return j

    def _list(self, depth: int) -> list[Any]:
        if depth > MAX_DEPTH:
            raise _Fail(MSG_TOO_DEEP)
        self.i += 1  # [
        items: list[Any] = []
        self._ws()
        if self._peek() == "]":
            self.i += 1
            return items
        while True:
            self._ws()
            if self.i >= self.n:
                raise _Fail(MSG_MISSING_BRACKET)
            items.append(self._value(allow_keep=False, in_list=True, depth=depth))
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                self._ws()
                if self._peek() == "]":
                    self.i += 1
                    return items
                continue
            if c == "]":
                self.i += 1
                return items
            if c == "":
                raise _Fail(MSG_MISSING_BRACKET)
            raise _Fail("expected , or ] in the list" + self._found())

    def _object(self, depth: int) -> dict[str, Any]:
        if depth > MAX_DEPTH:
            raise _Fail(MSG_TOO_DEEP)
        self.i += 1  # {
        out: dict[str, Any] = {}
        self._ws()
        if self._peek() == "}":
            self.i += 1
            return out
        while True:
            self._ws()
            c = self._peek()
            if c == "":
                raise _Fail(MSG_MISSING_BRACE)
            if c != '"' and c != "'":
                raise _Fail("object keys must be strings in quotes" + self._found())
            key = self._string()
            self._ws()
            if self._peek() != ":":
                raise _Fail("expected : after the object key" + self._found())
            self.i += 1
            out[key] = self._value(allow_keep=False, depth=depth)
            self._ws()
            c = self._peek()
            if c == ",":
                self.i += 1
                self._ws()
                if self._peek() == "}":
                    self.i += 1
                    return out
                continue
            if c == "}":
                self.i += 1
                return out
            if c == "":
                raise _Fail(MSG_MISSING_BRACE)
            raise _Fail("expected , or } in the object" + self._found())


def _normalize(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def parse_program(text: str) -> list[Statement]:
    """Parse a program into statements.

    Raises :class:`CanvasLangError` carrying every syntax error: one per bad
    statement, with parsing resuming on the next line. A statement's ``line`` is
    the line it starts on.
    """
    text = _normalize(text)
    n = len(text)
    spans: list[tuple[int, int, int]] = []
    pos = 0
    line = 1
    while pos < n:
        j = pos
        while j < n and text[j] != "\n" and text[j].isspace():
            j += 1
        if j >= n:
            break
        c = text[j]
        if c == "\n":
            pos = j + 1
            line += 1
            continue
        if c == "#":
            k = text.find("\n", j)
            if k < 0:
                break
            pos = k + 1
            line += 1
            continue
        end = _logical_end(text, j)
        spans.append((j, end, line))
        line += text.count("\n", j, end) + 1
        pos = end + 1
    if len(spans) > MAX_BATCH_STATEMENTS:
        raise CanvasLangError([
            LangError(
                spans[MAX_BATCH_STATEMENTS][2],
                f"a batch holds at most {MAX_BATCH_STATEMENTS} statements; got {len(spans)}",
            )
        ])
    statements: list[Statement] = []
    errors: list[LangError] = []
    dropped = 0
    dropped_line = 0
    for j, end, stmt_line in spans:
        try:
            statements.append(_Stmt(text[j:end], stmt_line).parse())
        except _Fail as fail:
            if len(errors) < MAX_ERRORS:
                errors.append(LangError(stmt_line, fail.message))
            else:
                if not dropped:
                    dropped_line = stmt_line
                dropped += 1
    if dropped:
        errors.append(LangError(dropped_line, f"{dropped} more syntax errors are not shown"))
    if errors:
        raise CanvasLangError(errors)
    return statements


# --- canonical literals ---------------------------------------------------------


def format_value(value: Any) -> str:
    """The canonical literal for ``value`` (the form ``canvas_get`` prints)."""
    return _format(value, 0)


def _format(value: Any, depth: int) -> str:
    """``depth`` is how many lists and objects enclose ``value``; the parser reads at most MAX_DEPTH of them."""
    if value is KEEP:
        return "..."
    if isinstance(value, Ref):
        return value.id
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("cannot write a non-finite number")
        return repr(value)
    if isinstance(value, (list, tuple)):
        if depth >= MAX_DEPTH:
            raise ValueError(MSG_TOO_DEEP)
        return "[" + ", ".join(_format(v, depth + 1) for v in value) + "]"
    if isinstance(value, dict):
        if depth >= MAX_DEPTH:
            raise ValueError(MSG_TOO_DEEP)
        pairs = (
            f"{json.dumps(str(k), ensure_ascii=False)}: {_format(v, depth + 1)}"
            for k, v in value.items()
        )
        return "{" + ", ".join(pairs) + "}"
    raise TypeError(f"cannot write a {type(value).__name__} as a Canvas Lang value")


__all__ = [
    "IDENT_RE",
    "KEEP",
    "MAX_BATCH_STATEMENTS",
    "MAX_DEPTH",
    "MAX_ERRORS",
    "MAX_FLOAT",
    "MAX_INT",
    "MAX_INT_DIGITS",
    "RESERVED_IDS",
    "ROOT_ID",
    "Append",
    "Assign",
    "CanvasLangError",
    "Clear",
    "Delete",
    "Keep",
    "LangError",
    "Ref",
    "SetProp",
    "Statement",
    "clip",
    "format_value",
    "id_problem",
    "parse_program",
]

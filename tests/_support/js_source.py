"""A small lexer for the static scans over ``ui/**/*.js`` and ``*.jsx`` (a source pin cannot parse JSX, so it reads the text with the non-code taken out).

``blank(src)`` returns the same text with every comment replaced by spaces, so a call or a property in a comment is not code. With ``strings=True`` the inside of
every ``'...'`` and ``"..."`` string is blanked as well, and with ``templates=True`` the static text of every template literal, while the ``${...}`` placeholders of
a template stay as code (they are: ``title={`Save (${name}) failed (${err.code})`}``). The body of a regular-expression literal is blanked with the strings
(``strings=True``), because a quote or a backtick inside one is not the start of a string or a template. Newlines are kept and nothing is removed, so an offset in the
result is the same offset in the source and a line number is ``src.count("\\n", 0, offset) + 1``.

It is a lexer, not a parser, and two guesses are made. (1) A ``/`` starts a regular-expression literal when the last significant character before it is one of
``( , = : [ ! & | ? { } ;``, or the last word is ``return``, ``typeof``, ``case``, ``of``, ``in`` (and ``void``, ``delete``, ``throw``, ``else``, ``do``, ``yield``, ``await``,
``instanceof``), or there is nothing before it; a ``/`` followed by ``>`` (a JSX self-closing tag after a brace) never does, and a literal that does not close on its own line is not
one (a regex literal cannot span lines), so a wrong guess costs one line at most. Division and JSX text (``and/or``, ``</p>``) are left alone. (2) A quote in JSX text
(``Don't``) opens a string: a ' or " string ends at the end of its line (a JavaScript string cannot span lines), which limits what such a quote can swallow to the rest of that line.
A template literal does span lines, so a stray backtick outside one would not be told apart; no file under ``ui/`` has that today. What a wrong guess cannot hide is a lexer that
LOST ITS PLACE: ``blank`` raises ``ValueError`` when the text ends inside a template literal, a ``${...}`` placeholder or a string, and the scans run it over every component file
(``tests/ui/test_refusal_reader.py``), so a file the lexer cannot follow fails by name instead of quietly reporting fewer sites.
"""

from __future__ import annotations

_OPEN = "([{"
_CLOSE = ")]}"

# What may stand before a regular-expression literal: these characters, or one of these words, or nothing at all.
_REGEX_AFTER_CHARS = frozenset("(,=:[!&|?{};")
_REGEX_AFTER_WORDS = frozenset({"return", "typeof", "case", "of", "in", "void", "delete", "throw", "else", "do", "yield", "await", "instanceof"})


def _word_before(src: str, i: int) -> str:
    j = i - 1
    while j >= 0 and src[j].isspace():
        j -= 1
    k = j
    while k >= 0 and (src[k].isalnum() or src[k] in "_$"):
        k -= 1
    return src[k + 1 : j + 1]


def _regex_end(src: str, i: int) -> int:
    """The offset just past a regular-expression literal that starts at ``i`` (a ``/``), or -1 when there is no closing ``/`` on the line (it is not one)."""
    n = len(src)
    j = i + 1
    in_class = False
    while j < n:
        ch = src[j]
        if ch == "\n":
            return -1
        if ch == "\\":
            if j + 1 < n and src[j + 1] == "\n":
                return -1
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return j + 1
        j += 1
    return -1


def blank(src: str, *, strings: bool = False, templates: bool = False) -> str:
    out = list(src)
    n = len(src)

    def wipe(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def line(offset: int) -> int:
        return src.count("\n", 0, offset) + 1

    # One frame per open construct: ["code", open braces, offset] for the file and for each `${...}` placeholder, ["tpl", offset] for the static text of a template literal.
    stack: list[list] = [["code", 0, 0]]
    last = ""          # the last significant character of the code so far ("" at the start of a code frame: a regex may begin there)
    i = 0
    while i < n:
        frame = stack[-1]
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if frame[0] == "code":
            if c == "/" and nxt == "/":
                j = src.find("\n", i)
                j = n if j < 0 else j
                wipe(i, j)
                i = j
            elif c == "/" and nxt == "*":
                j = src.find("*/", i + 2)
                j = n if j < 0 else j + 2
                wipe(i, j)
                i = j
            elif c == "/" and nxt != ">" and (last == "" or last in _REGEX_AFTER_CHARS or (last.isalnum() or last in "_$") and _word_before(src, i) in _REGEX_AFTER_WORDS):
                end = _regex_end(src, i)
                if end < 0:
                    last = c
                    i += 1
                else:
                    if strings:
                        wipe(i + 1, end - 1)
                    while end < n and src[end].isalpha():          # the flags
                        end += 1
                    last = "x"                                         # after a regex literal a slash divides
                    i = end
            elif c in "'\"":
                j = i + 1
                while j < n and src[j] != c and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                if j >= n:
                    raise ValueError(f"unterminated string literal opened on line {line(i)}: the text ends inside it")
                if strings:
                    wipe(i + 1, j)
                last = c
                i = j + 1
            elif c == "`":
                stack.append(["tpl", i])
                last = "`"
                i += 1
            elif c == "{":
                frame[1] += 1
                last = c
                i += 1
            elif c == "}":
                if frame[1] == 0 and len(stack) > 1:
                    stack.pop()  # the placeholder ends: back in the static text of its template
                else:
                    frame[1] = max(0, frame[1] - 1)
                    last = c
                i += 1
            else:
                if not c.isspace():
                    last = c
                i += 1
        else:
            if c == "\\":
                if templates:
                    wipe(i, i + 2)
                i += 2
            elif c == "`":
                stack.pop()
                last = "`"
                i += 1
            elif c == "$" and nxt == "{":
                stack.append(["code", 0, i])
                last = ""
                i += 2
            else:
                if templates:
                    wipe(i, i + 1)
                i += 1
    if len(stack) > 1:
        raise ValueError(f"unterminated template literal or placeholder opened on line {line(stack[1][-1])}: the text ends inside it")
    return "".join(out)


def close_of(text: str, open_at: int) -> int:
    """The offset just past the bracket that closes the one at ``open_at`` (-1 when the text ends first). ``text`` should have its strings blanked."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] in _OPEN:
            depth += 1
        elif text[i] in _CLOSE:
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def value_end(text: str, start: int) -> int:
    """Where the value that begins at ``start`` ends: at a comma or semicolon outside any bracket, or at the bracket that closes the group it sits in."""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                return i
            depth -= 1
        elif ch in ",;" and depth == 0:
            return i
    return len(text)


def line_of(src: str, offset: int) -> int:
    return src.count("\n", 0, offset) + 1

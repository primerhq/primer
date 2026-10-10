"""A small lexer for the static scans over ``ui/**/*.js`` and ``*.jsx`` (a source pin cannot parse JSX, so it reads the text with the non-code taken out).

``blank(src)`` returns the same text with every comment replaced by spaces, so a call or a property in a comment is not code. With ``strings=True`` the inside of
every ``'...'`` and ``"..."`` string is blanked as well, and with ``templates=True`` the static text of every template literal, while the ``${...}`` placeholders of
a template stay as code (they are: ``title={`Save (${name}) failed (${err.code})`}`). Newlines are kept and nothing is removed, so an offset in the result is the
same offset in the source and a line number is ``src.count("\\n", 0, offset) + 1``.

It is a lexer, not a parser: a regular-expression literal is not told from division and a quote in JSX text (``Don't``) opens a string. A ' or " string therefore
ends at the end of its line (a JavaScript string cannot span lines), which limits what such a quote can swallow to the rest of that line; a template literal does
span lines, and a stray backtick outside one would not be told apart. No file under ``ui/`` has that today; the scans that use this say how many sites they saw, so
a lexer gone blind fails them instead of passing them.
"""

from __future__ import annotations

_OPEN = "([{"
_CLOSE = ")]}"


def blank(src: str, *, strings: bool = False, templates: bool = False) -> str:
    out = list(src)
    n = len(src)

    def wipe(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    # One frame per open construct: ["code", open braces] for the file and for each `${...}` placeholder, ["tpl"] for the static text of a template literal.
    stack: list[list] = [["code", 0]]
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
            elif c in "'\"":
                j = i + 1
                while j < n and src[j] != c and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                if strings:
                    wipe(i + 1, j)
                i = j + 1
            elif c == "`":
                stack.append(["tpl"])
                i += 1
            elif c == "{":
                frame[1] += 1
                i += 1
            elif c == "}":
                if frame[1] == 0 and len(stack) > 1:
                    stack.pop()  # the placeholder ends: back in the static text of its template
                else:
                    frame[1] = max(0, frame[1] - 1)
                i += 1
            else:
                i += 1
        else:
            if c == "\\":
                if templates:
                    wipe(i, i + 2)
                i += 2
            elif c == "`":
                stack.pop()
                i += 1
            elif c == "$" and nxt == "{":
                stack.append(["code", 0])
                i += 2
            else:
                if templates:
                    wipe(i, i + 1)
                i += 1
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

"""A seeded generator of mostly valid Canvas Lang programs that knows the current canvas.

Property tests drive ``apply_batch`` (and the decompiler) with it: most of what it
writes is accepted, a little is deliberately wrong, and the same seed always
writes the same programs.
"""

from __future__ import annotations

import json
import random
from typing import Any

from primer.canvas.catalog import CONTAINER_TYPES
from primer.canvas.state import CanvasState

WORDS = ["Jazz", "Lo-fi", "Classical", "Play", "Skip", "Hello", "café \U0001F600", 'say "hi"', "it's", "a # b", "x\ny"]
OPTIONS = ["Jazz", "Lo-fi", "Classical", "Blues"]
IDS = [f"n{i}" for i in range(12)]
LEAF_TYPES = ["Divider", "Text", "Button", "Select", "Image", "Audio", "Link", "Html"]
GARBAGE = [
    "root = Text('a')",
    "del root",
    "n1.value = 5",
    'n2 = Select("a", [])',
    "n3 = Image(...)",
    "n4.children = [n4]",
    "n5 += [root]",
    "n6 = Column([n6])",
]
UNPARSEABLE = "x y z"
#: Html values at the edge of what the language reads: its widest integers, its deepest nesting, extreme floats.
EDGE_VALUES = [
    "999999999999999", "-999999999999999", "[" * 32 + "]" * 32, "1e300", "-0.0", "5e-324",
    '{"a": {"b": [1e300, -0.0, 5e-324]}}',
]


def q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def nested(levels: int, kinds: str = "l") -> Any:
    """A value nested ``levels`` deep around an empty list or object.

    ``kinds`` says, from the inside out, whether each level is a list ("l") or an object ("o"), and repeats:
    ``nested(2)`` is ``[[]]``, ``nested(2, "o")`` is ``{"k": {}}``, ``nested(2, "lo")`` is ``{"k": []}``.
    """
    value: Any = [] if kinds[0] == "l" else {}
    for level in range(1, levels):
        value = [value] if kinds[level % len(kinds)] == "l" else {"k": value}
    return value


def leaf_call(r: random.Random, kind: str) -> str:
    if kind == "Divider":
        return "Divider()"
    if kind == "Text":
        style = f", {q(r.choice(['h1', 'h2', 'h3', 'body', 'caption', 'code']))}" if r.random() < 0.5 else ""
        return f"Text({q(r.choice(WORDS))}{style})"
    if kind == "Button":
        extra = r.choice(["", ', "primary"', ', "danger", true', ", wake=false", ", disabled=true"])
        return f"Button({q(r.choice(WORDS))}{extra})"
    if kind == "Select":
        options = r.sample(OPTIONS, r.randrange(1, 5))
        multiple = r.random() < 0.3
        value = ""
        if r.random() < 0.5:
            value = ", " + (f"[{q(r.choice(options))}]" if multiple else q(r.choice(options)))
        return f"Select({q('Pick')}, {json.dumps(options)}{value}" + (", multiple=true" if multiple else "") + ")"
    if kind == "Image":
        src = r.choice(["https://cdn.example.com/a.png", "file:pics/b.jpg", "data:image/png;base64,AAAA"])
        height = f", height={r.choice([16, 100, 2000])}" if r.random() < 0.3 else ""
        return f"Image({q(src)}, {q('alt text')}{height})"
    if kind == "Audio":
        src = r.choice(["https://cdn.example.com/a.mp3", "file:sounds/b.wav"])
        title = f", {q('Title')}" if r.random() < 0.5 else ""
        return f"Audio({q(src)}{title})"
    if kind == "Link":
        return f"Link({q('Open')}, {q(r.choice(['https://example.com', 'http://example.org/x']))})"
    body = r.choice(["<b>hi</b>", "<div id=a></div><script>a.textContent='x'</script>", "<p>café</p>", 'say "hi"\n\\n'])
    height = f", {r.choice([20, 60, 300])}" if r.random() < 0.5 else ""
    return f"Html({q(body)}{height})"


def container_call(r: random.Random, kind: str, children: list[str]) -> str:
    extra = ""
    if kind in ("Column", "Row") and r.random() < 0.4:
        extra = f", {r.choice([0, 4, 8, 16, 64])}"
    if kind == "Card" and r.random() < 0.5:
        extra = f", {q('Card title')}"
    return f"{kind}([{', '.join(children)}]{extra})"


_PROP_CHOICES = {
    "Text": [("style", '"h2"'), ("text", q("new text")), ("hidden", "true")],
    "Button": [("disabled", "true"), ("disabled", "null"), ("label", q("L")), ("style", '"danger"')],
    "Select": [("options", '["Jazz", "Blues"]'), ("multiple", "true"), ("label", q("Pick 2")), ("wake", "true")],
    "Image": [("height", "120"), ("alt", q("new alt")), ("src", q("https://cdn.example.com/z.png"))],
    "Audio": [("loop", "true"), ("title", q("T"))],
    "Link": [("label", q("L2")), ("href", q("https://example.net"))],
    "Html": [("height", "100"), ("html", q("<i>z</i>"))],
    "Divider": [("hidden", "true")],
}


def program(r: random.Random, state: CanvasState, *, garbage: float = 0.08) -> str:
    """The next program for ``state``: one to four edits, now and then a wrong one."""
    lines: list[str] = []
    fresh = [i for i in IDS if i not in state.elements]
    for _ in range(r.randrange(1, 5)):
        action = r.choice(["add", "add", "add", "replace", "setprop", "setvalue", "del", "move", "clear", "container"])
        containers = [eid for eid, el in state.elements.items() if "children" in el]
        leaves = [eid for eid, el in state.elements.items() if "children" not in el]
        if action == "add" and fresh:
            new = fresh.pop()
            lines += [f"{new} = {leaf_call(r, r.choice(LEAF_TYPES))}", f"{r.choice(containers)} += [{new}]"]
        elif action == "container" and len(fresh) >= 2:
            box, inner = fresh.pop(), fresh.pop()
            lines += [
                f"{inner} = {leaf_call(r, 'Text')}",
                f"{box} = {container_call(r, r.choice(sorted(CONTAINER_TYPES)), [inner])}",
                f"{r.choice(containers)} += [{box}]",
            ]
        elif action == "replace" and leaves:
            eid = r.choice(leaves)
            lines.append(f"{eid} = {leaf_call(r, r.choice(['Text', 'Button', 'Select', 'Html', state.elements[eid]['type']]))}")
        elif action == "setprop" and leaves:
            eid = r.choice(leaves)
            prop, value = r.choice(_PROP_CHOICES[state.elements[eid]["type"]])
            lines.append(f"{eid}.{prop} = {value}")
        elif action == "setvalue" and leaves:
            eid = r.choice(leaves)
            kind = state.elements[eid]["type"]
            if kind == "Select":
                lines.append(f"{eid}.value = {q(r.choice(OPTIONS))}" if r.random() < 0.7 else f"{eid}.value = [{q(r.choice(OPTIONS))}]")
            elif kind == "Html":
                lines.append(f"{eid}.value = " + r.choice(["1", q("s"), "[1, 2]", '{"k": 1}', "null", *EDGE_VALUES]))
        elif action == "del":
            victims = [eid for eid in state.elements if eid != "root"]
            if victims:
                lines.append(f"del {r.choice(victims)}")
        elif action == "move" and len(containers) >= 2 and leaves:
            lines.append(f"{r.choice(containers)}.children = [{r.choice(leaves)}]")
        elif action == "clear" and r.random() < 0.2:
            lines.append("clear")
    if r.random() < garbage:
        lines.append(r.choice(GARBAGE + [UNPARSEABLE]))
    return "\n".join(lines) + "\n"

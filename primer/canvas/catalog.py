"""The element catalog: the eleven element types, their signatures and argument rules.

The table is research report d.4 plus the plan's builder bounds (Pivot P5, Task 2).
This module only describes elements and judges one argument at a time
(:func:`validate_arg`); building elements from statements and judging a whole
batch live in ``primer.canvas.state``. Pure: no I/O.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from primer.canvas.lang import Ref, id_problem

# Limits that belong to the element table (the operator-tunable ones are in state.Limits).
TEXT_MAX_CHARS = 10000
SELECT_OPTIONS_MAX = 100
OPTION_VALUE_MAX_CHARS = 200
URL_MAX_CHARS = 2048
FILE_PATH_MAX_CHARS = 512
DATA_URI_MAX_CHARS = 65536
#: The most characters of JSON an Html element's value may hold.
HTML_VALUE_MAX_CHARS = 1000

IMAGE_FILE_TYPES = ("png", "jpg", "jpeg", "gif", "webp", "avif")
AUDIO_FILE_TYPES = ("mp3", "ogg", "oga", "wav", "m4a", "aac", "flac", "weba")

COMMON_PROPS = ("hidden", "disabled")

_USE_HTTPS = "use https (or file:<path> for a workspace file)"
_FILE_REL = "file: paths are relative to the workspace and stay inside it"
_DATA_TOO_LONG = f"data: sources are limited to {DATA_URI_MAX_CHARS} characters"
_HREF = "Link href must be an http or https URL"
_OPTIONS_SHAPE = "each option must be a string or a [value, label] pair of strings"
_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


@dataclass(frozen=True)
class Param:
    """One argument of an element.

    ``kind`` is one of children, string, text, number, bool, enum, options, value,
    src, href, html. ``lo`` and ``hi`` bound a string's length, a number, or the
    number of options. ``default`` is what an omitted argument means; the stored
    form leaves a default out.
    """

    name: str
    required: bool
    kind: str
    enum: tuple[str, ...] = ()
    lo: float | None = None
    hi: float | None = None
    default: Any = None


@dataclass(frozen=True)
class ElementSpec:
    type: str
    params: tuple[Param, ...]
    container: bool


def _children() -> Param:
    return Param("children", True, "children")


def _gap() -> Param:
    return Param("gap", False, "number", lo=0, hi=64, default=8)


def _align(default: str) -> Param:
    return Param("align", False, "enum", enum=("start", "center", "end", "stretch"), default=default)


def _label() -> Param:
    return Param("label", True, "string", lo=1, hi=80)


CATALOG: dict[str, ElementSpec] = {
    spec.type: spec
    for spec in (
        ElementSpec("Column", (_children(), _gap(), _align("stretch")), True),
        ElementSpec("Row", (_children(), _gap(), _align("center"), Param("wrap", False, "bool", default=True)), True),
        ElementSpec("Card", (_children(), Param("title", False, "string", lo=1, hi=200)), True),
        ElementSpec("Divider", (), False),
        ElementSpec(
            "Text",
            (
                Param("text", True, "text", hi=TEXT_MAX_CHARS),
                Param("style", False, "enum", enum=("h1", "h2", "h3", "body", "caption", "code"), default="body"),
            ),
            False,
        ),
        ElementSpec(
            "Button",
            (
                _label(),
                Param("style", False, "enum", enum=("primary", "secondary", "danger"), default="secondary"),
                Param("disabled", False, "bool", default=False),
                Param("wake", False, "bool", default=True),
            ),
            False,
        ),
        ElementSpec(
            "Select",
            (
                _label(),
                Param("options", True, "options", lo=1, hi=SELECT_OPTIONS_MAX),
                Param("value", False, "value"),
                Param("multiple", False, "bool", default=False),
                Param("wake", False, "bool", default=False),
            ),
            False,
        ),
        ElementSpec(
            "Image",
            (
                Param("src", True, "src"),
                Param("alt", True, "string", lo=1, hi=300),
                Param("fit", False, "enum", enum=("contain", "cover"), default="contain"),
                Param("height", False, "number", lo=16, hi=2000),
            ),
            False,
        ),
        ElementSpec(
            "Audio",
            (Param("src", True, "src"), Param("title", False, "string", lo=1, hi=200), Param("loop", False, "bool", default=False)),
            False,
        ),
        ElementSpec("Link", (_label(), Param("href", True, "href")), False),
        ElementSpec("Html", (Param("html", True, "html"), Param("height", False, "number", lo=20, hi=2000)), False),
    )
}

CONTAINER_TYPES = frozenset(name for name, spec in CATALOG.items() if spec.container)

COMMON_PARAMS: dict[str, Param] = {name: Param(name, False, "bool", default=False) for name in COMMON_PROPS}

#: Event kind -> whether it wakes the agent by default, per element type.
EVENT_KINDS: dict[str, dict[str, bool]] = {
    "Button": {"click": True},
    "Select": {"change": False},
    "Audio": {"play": False, "pause": False, "ended": False},
    "Link": {"open": False},
    "Html": {"emit": True, "set_value": False},
}


def signature(type_name: str) -> str:
    """For example ``Select(label, options, value?, multiple?, wake?)``."""
    spec = CATALOG[type_name]
    return f"{type_name}({', '.join(p.name if p.required else p.name + '?' for p in spec.params)})"


def property_help(type_name: str) -> str:
    """The signature plus the properties every element takes (for unknown-property errors)."""
    return f"{signature(type_name)}; every element also takes {', '.join(COMMON_PROPS)}"


def param_for(spec: ElementSpec, name: str) -> Param | None:
    """The signature parameter called ``name``, else a common property, else None."""
    for param in spec.params:
        if param.name == name:
            return param
    return COMMON_PARAMS.get(name)


def all_params(spec: ElementSpec) -> tuple[Param, ...]:
    """The signature parameters, then the common properties the signature lacks."""
    own = {p.name for p in spec.params}
    return spec.params + tuple(COMMON_PARAMS[n] for n in COMMON_PROPS if n not in own)


def has_unstorable_char(value: Any, _depth: int = 0) -> bool:
    """Whether a string inside ``value`` holds a NUL or a lone surrogate.

    Neither can be stored (Postgres JSONB refuses NUL; a lone surrogate cannot be
    written as UTF-8), so a canvas never accepts them.
    """
    if isinstance(value, str):
        return _UNSTORABLE.search(value) is not None
    if _depth > 64:
        return True
    if isinstance(value, (list, tuple)):
        return any(has_unstorable_char(v, _depth + 1) for v in value)
    if isinstance(value, dict):
        return any(has_unstorable_char(k, _depth + 1) or has_unstorable_char(v, _depth + 1) for k, v in value.items())
    return False


def _num(x: float | None) -> str:
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else str(x)


def _url_ok(value: str, schemes: tuple[str, ...]) -> bool:
    """A plain absolute URL: allowed scheme, a host, no userinfo, no spaces or backslashes."""
    if len(value) > URL_MAX_CHARS or "\\" in value:
        return False
    if any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return False
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() not in schemes:
        return False
    if not parts.netloc or "@" in parts.netloc:
        return False
    return bool(parts.hostname)


def _file_types_message(type_name: str) -> str:
    if type_name == "Image":
        types = IMAGE_FILE_TYPES
        return f"file:<path> must name a {', '.join(types[:-1])} or {types[-1]} image"
    types = AUDIO_FILE_TYPES
    return f"file:<path> must name an {', '.join(types[:-1])} or {types[-1]} audio file"


def _check_file(type_name: str, path: str) -> str | None:
    if not path or len(path) > FILE_PATH_MAX_CHARS or "\\" in path:
        return _FILE_REL
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return _FILE_REL
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        return _FILE_REL
    if segments[0].lower() in (".state", ".tmp"):
        return _FILE_REL
    suffix = PurePosixPath(segments[-1]).suffix.lower().lstrip(".")
    allowed = IMAGE_FILE_TYPES if type_name == "Image" else AUDIO_FILE_TYPES
    if suffix not in allowed:
        return _file_types_message(type_name)
    return None


def _check_data(type_name: str, value: str) -> str | None:
    if len(value) > DATA_URI_MAX_CHARS:
        return _DATA_TOO_LONG
    media = "image" if type_name == "Image" else "audio"
    if re.match(rf"data:{media}/[a-z0-9.+-]+;base64,", value, re.IGNORECASE) is None:
        return f"data: sources must start with data:{media}/<type>;base64,"
    return None


def _check_src(type_name: str, value: Any) -> str | None:
    if not isinstance(value, str):
        return "src must be text"
    head = value[:5].lower()
    if head == "file:":
        return _check_file(type_name, value[5:])
    if head == "data:":
        return _check_data(type_name, value)
    if len(value) > URL_MAX_CHARS and value[:8].lower() == "https://":
        return f"https URLs are limited to {URL_MAX_CHARS} characters"
    if not _url_ok(value, ("https",)):
        return _USE_HTTPS
    return None


def _check_options(type_name: str, param: Param, value: Any) -> str | None:
    if not isinstance(value, (list, tuple)):
        return f"{type_name} needs {param.name} as a list"
    if not param.lo <= len(value) <= param.hi:  # type: ignore[operator]
        return f"{param.name} must have {_num(param.lo)} to {_num(param.hi)} entries"
    shapes: set[str] = set()
    values: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            shapes.add("string")
            values.append(entry)
        elif isinstance(entry, (list, tuple)) and len(entry) == 2 and all(isinstance(e, str) for e in entry):
            shapes.add("pair")
            values.append(entry[0])
        else:
            return _OPTIONS_SHAPE
    if len(shapes) > 1:
        return f"{param.name} must be all strings or all [value, label] pairs"
    for option in values:
        if len(option) > OPTION_VALUE_MAX_CHARS:
            return f"option values are limited to {OPTION_VALUE_MAX_CHARS} characters"
    seen: set[str] = set()
    for option in values:
        if option in seen:
            return f"option values must be unique; '{option}' appears twice"
        seen.add(option)
    return None


def validate_arg(type_name: str, param: Param, value: Any, *, max_html_bytes: int) -> str | None:
    """Judge one argument by itself; the error message, or None when it is fine.

    Rules that need a sibling argument (a Select's value against its options) or
    the rest of the canvas live in ``primer.canvas.state``.
    """
    name = param.name
    kind = param.kind
    if kind != "children" and has_unstorable_char(value):
        return f"{name} contains a character that cannot be stored (a NUL or a lone surrogate)"
    if kind == "children":
        if not isinstance(value, (list, tuple)):
            return f"{type_name} needs {name} as a list"
        for item in value:
            if not isinstance(item, Ref):
                return "children must be element ids written without quotes, for example [a, b]"
            problem = id_problem(item.id)
            if problem:
                return problem
        return None
    if kind == "string":
        if not isinstance(value, str):
            return f"{name} must be text"
        if not param.lo <= len(value) <= param.hi:  # type: ignore[operator]
            return f"{name} must be {_num(param.lo)} to {_num(param.hi)} characters"
        return None
    if kind == "text":
        if not isinstance(value, str):
            return f"{name} must be text"
        if len(value) > param.hi:  # type: ignore[operator]
            return f"{name} is {len(value)} characters; the limit is {_num(param.hi)}"
        return None
    if kind == "number":
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or (param.lo is not None and value < param.lo)
            or (param.hi is not None and value > param.hi)
        ):
            return f"{name} must be a number from {_num(param.lo)} to {_num(param.hi)}"
        return None
    if kind == "bool":
        return None if isinstance(value, bool) else f"{name} must be true or false"
    if kind == "enum":
        return None if value in param.enum and isinstance(value, str) else f"{name} must be one of {', '.join(param.enum)}"
    if kind == "options":
        return _check_options(type_name, param, value)
    if kind == "value":
        if isinstance(value, str) or (isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)):
            return None
        return f"{name} must be one of the options (a list of them when multiple is true)"
    if kind == "src":
        return _check_src(type_name, value)
    if kind == "href":
        return None if isinstance(value, str) and _url_ok(value, ("http", "https")) else _HREF
    if kind == "html":
        if not isinstance(value, str):
            return f"{name} must be text"
        size = len(value.encode("utf-8"))
        if size > max_html_bytes:
            return f"{type_name} is {size} bytes; the limit is {max_html_bytes}"
        return None
    raise ValueError(f"unknown parameter kind {kind!r}")


__all__ = [
    "AUDIO_FILE_TYPES",
    "CATALOG",
    "COMMON_PARAMS",
    "COMMON_PROPS",
    "CONTAINER_TYPES",
    "EVENT_KINDS",
    "HTML_VALUE_MAX_CHARS",
    "IMAGE_FILE_TYPES",
    "ElementSpec",
    "Param",
    "all_params",
    "has_unstorable_char",
    "param_for",
    "property_help",
    "signature",
    "validate_arg",
]

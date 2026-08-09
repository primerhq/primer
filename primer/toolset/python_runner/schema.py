"""Function signature to a self-contained JSON Schema.

Rules, all enforced at registration:

* ``ctx: ToolContext`` is injected by the engine and is never a schema
  property, matching what InternalToolsetProvider already does by signature
  inspection.
* every other parameter must be annotated AND documented.
* the result must be self-contained: make_tool validates examples against it
  with Draft202012Validator, which cannot resolve external refs.

The mapping is structural, over the AST, and never evaluates the annotation.
Registration must not execute a line of the module it is validating, and
``eval``-ing an annotation would do exactly that.
"""

from __future__ import annotations

import ast
from typing import Any

from primer.toolset.python_runner.docstring import ParsedDocstring

CTX_PARAM = "ctx"

_PRIMITIVES: dict[str, dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "dict": {"type": "object"},
    "list": {"type": "array"},
}

_SUPPORTED = "str, int, float, bool, list[...], dict, or a union with None"


class SchemaError(ValueError):
    """A signature that cannot produce a usable args schema.

    ``field`` is the offending parameter name so the API can point at it.
    """

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


def _annotation_to_schema(node: ast.expr, param: str) -> dict[str, Any]:
    if isinstance(node, ast.Name) and node.id in _PRIMITIVES:
        return dict(_PRIMITIVES[node.id])
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}
    if isinstance(node, ast.Subscript):
        base = node.value
        if isinstance(base, ast.Name) and base.id == "list":
            return {
                "type": "array",
                "items": _annotation_to_schema(node.slice, param),
            }
        if isinstance(base, ast.Name) and base.id == "dict":
            return {"type": "object"}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _annotation_to_schema(node.left, param)
        right = _annotation_to_schema(node.right, param)
        # `X | None` is the common case: keep X's shape and mark it nullable
        # rather than emitting an anyOf the model has to reason about.
        if right == {"type": "null"}:
            return {**left, "nullable": True}
        if left == {"type": "null"}:
            return {**right, "nullable": True}
        return {"anyOf": [left, right]}
    raise SchemaError(
        f"parameter {param!r} has an annotation this runner cannot map to a "
        f"JSON Schema; use {_SUPPORTED}",
        field=param,
    )


def build_args_schema(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
    doc: ParsedDocstring,
    *,
    require_arg_docs: bool = True,
) -> dict[str, Any]:
    """Build the tool's args schema from ``fn_node``'s signature.

    ``require_arg_docs=False`` relaxes the every-parameter-documented
    rule for contexts whose descriptions never reach LLM context
    (service bundle functions); annotations stay mandatory because they
    ARE the schema.
    """
    a = fn_node.args
    if a.vararg is not None:
        raise SchemaError(
            "*args cannot be described as a tool schema", field=a.vararg.arg
        )
    if a.kwarg is not None:
        raise SchemaError(
            "**kwargs cannot be described as a tool schema", field=a.kwarg.arg
        )

    positional = list(a.posonlyargs) + list(a.args)
    defaults: dict[str, Any] = {}
    if a.defaults:
        for arg, default in zip(positional[-len(a.defaults) :], a.defaults):
            defaults[arg.arg] = ast.literal_eval(default)
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        if default is not None:
            defaults[arg.arg] = ast.literal_eval(default)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg in positional + list(a.kwonlyargs):
        name = arg.arg
        if name == CTX_PARAM:
            continue
        if arg.annotation is None:
            raise SchemaError(
                f"parameter {name!r} needs a type annotation", field=name
            )
        if name not in doc.args and require_arg_docs:
            raise SchemaError(
                f"parameter {name!r} is not documented in the docstring's "
                f"Args: section",
                field=name,
            )
        prop = _annotation_to_schema(arg.annotation, name)
        if name in doc.args:
            prop["description"] = doc.args[name]
        if name in defaults:
            prop["default"] = defaults[name]
        else:
            required.append(name)
        properties[name] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }

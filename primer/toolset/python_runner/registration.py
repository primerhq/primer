"""Source to Tool descriptors, by AST only.

Registration NEVER executes the module. The source is untrusted - an agent can
reach the toolset-management tools - so the only safe way to inspect it on the
host is structurally. Execution happens later, in the runner, behind isolation.

Yielding is declared by a ``@resumes(fn)`` companion, not inferred from a
return annotation. make_tool's explicit ``yields`` flag replaced exactly that
kind of source introspection; this keeps the decision explicit while still
giving authors a decorator to write.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from jsonschema.exceptions import ValidationError

from primer.model.chat import Tool, ToolExample
from primer.toolset._describe import make_tool
from primer.toolset.python_runner.docstring import (
    DocstringError,
    ParsedDocstring,
    parse_docstring,
)
from primer.toolset.python_runner.schema import SchemaError, build_args_schema

TOOL_DECORATOR = "primer_tool"
RESUME_DECORATOR = "resumes"
TIMEOUT_CEILING_SECONDS = 300.0

_FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


class RegistrationError(ValueError):
    """Source that cannot produce a usable toolset.

    ``field`` and ``lineno`` are surfaced in the API's RFC7807 extensions so
    the console can point at the offending line.
    """

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        lineno: int | None = None,
    ) -> None:
        super().__init__(message)
        self.field = field
        self.lineno = lineno


@dataclass
class RegisteredTool:
    """One decorated function, resolved into everything dispatch needs."""

    tool: Tool
    fn_name: str
    resume_fn_name: str | None
    timeout_seconds: float
    # 1-based line of the `def`, for the console's function outline. Errors
    # already carry a line; a successfully registered tool needs one too, or
    # the outline can list a function but not jump to it.
    lineno: int = 0


def _decorator_named(node: ast.expr, name: str) -> ast.Call | ast.Name | None:
    """Match ``@name`` and ``@name(...)``, ignoring anything else."""
    if isinstance(node, ast.Name) and node.id == name:
        return node
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ):
        return node
    return None


def _timeout_from(dec: ast.Call | ast.Name, default: float, fn: str) -> float:
    if not isinstance(dec, ast.Call):
        return default
    for kw in dec.keywords:
        if kw.arg != "timeout_seconds":
            continue
        try:
            value = float(ast.literal_eval(kw.value))
        except (ValueError, TypeError) as exc:
            raise RegistrationError(
                f"{fn}: timeout_seconds must be a literal number",
                field="timeout_seconds",
                lineno=getattr(kw.value, "lineno", None),
            ) from exc
        if value <= 0 or value > TIMEOUT_CEILING_SECONDS:
            raise RegistrationError(
                f"{fn}: timeout_seconds must be > 0 and <= "
                f"{TIMEOUT_CEILING_SECONDS}",
                field="timeout_seconds",
                lineno=getattr(kw.value, "lineno", None),
            )
        return value
    return default


def _collect(
    tree: ast.Module,
) -> tuple[list[tuple[_FunctionNode, ast.expr]], dict[str, str]]:
    """Return the decorated tool functions and the {tool: resume_fn} map."""
    tool_nodes: list[tuple[_FunctionNode, ast.expr]] = []
    resume_for: dict[str, str] = {}

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (found := _decorator_named(dec, TOOL_DECORATOR)) is not None:
                tool_nodes.append((node, found))
                continue
            if (found := _decorator_named(dec, RESUME_DECORATOR)) is None:
                continue
            if not isinstance(found, ast.Call) or not found.args:
                raise RegistrationError(
                    f"{node.name}: @resumes needs the tool it resumes, "
                    f"e.g. @resumes(my_tool)",
                    lineno=node.lineno,
                )
            target = found.args[0]
            if not isinstance(target, ast.Name):
                raise RegistrationError(
                    f"{node.name}: @resumes takes a function name",
                    lineno=node.lineno,
                )
            resume_for[target.id] = node.name

    return tool_nodes, resume_for


def _lenient_docstring(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ParsedDocstring:
    """Best-effort docstring for contexts outside LLM context.

    Service bundle functions are called by app code, not by a model, so
    a missing or partial docstring degrades to a synthesized description
    instead of failing registration. Annotations still drive the schema.
    """
    try:
        return parse_docstring(ast.get_docstring(node) or "")
    except DocstringError:
        return ParsedDocstring(
            purpose=f"Run the {node.name} function.",
            when="Use when a service app calls it through the gateway.",
        )


def _parse_and_collect(
    source: str, *, allow_yielding: bool,
) -> tuple[list[tuple[_FunctionNode, ast.expr]], dict[str, str]]:
    """Module-level parse + validation shared by both entry points below.

    Raises :class:`RegistrationError` for problems that are not
    attributable to one function -- bad syntax, a dangling ``@resumes``
    reference, or disallowed yielding -- so both the raise-on-first-error
    and collect-every-verdict callers fail the same way here.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RegistrationError(
            f"the module does not parse: {exc.msg}",
            field="source",
            lineno=exc.lineno,
        ) from exc

    tool_nodes, resume_for = _collect(tree)

    tool_names = {node.name for node, _ in tool_nodes}
    for target in resume_for:
        if target not in tool_names:
            raise RegistrationError(
                f"@resumes references {target!r}, which is not a "
                f"@{TOOL_DECORATOR} function in this module"
            )

    if not allow_yielding and resume_for:
        offender = sorted(resume_for)[0]
        raise RegistrationError(
            f"{offender}: yielding functions are not allowed in this "
            f"context (the caller is synchronous); remove the "
            f"@{RESUME_DECORATOR} companion"
        )

    return tool_nodes, resume_for


def _register_one(
    node: _FunctionNode,
    dec: ast.expr,
    *,
    resume_for: dict[str, str],
    toolset_id: str,
    default_timeout: float,
    require_docstrings: bool,
) -> RegisteredTool:
    """Register one decorated function. Raises ``RegistrationError`` on
    any failure specific to this function (bad docstring, bad schema, a
    docstring example that disagrees with the signature, a bad
    ``timeout_seconds``)."""
    try:
        if require_docstrings:
            doc = parse_docstring(ast.get_docstring(node) or "")
        else:
            doc = _lenient_docstring(node)
        schema = build_args_schema(
            node, doc, require_arg_docs=require_docstrings
        )
    except (DocstringError, SchemaError) as exc:
        raise RegistrationError(
            f"{node.name}: {exc}", field=exc.field, lineno=node.lineno
        ) from exc

    timeout = _timeout_from(dec, default_timeout, node.name)
    resume_name = resume_for.get(node.name)
    try:
        tool = make_tool(
            id=node.name,
            toolset_id=toolset_id,
            purpose=doc.purpose,
            when=doc.when,
            args_schema=schema,
            examples=[ToolExample(args=a) for a in doc.examples],
            yields=resume_name is not None,
        )
    except ValidationError as exc:
        # make_tool validates every example against the schema. A docstring
        # example that disagrees with the signature is a registration
        # failure, not something to ship to a model.
        raise RegistrationError(
            f"{node.name}: a docstring example does not match the "
            f"argument schema: {exc.message}",
            field="examples",
            lineno=node.lineno,
        ) from exc

    return RegisteredTool(
        tool=tool,
        fn_name=node.name,
        resume_fn_name=resume_name,
        timeout_seconds=timeout,
        lineno=node.lineno,
    )


def register_module(
    source: str,
    toolset_id: str,
    default_timeout: float,
    *,
    require_docstrings: bool = True,
    allow_yielding: bool = True,
) -> list[RegisteredTool]:
    """Parse ``source`` and return one RegisteredTool per decorated function.

    ``require_docstrings=False`` drops the docstring-anatomy bar (used by
    service bundles, whose descriptions never reach a model);
    ``allow_yielding=False`` rejects ``@resumes`` companions outright
    (used by callers with no park/resume path, e.g. the synchronous
    service gateway). Defaults preserve the original strict behavior.

    Raises ``RegistrationError`` on the FIRST bad function -- callers that
    persist a toolset (or otherwise need an all-or-nothing result) want
    exactly that. For a full per-function report instead, see
    :func:`register_module_report`.
    """
    tool_nodes, resume_for = _parse_and_collect(
        source, allow_yielding=allow_yielding,
    )
    return [
        _register_one(
            node, dec,
            resume_for=resume_for,
            toolset_id=toolset_id,
            default_timeout=default_timeout,
            require_docstrings=require_docstrings,
        )
        for node, dec in tool_nodes
    ]


@dataclass
class ToolRegistrationFailure:
    """One decorated function that failed to register, captured instead of
    raised. Returned alongside successful :class:`RegisteredTool` entries
    by :func:`register_module_report`."""

    fn_name: str
    message: str
    field: str | None
    lineno: int


def register_module_report(
    source: str,
    toolset_id: str,
    default_timeout: float,
    *,
    require_docstrings: bool = True,
    allow_yielding: bool = True,
) -> list[RegisteredTool | ToolRegistrationFailure]:
    """Like :func:`register_module`, but a bad function does not abort the
    rest of the module: every ``@primer_tool`` function gets its own
    verdict, in source order. Module-level problems (bad syntax, a
    dangling ``@resumes`` reference, disallowed yielding) still raise
    ``RegistrationError``, since those aren't attributable to one
    function.

    Used by the dry-run validate endpoint, whose contract is a per-tool
    array -- a module with 2 bad tools and 3 good ones should report all
    5 verdicts, not abort after the first bad one.
    """
    tool_nodes, resume_for = _parse_and_collect(
        source, allow_yielding=allow_yielding,
    )

    out: list[RegisteredTool | ToolRegistrationFailure] = []
    for node, dec in tool_nodes:
        try:
            out.append(_register_one(
                node, dec,
                resume_for=resume_for,
                toolset_id=toolset_id,
                default_timeout=default_timeout,
                require_docstrings=require_docstrings,
            ))
        except RegistrationError as exc:
            out.append(ToolRegistrationFailure(
                fn_name=node.name,
                message=str(exc),
                field=exc.field,
                lineno=exc.lineno if exc.lineno is not None else node.lineno,
            ))
    return out

"""Sandboxed Jinja2 renderer for harness templates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from jinja2.exceptions import (
    TemplateError as JinjaTemplateError,
    UndefinedError,
)


_KINDS = ("agent", "graph", "collection", "document", "toolset")
_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class HarnessTemplateError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        template: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.template = template


@dataclass
class RenderedFile:
    template_path: str           # relative to subpath
    template_name: str           # bare logical name from YAML
    kind: str
    source_bytes: bytes
    rendered_text: str
    rendered: dict[str, Any]
    content: str | None = None   # for kind=document, resolved content_path body


def _env() -> SandboxedEnvironment:
    import base64
    import json as _json

    env = SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
    )
    env.filters["tojson"] = lambda obj: _json.dumps(obj)
    env.filters["b64encode"] = lambda s: base64.b64encode(
        s.encode("utf-8") if isinstance(s, str) else s,
    ).decode("ascii")
    return env


def render_template(
    source: str,
    *,
    overrides: dict[str, Any],
    harness_ctx: dict[str, Any],
) -> str:
    """Render one template string to text."""
    env = _env()
    try:
        tmpl = env.from_string(source)
        return tmpl.render(overrides=overrides, harness=harness_ctx)
    except UndefinedError as exc:
        raise HarnessTemplateError("template_render_failed", str(exc)) from exc
    except JinjaTemplateError as exc:
        raise HarnessTemplateError("template_render_failed", str(exc)) from exc
    except Exception as exc:
        raise HarnessTemplateError("template_render_failed", str(exc)) from exc


def render_bundle(
    *,
    checkout_dir: str,
    subpath: str | None,
    overrides: dict[str, Any],
    harness_ctx: dict[str, Any],
) -> list[RenderedFile]:
    """Walk templates/ under (checkout_dir/subpath) and render each YAML."""
    base = Path(checkout_dir)
    if subpath:
        base = base / subpath
    templates_dir = base / "templates"
    if not templates_dir.is_dir():
        raise HarnessTemplateError(
            "harness_yaml_invalid",
            f"templates/ directory not found under {subpath or '.'}",
        )

    out: list[RenderedFile] = []
    for path in sorted(templates_dir.rglob("*.yaml")):
        rel = str(path.relative_to(templates_dir))
        source_bytes = path.read_bytes()
        try:
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessTemplateError(
                "template_yaml_invalid",
                f"{rel}: not UTF-8",
                template=rel,
            ) from exc
        try:
            rendered_text = render_template(
                source_text,
                overrides=overrides,
                harness_ctx=harness_ctx,
            )
        except HarnessTemplateError as exc:
            exc.template = rel
            raise
        try:
            data = yaml.safe_load(rendered_text)
        except yaml.YAMLError as exc:
            raise HarnessTemplateError(
                "template_yaml_invalid",
                f"{rel}: {exc}",
                template=rel,
            ) from exc
        if not isinstance(data, dict):
            raise HarnessTemplateError(
                "template_yaml_invalid",
                f"{rel}: top-level YAML must be a mapping",
                template=rel,
            )
        kind = data.get("kind")
        name = data.get("name")
        spec = data.get("spec")
        if kind not in _KINDS:
            raise HarnessTemplateError(
                "template_kind_unknown",
                f"{rel}: unknown kind {kind!r}",
                template=rel,
            )
        if not isinstance(name, str) or not name:
            raise HarnessTemplateError(
                "template_yaml_invalid",
                f"{rel}: missing or invalid 'name'",
                template=rel,
            )
        if not _NAME_RE.match(name):
            raise HarnessTemplateError(
                "template_yaml_invalid",
                f"{rel}: 'name' must match [a-z][a-z0-9-]{{0,62}} (got {name!r})",
                template=rel,
            )
        if not isinstance(spec, dict):
            raise HarnessTemplateError(
                "template_yaml_invalid",
                f"{rel}: 'spec' must be a mapping",
                template=rel,
            )
        content: str | None = None
        if kind == "document":
            inline = data.get("content_inline")
            cpath = data.get("content_path")
            if inline is not None and cpath is not None:
                raise HarnessTemplateError(
                    "template_yaml_invalid",
                    f"{rel}: only one of content_inline / content_path allowed",
                    template=rel,
                )
            if inline is not None:
                if not isinstance(inline, str):
                    raise HarnessTemplateError(
                        "template_yaml_invalid",
                        f"{rel}: content_inline must be a string",
                        template=rel,
                    )
                content = inline
            elif cpath is not None:
                if not isinstance(cpath, str):
                    raise HarnessTemplateError(
                        "template_yaml_invalid",
                        f"{rel}: content_path must be a string",
                        template=rel,
                    )
                norm = Path(cpath).as_posix()
                if norm.startswith("/") or ".." in Path(norm).parts:
                    raise HarnessTemplateError(
                        "template_yaml_invalid",
                        f"{rel}: content_path must be a relative, "
                        "non-traversing path",
                        template=rel,
                    )
                target = base / norm
                if not target.is_file():
                    raise HarnessTemplateError(
                        "template_yaml_invalid",
                        f"{rel}: content_path {norm!r} not found",
                        template=rel,
                    )
                content = target.read_text(encoding="utf-8")
        out.append(
            RenderedFile(
                template_path=rel,
                template_name=name,
                kind=kind,
                source_bytes=source_bytes,
                rendered_text=rendered_text,
                rendered=data,
                content=content,
            ),
        )
    return out


__all__ = [
    "HarnessTemplateError",
    "RenderedFile",
    "render_bundle",
    "render_template",
]

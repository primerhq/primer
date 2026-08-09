"""Publish-time validation of a service bundle.

Everything a bundle can get wrong fails HERE, at publish, never at
request time (mirroring the python-runner's "registration is where a bad
tool must fail" principle). Pure bytes-in: this module does no storage
or network I/O, which keeps the whole gate unit-testable.

Spec: ``docs/superpowers/specs/2026-08-08-services-design.md`` section 5.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field

import yaml
from pydantic import ValidationError

from primer.model.service import ServiceFunctionSpec, ServiceManifest
from primer.toolset.python_runner.registration import (
    RegistrationError,
    register_module,
)

MAX_BUNDLE_BYTES = 10 * 1024 * 1024
"""Uncompressed size cap; counted while reading, not from headers."""

MAX_BUNDLE_FILES = 200

MANIFEST_PATH = "service.yaml"

DEFAULT_FUNCTIONS_FILE = "functions.py"

_DEFAULT_FN_TIMEOUT = 30.0


class BundleError(ValueError):
    """A bundle that must not be published.

    ``field`` names the offending file or manifest key; ``lineno`` is set
    for python source errors so the API can point an author at the line.
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
class ValidatedBundle:
    """The publish pipeline's output: everything a ServiceVersion needs."""

    manifest: ServiceManifest
    files: dict[str, bytes]
    functions: list[ServiceFunctionSpec] = field(default_factory=list)
    function_sources: dict[str, str] = field(default_factory=dict)


def _safe_name(name: str) -> str:
    """Reject absolute and escaping member names; normalise './' prefixes."""
    cleaned = name[2:] if name.startswith("./") else name
    if not cleaned or cleaned.startswith("/"):
        raise BundleError(
            f"bundle member {name!r} has an absolute path", field=name
        )
    if any(part == ".." for part in cleaned.split("/")):
        raise BundleError(
            f"bundle member {name!r} escapes the bundle root", field=name
        )
    return cleaned


def _extract(tar_gz: bytes) -> dict[str, bytes]:
    try:
        tf = tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"the bundle is not a gzipped tar: {exc}") from exc

    files: dict[str, bytes] = {}
    total = 0
    with tf:
        for member in tf:
            if member.isdir():
                continue
            if not member.isreg():
                raise BundleError(
                    f"bundle member {member.name!r} is not a regular file "
                    "(links and devices are rejected)",
                    field=member.name,
                )
            name = _safe_name(member.name)
            if len(files) >= MAX_BUNDLE_FILES:
                raise BundleError(
                    f"the bundle exceeds {MAX_BUNDLE_FILES} files"
                )
            fh = tf.extractfile(member)
            data = fh.read(MAX_BUNDLE_BYTES - total + 1) if fh else b""
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                raise BundleError(
                    f"the bundle exceeds {MAX_BUNDLE_BYTES} bytes uncompressed"
                )
            files[name] = data
    return files


def _parse_manifest(files: dict[str, bytes]) -> ServiceManifest:
    raw = files.get(MANIFEST_PATH)
    if raw is None:
        return ServiceManifest()
    try:
        loaded = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BundleError(
            f"{MANIFEST_PATH} does not parse: {exc}", field=MANIFEST_PATH
        ) from exc
    if not isinstance(loaded, dict):
        raise BundleError(
            f"{MANIFEST_PATH} must be a mapping", field=MANIFEST_PATH
        )
    try:
        return ServiceManifest.model_validate(loaded)
    except ValidationError as exc:
        raise BundleError(
            f"{MANIFEST_PATH} is invalid: {exc}", field=MANIFEST_PATH
        ) from exc


def _register_functions(
    manifest: ServiceManifest, files: dict[str, bytes]
) -> tuple[list[ServiceFunctionSpec], dict[str, str]]:
    specs: list[ServiceFunctionSpec] = []
    sources: dict[str, str] = {}
    seen: dict[str, str] = {}
    for path in manifest.functions:
        raw = files.get(path)
        if raw is None:
            raise BundleError(
                f"manifest lists functions file {path!r} but the bundle "
                "does not contain it",
                field=path,
            )
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BundleError(
                f"functions file {path!r} is not utf-8", field=path
            ) from exc
        try:
            registered = register_module(
                source,
                "service-bundle",
                _DEFAULT_FN_TIMEOUT,
                require_docstrings=False,
                allow_yielding=False,
            )
        except RegistrationError as exc:
            raise BundleError(
                f"{path}: {exc}", field=exc.field or path, lineno=exc.lineno
            ) from exc
        sources[path] = source
        for rt in registered:
            if rt.fn_name in seen:
                raise BundleError(
                    f"function {rt.fn_name!r} is defined in both "
                    f"{seen[rt.fn_name]!r} and {path!r}",
                    field=rt.fn_name,
                )
            seen[rt.fn_name] = path
            specs.append(
                ServiceFunctionSpec(
                    name=rt.fn_name,
                    schema=rt.tool.args_schema,
                    timeout_seconds=rt.timeout_seconds,
                    source_file=path,
                )
            )
    return specs, sources


def validate_bundle(tar_gz: bytes) -> ValidatedBundle:
    """Validate a gzipped-tar bundle and derive its function specs.

    Raises :class:`BundleError` on any violation of spec section 5's
    publish gate; a bundle that comes back as a :class:`ValidatedBundle`
    is safe to persist verbatim.
    """
    files = _extract(tar_gz)
    manifest = _parse_manifest(files)
    if not manifest.functions and DEFAULT_FUNCTIONS_FILE in files:
        manifest = manifest.model_copy(
            update={"functions": [DEFAULT_FUNCTIONS_FILE]}
        )
    functions, sources = _register_functions(manifest, files)
    return ValidatedBundle(
        manifest=manifest,
        files=files,
        functions=functions,
        function_sources=sources,
    )


__all__ = [
    "MAX_BUNDLE_BYTES",
    "MAX_BUNDLE_FILES",
    "BundleError",
    "ValidatedBundle",
    "validate_bundle",
]

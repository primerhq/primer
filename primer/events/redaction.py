"""Event-payload redaction (2026-08-23 three-view console wiring, P6).

CRUD events carry the stored row's own JSON dump, and storage
deliberately unwraps ``SecretStr`` to plaintext before persisting
(:func:`primer.storage.sqlite._append_crud_event`), so provider API
keys, template env values and channel tokens sit in event payloads in
the clear. Every event read passes through here - the admin gate is an
access control, not an excuse to ship secrets to a browser.

Two defenses, both over-inclusive by design (masking a non-secret is
harmless; missing one is the incident):

* **Model registry**: every field name any ``primer.model`` class
  declares with ``SecretStr`` anywhere in its annotation (``api_key``,
  ``env`` as ``dict[str, SecretStr]``, ...) is masked wherever it
  appears in a payload.
* **Key-name defense**: any key that *looks* secret-bearing
  (key/token/secret/password/credential/dsn/authorization/cookie) is
  masked even when no model declares it - the payload may predate the
  current schema or come from a source outside the model package.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
import typing
from typing import Any

from pydantic import BaseModel, SecretStr

MASK = "•••redacted•••"

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:^|[_-])("
    r"key|keys|token|tokens|secret|secrets|password|passwd|"
    r"credential|credentials|dsn|authorization|cookie|cookies"
    r")$"
)

_secret_field_names: frozenset[str] | None = None


def _mentions_secretstr(annotation: Any) -> bool:
    """True when ``SecretStr`` appears anywhere in a type annotation."""
    if annotation is SecretStr:
        return True
    return any(
        _mentions_secretstr(arg) for arg in typing.get_args(annotation)
    )


def secret_field_names() -> frozenset[str]:
    """Field names declared with ``SecretStr`` anywhere in primer.model.

    Computed once per process; import errors in a model module are a
    bug elsewhere, so they propagate rather than silently shrinking the
    redaction set.
    """
    global _secret_field_names
    if _secret_field_names is None:
        import primer.model as model_pkg

        names: set[str] = set()
        for modinfo in pkgutil.iter_modules(model_pkg.__path__):
            mod = importlib.import_module(f"primer.model.{modinfo.name}")
            for _, cls in inspect.getmembers(mod, inspect.isclass):
                if not (
                    issubclass(cls, BaseModel)
                    and cls.__module__ == mod.__name__
                ):
                    continue
                for fname, finfo in cls.model_fields.items():
                    if _mentions_secretstr(finfo.annotation):
                        names.add(fname)
        _secret_field_names = frozenset(names)
    return _secret_field_names


def _is_sensitive_key(key: str) -> bool:
    return key in secret_field_names() or bool(_SENSITIVE_KEY_RE.search(key))


def _mask_value(value: Any) -> Any:
    """Mask a sensitive field's VALUE while keeping its shape readable.

    A dict keeps its keys (an env override's KEY names are config, not
    secrets) with every value masked; a list masks each element; any
    scalar becomes the mask. ``None`` stays ``None`` - "unset" is not a
    secret, and masking it would fake one.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _mask_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_value(v) for v in value]
    return MASK


def redact_payload(payload: Any) -> Any:
    """Deep-copy ``payload`` with every sensitive field masked."""
    if isinstance(payload, dict):
        return {
            k: _mask_value(v) if _is_sensitive_key(str(k)) else redact_payload(v)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [redact_payload(v) for v in payload]
    return payload


def redact_event(event: Any) -> Any:
    """Return a copy of an :class:`~primer.model.event.Event` with its
    payload redacted. Envelope fields (ids, types, timestamps) are not
    secret-bearing and pass through untouched."""
    return event.model_copy(update={"payload": redact_payload(event.payload)})


__all__ = [
    "MASK",
    "redact_event",
    "redact_payload",
    "secret_field_names",
]

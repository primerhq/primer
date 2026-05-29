"""Deterministic K8s object names from workspace ids.

K8s object names cap at 63 characters and must be DNS-label-safe
(lowercase alphanumerics + hyphens, no leading/trailing hyphen).

Strategy:
- Short, safe workspace ids -> primer-ws-<id> (if combined <= 63)
- Anything else -> primer-ws-<sha256(id)[:16]>
The mapping is persisted on the workspace row as runtime_meta.k8s_object_name
so the backend can re-derive URLs without re-hashing on every attach.
"""
import hashlib
import re

_PREFIX = "primer-ws-"
_MAX_LEN = 63
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _is_safe(id_: str) -> bool:
    return bool(_DNS_LABEL_RE.fullmatch(id_)) and (len(_PREFIX) + len(id_)) <= _MAX_LEN


def k8s_object_name(workspace_id: str) -> str:
    if _is_safe(workspace_id):
        return _PREFIX + workspace_id
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    return _PREFIX + digest

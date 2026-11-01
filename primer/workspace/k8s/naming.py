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
# A StatefulSet name must leave room for the controller's ``-<hash>`` revision label on its pod, which is also capped at 63: past 52 the pod never
# appears (kubernetes/kubernetes#64023). A Secret or Service could take 63, but the three share one name.
_MAX_STS_LEN = 52
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _is_safe(id_: str) -> bool:
    return bool(_DNS_LABEL_RE.fullmatch(id_)) and (len(_PREFIX) + len(id_)) <= _MAX_STS_LEN


def legacy_k8s_object_name(workspace_id: str) -> str | None:
    """The name an id of 43 to 53 characters had before the StatefulSet limit was honoured, or ``None`` when it never differed.

    A workspace created under that name keeps living under it: destroy and re-attach look for it when the current name is absent.
    """
    legacy = _PREFIX + workspace_id
    if bool(_DNS_LABEL_RE.fullmatch(workspace_id)) and _MAX_STS_LEN < len(legacy) <= _MAX_LEN:
        return legacy
    return None


def k8s_object_name(workspace_id: str) -> str:
    if _is_safe(workspace_id):
        return _PREFIX + workspace_id
    digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    return _PREFIX + digest

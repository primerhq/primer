"""3-way diff over RenderedEntry lists for harness sync."""

from __future__ import annotations

from dataclasses import dataclass, field

from primer.model.harness import RenderedEntry


class DiffOp:
    NOOP = "noop"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Diff:
    creates: list[RenderedEntry] = field(default_factory=list)
    updates: list[tuple[RenderedEntry, RenderedEntry]] = field(default_factory=list)
    deletes: list[RenderedEntry] = field(default_factory=list)
    noops: list[RenderedEntry] = field(default_factory=list)


def diff_renderings(
    old: list[RenderedEntry], new: list[RenderedEntry],
) -> Diff:
    """Diff by (kind, template_name)."""
    def key(e: RenderedEntry) -> tuple[str, str]:
        return (e.kind, e.template_name)

    old_idx = {key(e): e for e in old}
    new_idx = {key(e): e for e in new}

    d = Diff()
    for k, new_e in new_idx.items():
        old_e = old_idx.get(k)
        if old_e is None:
            d.creates.append(new_e)
        elif old_e.rendered_hash == new_e.rendered_hash:
            d.noops.append(new_e)
        else:
            d.updates.append((old_e, new_e))
    for k, old_e in old_idx.items():
        if k not in new_idx:
            d.deletes.append(old_e)
    return d


__all__ = ["Diff", "DiffOp", "diff_renderings"]

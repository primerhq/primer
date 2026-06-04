"""Build-time lint for the user-docs source tree.

See ``docs/superpowers/specs/2026-06-04-user-documentation-system-design.md``
section 10 for the rule list. Returns a list of structured
:class:`LintIssue` records. The doc service calls :func:`run_lint`
after building its index; the FastAPI lifespan handler decides what
to do with the result (block startup in dev mode, log loudly in
production).

Rules (severity in parens, ``error`` unless noted):

1. ``no_em_dash`` (error) -- U+2014 anywhere in the doc source.
2. ``broken_ref`` (error) -- ref:/ai-doc: target unresolved.
3. ``unknown_embed_id`` (error) -- mockup:<id> not in registry.
4. ``missing_frontmatter_key`` (error) -- required key missing;
   cookbook docs additionally require difficulty/time_minutes/tags.
5. ``duplicate_slug`` (error) -- two docs sharing a slug.
6. ``section_path_mismatch`` / ``reserved_section`` (error).
7. ``mermaid_unknown_type`` (error) -- mermaid block first line is
   not a recognised diagram type.
8. ``h1_in_body`` (error) -- '#' inside the body (h1 reserved for
   frontmatter title).
9. ``mockup_invalid_json`` (error) -- mockup body fails json.loads.
10. ``forbidden_token`` (warning) -- TODO / FIXME / xxx /
    'lorem ipsum' in body.

Docs under ``_meta/`` are exempt from every rule -- the authoring
guide is allowed to demonstrate forbidden patterns by example.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    from primer.user_docs_service import UserDocsService


Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintIssue:
    """One lint finding."""

    file: str
    line: int | None
    rule: str
    severity: Severity
    message: str
    suggestion: str | None = None


EM_DASH = "—"

_FORBIDDEN_TOKENS = ("TODO", "FIXME", "xxx", "lorem ipsum")

# Reserved section prefixes that may not appear in a doc's frontmatter
# `section:` field. Spec section 8.1.
RESERVED_SECTIONS = frozenset({"_ai", "_meta"})

REQUIRED_FRONTMATTER_KEYS = ("slug", "title", "summary", "section")
COOKBOOK_REQUIRED_KEYS = ("difficulty", "time_minutes", "tags")
COOKBOOK_DIFFICULTY_VALUES = frozenset(
    {"beginner", "intermediate", "advanced"}
)

_KNOWN_MERMAID_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram",
    "stateDiagram", "stateDiagram-v2", "erDiagram", "journey",
    "gantt", "pie", "requirementDiagram", "gitGraph", "mindmap",
    "timeline", "C4Context", "C4Container", "C4Component",
)

_DIRECTIVE_FENCE_RE = re.compile(r"^```([\w:./-]+)\s*$")
_DIRECTIVE_PREFIXES = (
    "mermaid", "mockup:", "callout:", "code-tabs:", "ref:", "ai-doc:",
)


def _is_directive(info_string: str) -> bool:
    for prefix in _DIRECTIVE_PREFIXES:
        if prefix.endswith(":") and info_string.startswith(prefix):
            return True
        if not prefix.endswith(":") and info_string == prefix:
            return True
    return False


def _iter_directives(body: str):
    """Yield ``(start_line, directive, payload_lines)`` for every fenced
    block whose info-string is a registered directive (mermaid bare, or
    one of the colon-prefixed kinds)."""
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        m = _DIRECTIVE_FENCE_RE.match(lines[i])
        if not m or not _is_directive(m.group(1)):
            i += 1
            continue
        directive = m.group(1)
        start = i + 1
        i = start
        buf: list[str] = []
        while i < len(lines) and not lines[i].startswith("```"):
            buf.append(lines[i])
            i += 1
        yield (start, directive, buf)
        i += 1  # past the closing fence (if any)


def _find_em_dash_lines(text: str) -> list[int]:
    return [
        i for i, line in enumerate(text.splitlines(), start=1)
        if EM_DASH in line
    ]


def _h1_lines(body: str) -> list[int]:
    """Lines that start with a single '# ' (not inside a fenced code
    block)."""
    out: list[int] = []
    in_fence = False
    for i, line in enumerate(body.splitlines(), start=1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("# ") and not line.startswith("## "):
            out.append(i)
    return out


def _frontmatter_text(fm: dict) -> str:
    """A best-effort rebuild of the frontmatter block as text for
    em-dash scanning. The lint reads the raw source by re-reading the
    file -- this stays simple and avoids leaking yaml serialisation."""
    parts = []
    for k, v in fm.items():
        parts.append(f"{k}: {v}")
    return "\n".join(parts)


def run_lint(
    svc: "UserDocsService",
    *,
    embeds_manifest: Sequence[str],
) -> list[LintIssue]:
    """Run every rule against every indexed entry in ``svc``.

    ``embeds_manifest`` is the list of valid embed ids; rule 3 uses
    this allowlist.
    """
    issues: list[LintIssue] = []
    seen_slugs: dict[str, str] = {}
    all_slugs = {e.slug for e in svc.all_entries()}
    entries_by_slug = {e.slug: e for e in svc.all_entries()}

    for entry in svc.all_entries():
        rel_path = str(entry.path.relative_to(svc._root))  # noqa: SLF001

        # Docs under _meta/ are exempt from every rule: the authoring
        # guide demonstrates patterns by example.
        rel_parts = entry.path.relative_to(svc._root).parts  # noqa: SLF001
        if rel_parts and rel_parts[0] == "_meta":
            continue

        # Rule 1: em-dash anywhere. Scan the raw source from disk so the
        # check is verbatim across body + frontmatter.
        try:
            raw = entry.path.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            raw = entry.body
        for line_no in _find_em_dash_lines(raw):
            issues.append(LintIssue(
                file=rel_path, line=line_no, rule="no_em_dash",
                severity="error",
                message=(
                    "em-dash (U+2014) found; use '-', '--', or "
                    "reword the sentence"
                ),
            ))

        # Rule 4: required frontmatter keys.
        for key in REQUIRED_FRONTMATTER_KEYS:
            if key not in entry.frontmatter:
                issues.append(LintIssue(
                    file=rel_path, line=None,
                    rule="missing_frontmatter_key", severity="error",
                    message=f"missing required frontmatter key: {key!r}",
                ))
        if entry.frontmatter.get("section") == "cookbook":
            for key in COOKBOOK_REQUIRED_KEYS:
                if key not in entry.frontmatter:
                    issues.append(LintIssue(
                        file=rel_path, line=None,
                        rule="missing_frontmatter_key",
                        severity="error",
                        message=(
                            f"cookbook doc missing required key: {key!r}"
                        ),
                    ))
            diff = entry.frontmatter.get("difficulty")
            if diff is not None and diff not in COOKBOOK_DIFFICULTY_VALUES:
                issues.append(LintIssue(
                    file=rel_path, line=None,
                    rule="invalid_difficulty", severity="error",
                    message=(
                        f"difficulty must be one of "
                        f"{sorted(COOKBOOK_DIFFICULTY_VALUES)}, got "
                        f"{diff!r}"
                    ),
                ))

        # Rule 5: slug uniqueness. The frontmatter `slug` value must be
        # unique across the tree even when docs live in different
        # sections.
        fm_slug = entry.frontmatter.get("slug")
        if fm_slug is not None:
            if fm_slug in seen_slugs:
                issues.append(LintIssue(
                    file=rel_path, line=None, rule="duplicate_slug",
                    severity="error",
                    message=(
                        f"slug {fm_slug!r} already used by "
                        f"{seen_slugs[fm_slug]}"
                    ),
                ))
            else:
                seen_slugs[fm_slug] = rel_path

        # Rule 6: file location matches frontmatter section.
        on_disk_section = rel_parts[0] if rel_parts else ""
        declared_section = entry.frontmatter.get("section", on_disk_section)
        if declared_section in RESERVED_SECTIONS:
            issues.append(LintIssue(
                file=rel_path, line=None, rule="reserved_section",
                severity="error",
                message=(
                    f"section {declared_section!r} is reserved and may "
                    f"not appear in frontmatter"
                ),
            ))
        elif declared_section != on_disk_section:
            issues.append(LintIssue(
                file=rel_path, line=None, rule="section_path_mismatch",
                severity="error",
                message=(
                    f"frontmatter section={declared_section!r} but file "
                    f"is under {on_disk_section!r}; move the file or "
                    f"correct the frontmatter"
                ),
            ))

        # Rule 8: no h1 in body.
        for line_no in _h1_lines(entry.body):
            issues.append(LintIssue(
                file=rel_path, line=line_no, rule="h1_in_body",
                severity="error",
                message=(
                    "body must start at '##' (h2); title comes from "
                    "frontmatter"
                ),
            ))

        # Rule 10: forbidden tokens (warning only). One warning per
        # token per file.
        body_lower = entry.body.lower()
        for tok in _FORBIDDEN_TOKENS:
            if tok.lower() in body_lower:
                for i, line in enumerate(
                    entry.body.splitlines(), start=1,
                ):
                    if tok.lower() in line.lower():
                        issues.append(LintIssue(
                            file=rel_path, line=i,
                            rule="forbidden_token", severity="warning",
                            message=(
                                f"forbidden token {tok!r} present; "
                                f"finish before shipping"
                            ),
                        ))
                        break

        # Rules 2, 3, 7, 9: directive walks.
        for start_line, directive, payload_lines in _iter_directives(
            entry.body,
        ):
            if directive.startswith("ref:"):
                target = directive[len("ref:"):]
                slug_part, _, anchor = target.partition("#")
                if slug_part not in all_slugs:
                    candidates = [
                        s for s in all_slugs
                        if slug_part.split("/")[-1] in s
                    ]
                    sug = (
                        f"did you mean {candidates[0]!r}?"
                        if candidates else None
                    )
                    issues.append(LintIssue(
                        file=rel_path, line=start_line - 1,
                        rule="broken_ref", severity="error",
                        message=f"ref target {slug_part!r} not found",
                        suggestion=sug,
                    ))
                elif anchor:
                    target_entry = entries_by_slug.get(slug_part)
                    valid_anchors = (
                        {h["anchor"] for h in target_entry.headings}
                        if target_entry else set()
                    )
                    if anchor not in valid_anchors:
                        issues.append(LintIssue(
                            file=rel_path, line=start_line - 1,
                            rule="broken_ref", severity="error",
                            message=(
                                f"anchor #{anchor} not found in "
                                f"{slug_part}; valid anchors: "
                                f"{sorted(valid_anchors)}"
                            ),
                        ))
            elif directive.startswith("ai-doc:"):
                slug_part = directive[len("ai-doc:"):]
                # noqa: SLF001 — accessing the service's root path is
                # the documented contract for the lint runner.
                ai_doc_path = (
                    svc._root.parent / "ai_docs" / f"{slug_part}.md"
                )  # noqa: SLF001
                if not ai_doc_path.exists():
                    issues.append(LintIssue(
                        file=rel_path, line=start_line - 1,
                        rule="broken_ref", severity="error",
                        message=(
                            f"ai-doc target primer/ai_docs/{slug_part}.md "
                            f"not found"
                        ),
                    ))
            elif directive.startswith("mockup:"):
                embed_id = directive[len("mockup:"):]
                if embed_id not in embeds_manifest:
                    issues.append(LintIssue(
                        file=rel_path, line=start_line - 1,
                        rule="unknown_embed_id", severity="error",
                        message=(
                            f"embed id {embed_id!r} not registered; "
                            f"valid ids: {sorted(embeds_manifest)}"
                        ),
                    ))
                # Rule 9: validate JSON body when non-empty.
                payload = "\n".join(payload_lines).strip()
                if payload:
                    try:
                        json.loads(payload)
                    except json.JSONDecodeError as exc:
                        issues.append(LintIssue(
                            file=rel_path,
                            line=start_line + exc.lineno - 1,
                            rule="mockup_invalid_json", severity="error",
                            message=(
                                f"mockup directive has malformed JSON: "
                                f"{exc.msg}"
                            ),
                        ))
            elif directive == "mermaid":
                first = next(
                    (ln for ln in payload_lines if ln.strip()), "",
                ).strip()
                first_token = first.split()[0] if first else ""
                if first_token and not any(
                    first_token.startswith(t)
                    for t in _KNOWN_MERMAID_TYPES
                ):
                    issues.append(LintIssue(
                        file=rel_path, line=start_line,
                        rule="mermaid_unknown_type", severity="error",
                        message=(
                            f"mermaid: first line must start with a "
                            f"known diagram type; got {first_token!r}. "
                            f"Known: "
                            f"{', '.join(sorted(_KNOWN_MERMAID_TYPES))}"
                        ),
                    ))

    return issues


__all__ = [
    "COOKBOOK_DIFFICULTY_VALUES",
    "COOKBOOK_REQUIRED_KEYS",
    "EM_DASH",
    "LintIssue",
    "REQUIRED_FRONTMATTER_KEYS",
    "RESERVED_SECTIONS",
    "Severity",
    "run_lint",
]

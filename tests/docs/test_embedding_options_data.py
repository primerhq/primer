"""The embedding-model option set is data (a wiki page), not code.

The enable-internal-collections flow presents this page's table to the
user, so the table must stay well-formed: the three memory tiers exactly
once, well-formed HuggingFace ids, numeric memory/speed columns ordered
small < mid < large, the mid row pinned to the model a clean install
seeds (the page and the seed must not drift apart), and the OQ-2 escape
hatches present (any HuggingFace model id; the small tier is the
documented safe pick, never a silent default).
"""

from __future__ import annotations

import re

from tests.docs._wiki import WIKI_DIR

PAGE = WIKI_DIR / "providers" / "embedding-model-options.md"

# A HuggingFace model id is `org/name` (one slash; both parts non-empty).
_HF_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TIERS = ("small", "mid", "large")


def _data_rows(text: str) -> list[dict[str, str]]:
    """The option-set table rows as {column: cell} dicts (header-ordered)."""
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    assert len(lines) >= 4, (
        f"the option table needs a header, a separator and >= 1 row; found "
        f"{len(lines)} table lines in {PAGE.name}"
    )
    header = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        assert len(cells) == len(header), (
            f"row cell count {len(cells)} != header count {len(header)}: {ln!r}"
        )
        rows.append(dict(zip(header, cells)))
    return rows


def _mem_mb(cell: str) -> float:
    """A memory cell ('893 MB', '3.9 GB', '~160 MB') as MB."""
    m = re.match(r"^\s*~?\s*([\d.]+)\s*(MB|GB)\b", cell.replace(",", ""))
    assert m, f"unparseable memory cell: {cell!r}"
    value = float(m.group(1))
    return value * 1000.0 if m.group(2) == "GB" else value


def _speed_s(cell: str) -> float:
    """A speed cell ('0.8 s', '20 s (load 92 s)') as seconds - the first number is the embed time."""
    m = re.match(r"^\s*~?\s*([\d.]+)\s*s\b", cell.replace(",", ""))
    assert m, f"unparseable speed cell: {cell!r}"
    return float(m.group(1))


def test_the_options_page_exists() -> None:
    assert PAGE.is_file(), f"{PAGE} is missing - the D2 option set has no home"


def test_three_tiers_exactly_once() -> None:
    rows = _data_rows(PAGE.read_text())
    tiers = [r["tier"] for r in rows]
    assert sorted(tiers) == sorted(_TIERS), (
        f"the option set spans the three memory tiers exactly once; got {tiers!r}"
    )


def test_each_row_is_well_formed() -> None:
    rows = _data_rows(PAGE.read_text())
    for row in rows:
        model = row["model id"].strip("`")
        assert _HF_ID.match(model), (
            f"model id {model!r} is not a HuggingFace `org/name` id"
        )
        assert row["dim"].isdigit(), f"{model!r}: dim must be an int; got {row['dim']!r}"
        _mem_mb(row["peak RSS"])
        _mem_mb(row["model delta"])
        _speed_s(row["150 docs"])


def test_memory_orders_small_below_mid_below_large() -> None:
    rows = {r["tier"]: r for r in _data_rows(PAGE.read_text())}
    small = _mem_mb(rows["small"]["peak RSS"])
    mid = _mem_mb(rows["mid"]["peak RSS"])
    large = _mem_mb(rows["large"]["peak RSS"])
    assert small < mid < large, (
        f"the tiers must order by measured peak RSS (small < mid < large); "
        f"got small={small}, mid={mid}, large={large}"
    )


def test_the_mid_row_pins_the_seeded_model() -> None:
    """The page and the install seed must not drift apart.

    The mid tier is the model a clean install seeds under the reserved
    `huggingface` provider (bootstrap/defaults.py); the flow promises
    "choosing it adds no registration and no download", so the page's
    mid row must name exactly the seeded model.
    """
    from primer.bootstrap.defaults import (
        RESERVED_EMBEDDERS,
        RESERVED_HUGGINGFACE_EMBEDDER,
    )

    seeded = RESERVED_EMBEDDERS[RESERVED_HUGGINGFACE_EMBEDDER]
    seeded_models = [m["name"] for m in seeded["models"]]
    rows = {r["tier"]: r for r in _data_rows(PAGE.read_text())}
    mid_model = rows["mid"]["model id"].strip("`")
    assert mid_model in seeded_models, (
        f"the mid row names {mid_model!r} but the install seeds "
        f"{seeded_models!r} - the 'no registration, no download' promise "
        f"would break; update one or the other (the spec's W1-14 tiers "
        f"span small/mid/large; the seed is the mid reference)"
    )


def test_the_oq2_escape_hatches_are_documented() -> None:
    text = PAGE.read_text()
    assert "ANY HuggingFace model id" in text, (
        "OQ-2: the user may name any HuggingFace model id; the page must "
        "carry the escape hatch (check -> test -> register via the "
        "embedding provider tools)"
    )
    assert "just pick one" in text, (
        "OQ-2: no silent default - the small tier is the documented safe "
        "pick when the user says 'just pick one'; the page must name it"
    )


def test_the_reranker_is_documented_as_optional() -> None:
    text = PAGE.read_text()
    assert "optional" in text.lower(), (
        "the reranker stays optional (the W1-06 red anchor): the page must "
        "say so where it names the reference reranker"
    )

"""Rules for the stored-model drift checker (scripts/check_model_drift.py).

The checker exists because three shape changes shipped across the session
cutover with the MIGRATIONS registry untouched, and each one made an
existing install unreadable. These tests pin the two rules that separate a
breaking change from a harmless one, and the parsing that feeds them.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_model_drift",
    Path(__file__).resolve().parents[2] / "scripts" / "check_model_drift.py",
)
drift = importlib.util.module_from_spec(_SPEC)
# Register before exec: @dataclass resolves its module through
# sys.modules[cls.__module__], which is None for a module that is still
# only a spec, and the decorator raises on an attribute of None.
sys.modules[_SPEC.name] = drift
_SPEC.loader.exec_module(drift)


def _field(src: str) -> ast.AnnAssign:
    """Parse a single annotated assignment out of a class body."""
    tree = ast.parse(f"class C:\n    {src}\n")
    return tree.body[0].body[0]


class TestRequiredDetection:
    @pytest.mark.parametrize("src", [
        "x: str",
        "x: str = Field(...)",
        "x: str = Field(..., min_length=1)",
        # Field() with only metadata and no default is required, the same
        # as a bare annotation.
        "x: str = Field(description='a thing')",
    ])
    def test_required_shapes(self, src):
        assert drift._is_required(_field(src)) is True

    @pytest.mark.parametrize("src", [
        "x: str = Field(default='a')",
        "x: list = Field(default_factory=list)",
        "x: str | None = Field(default=None, description='a thing')",
        "x: int = 3",
    ])
    def test_optional_shapes(self, src):
        assert drift._is_required(_field(src)) is False

    def test_multiline_field_is_not_missed(self):
        """The house style wraps Field( onto the next line.

        A regex over `Field(` reports these as optional, which silently
        under-reports exactly the rule that matters most.
        """
        node = _field("x: str = Field(\n        ..., min_length=1,\n    )")
        assert drift._is_required(node) is True


class TestExtraPolicy:
    def test_forbid_is_detected(self):
        cls = ast.parse(
            'class C:\n    model_config = ConfigDict(extra="forbid")\n'
        ).body[0]
        assert drift._forbids_extra(cls) is True

    def test_absent_config_is_not_forbid(self):
        cls = ast.parse("class C:\n    x: int = 1\n").body[0]
        assert drift._forbids_extra(cls) is False


class TestReachability:
    def test_nested_types_are_pulled_in_from_a_stored_root(self):
        edges = {"Channel": {"SlackChannelConfig"},
                 "SlackChannelConfig": {"ChatConfig"},
                 "Unrelated": {"AlsoUnrelated"}}
        reached = drift._reachable({"Channel"}, edges)
        assert "ChatConfig" in reached, "nested blocks must be in scope"
        assert "Unrelated" not in reached


def _shape(name, *, required=(), optional=(), forbid=False):
    return drift.ClassShape(
        name=name, path="primer/model/x.py", forbids_extra=forbid,
        required=set(required), optional=set(optional),
    )


class TestRules:
    def test_new_required_field_is_breaking(self):
        """Document gained a required slug; every existing row failed."""
        breaking, _ = drift.compare(
            {"Document": _shape("Document", required=["path"])},
            {"Document": _shape("Document", required=["path", "slug"])},
        )
        assert [b["field"] for b in breaking] == ["slug"]

    def test_optional_becoming_required_is_breaking(self):
        breaking, _ = drift.compare(
            {"C": _shape("C", optional=["x"])},
            {"C": _shape("C", required=["x"])},
        )
        assert breaking[0]["rule"].startswith("existing optional field")

    def test_removal_under_forbid_is_breaking(self):
        """ChatConfig forbids extras, so the retired keys raised on load."""
        breaking, benign = drift.compare(
            {"ChatConfig": _shape("ChatConfig", optional=["enabled", "default_agent"], forbid=True)},
            {"ChatConfig": _shape("ChatConfig", optional=["enabled"], forbid=True)},
        )
        assert [b["field"] for b in breaking] == ["default_agent"]
        assert benign == []

    def test_removal_under_ignore_is_benign(self):
        """The identical removal on a class that ignores extras is fine.

        This is why the policy has to be read per class: Collection dropped
        two top-level fields harmlessly in the same commit that ChatConfig
        broke on three.
        """
        breaking, benign = drift.compare(
            {"Collection": _shape("Collection", optional=["embedder", "system"])},
            {"Collection": _shape("Collection", optional=["system"])},
        )
        assert breaking == []
        assert [b["field"] for b in benign] == ["embedder"]

    def test_disappearing_class_is_surfaced(self):
        """A rename escapes a both-refs comparison entirely.

        CollectionSearch became CollectionSearchConfig with two newly
        required fields. Comparing only shared names reported that as two
        benign removals and missed the defect that broke every collection.
        """
        breaking, _ = drift.compare(
            {"CollectionSearch": _shape("CollectionSearch", optional=["cer"])},
            {},
        )
        assert breaking[0]["model"] == "CollectionSearch"
        assert breaking[0]["field"] == "*"

    def test_unchanged_shapes_report_nothing(self):
        breaking, benign = drift.compare(
            {"C": _shape("C", required=["a"], optional=["b"])},
            {"C": _shape("C", required=["a"], optional=["b"])},
        )
        assert (breaking, benign) == ([], [])

"""What an approval card draws first: the lead paths a tool declares (``Tool.preview_lead``) and the fingerprints the park stamps (review of #860,
R860-4 and R860-5).

The park (``primer.agent.approval.resolve_preview``) stamps ``lead`` and, for each lead path that holds text, ``digests[path]`` (``sha256 <12 hex>,
<N> lines``); the card (``_approval_preview``) draws each lead path before the rest: its value when the allowlist shows it, followed by the
fingerprint, or the fingerprint alone when the allowlist withholds it. The card never reads a withheld value to make one.
"""

from __future__ import annotations

from primer.api.routers.workspaces import _approval_preview
from primer.common.preview_allowlist import _preview_stamp, lead_digests, text_digest

SOURCE = "def f():\n    return 'Zq7!pLm2$vXr9&kTe4'\n"
CALL = {"name": "system__create_toolset", "arguments": {"entity": {"id": "py", "provider": "python", "config": {"source": SOURCE}}}}


def test_the_park_fingerprints_each_lead_path_that_holds_text():
    digests = lead_digests(["entity.config.source", "entity.id", "entity.missing", "entity.config"], CALL["arguments"])

    assert digests == {"entity.config.source": text_digest(SOURCE), "entity.id": text_digest("py")}
    assert text_digest(SOURCE).startswith("sha256 ") and text_digest(SOURCE).endswith(", 2 lines")


def test_a_stamp_keeps_its_lead_and_only_well_formed_fingerprints_of_lead_paths():
    stamp = _preview_stamp({"preview": {
        "paths": ["entity.id"], "source": "tool", "lead": ["entity.config.source"],
        "digests": {"entity.config.source": text_digest(SOURCE), "entity.id": text_digest("py")},
    }})
    forged = _preview_stamp({"preview": {
        "paths": ["entity.id"], "source": "tool", "lead": ["entity.config.source"], "digests": {"entity.config.source": "def f(): leaked"},
    }})

    assert stamp == {"paths": ["entity.id"], "source": "tool", "lead": ["entity.config.source"], "digests": {"entity.config.source": text_digest(SOURCE)}}
    assert forged["digests"] == {}, "a fingerprint that is not one is not drawn"


def test_a_withheld_lead_path_is_drawn_as_its_fingerprint_and_a_shown_one_as_its_value_first():
    hidden = _approval_preview(CALL, {"paths": ["entity.id"], "source": "tool", "lead": ["entity.config.source"],
                                      "digests": {"entity.config.source": text_digest(SOURCE)}})
    shown = _approval_preview(CALL, {"paths": ["entity.id", "entity.provider"], "source": "tool", "lead": ["entity.provider", "entity.absent"]})

    assert hidden["arguments"].startswith(f"entity.config.source=<{text_digest(SOURCE)}>, entity="), hidden
    assert "Zq7" not in hidden["arguments"] and "entity.config" in hidden["hidden_keys"]
    assert shown["arguments"].startswith("entity.provider=python, entity="), shown


def test_a_stamp_with_no_lead_draws_as_before():
    plain = _approval_preview(CALL, {"paths": ["entity.id"], "source": "tool"})

    assert plain["arguments"].startswith("entity="), plain

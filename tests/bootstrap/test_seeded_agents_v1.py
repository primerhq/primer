"""The freezes: every seeded definition a build of main shipped, and the current one.

``primer.bootstrap.seeded_agents_v1`` holds verbatim copies of the
definitions earlier builds seeded. Migrations 11 and 15 upgrade a stored
row only while it still equals one of them, so no copy may drift: a
reworded description, a changed tool set or a new prompt line changes the
digest and fails this file.

The last test pins the CURRENT definitions (audit G7-10). The ensure pass
never overwrites a row, so a change to a seeded definition reaches fresh
installs only; an install seeded before it keeps the old one unless the
old one is frozen here and a migration moves it.
"""
from __future__ import annotations

from primer.bootstrap.operator_defaults import SEEDED_AGENT_FACTORIES
from primer.bootstrap.seeded_agents_v1 import (
    PREVIOUS_SHIPPED,
    SEEDED_AGENTS_AUGUST,
    SEEDED_AGENTS_V1,
    SEEDED_AGENTS_V2,
    SeededDefinition,
    definition_digest,
)

#: The sha256 of each definition the current factories build.
CURRENT_DIGESTS = {
    "operator": "934facec1314713b992b6793e98d6f09fad721226501a0ce22c3f9fc61ba7bd1",
    "builder": "f09b4c2df4612ebf8589731c1a48cabb9d329338a68ac8ed71fcb4a44bf49d97",
    "planner": "0f8739cd3a84e5d1adc16a518ea1a6fbda0273b243a274a7925f45bd4a5574e3",
    "explorer": "967cb562101ae11d52dab5ea50d20ddf8e3187d602e63c30ba2d0561cc42dc41",
    "tool-runner": "231b3d359b4165d72673b6b7c6a4c4890d098ca2ef5e390321e0732f6da9f602",
}

_V1_DIGESTS = {
    "operator": "392a1ac405e790ffed8eee8f2a077743c7ad81d6ac41ff4b12833843c59969c4",
    "builder": "77350429bf2f31f66d996900ce80896abe2b3ebb24ff47132953e23503ea961d",
    "planner": "6a659aaea802d72bb5e29a801878aa8ad7093d0a3514d92ddcbcf786647774ed",
    "explorer": "74aab5d24209667846fc3eb693e218287485bd114c40afd34e07c9bf63246b7a",
    "tool-runner": "f022c9e654fa0cdff8256d39c8893ee09a71f77377fa52521110e7fc3a9e4c79",
}


def _current() -> dict[str, SeededDefinition]:
    out = {}
    for agent_id, factory in SEEDED_AGENT_FACTORIES.items():
        agent = factory("p")
        out[agent_id] = SeededDefinition(
            agent.description, tuple(agent.system_prompt), tuple(agent.tools),
        )
    return out


def test_v1_is_the_previous_shipped_definition():
    assert {k: definition_digest(v) for k, v in SEEDED_AGENTS_V1.items()} == _V1_DIGESTS


def test_the_august_builds_are_frozen_as_seeded_after_migration_6():
    """The 2026-08-19 to 08-22 builds, keyed by the commit that shipped each (audit G7-04).

    The planner, explorer and tool-runner those builds seeded equal v1's;
    the operator and builder do not, which is why migration 11 left them.
    """
    assert {
        build: {agent_id: definition_digest(d) for agent_id, d in definitions.items()}
        for build, definitions in SEEDED_AGENTS_AUGUST.items()
    } == {
        "5c78cca62": {
            "operator": "4ad803f4c76294ba04b159a9fc8690c261b74d5a6998c154713aa99e1e938a41",
            "builder": "bb1e713d9e3b7f57dc55c9cc167e25e3c732166239261a06fd18e1ebd66e13d2",
        },
        "64704ff5d": {
            "operator": "573119f69dc3ec977fa460d2df9c363fe008152dda2edf967d61096793b59b93",
            "builder": "bb1e713d9e3b7f57dc55c9cc167e25e3c732166239261a06fd18e1ebd66e13d2",
        },
        "e31fc5e18": {
            "operator": "573119f69dc3ec977fa460d2df9c363fe008152dda2edf967d61096793b59b93",
            "builder": "bb1e713d9e3b7f57dc55c9cc167e25e3c732166239261a06fd18e1ebd66e13d2",
            "planner": _V1_DIGESTS["planner"],
            "explorer": _V1_DIGESTS["explorer"],
            "tool-runner": _V1_DIGESTS["tool-runner"],
        },
        "8d8b10dd9": {
            "operator": "e4da886306a5a9f6aa58d28f2c93c3e8d010e1268be61426bfd759ac56291e4f",
            "builder": "e6868752255899099f12a81045c0091d566e7171b8163b1af63965756fd59dd8",
            "planner": _V1_DIGESTS["planner"],
            "explorer": _V1_DIGESTS["explorer"],
            "tool-runner": _V1_DIGESTS["tool-runner"],
        },
    }


def test_the_august_grants_are_as_migration_6_left_them():
    """Migration 6 runs before 11 and 15, so no stored grant still names a retired search tool."""
    retired = {"collections__grep_collection", "collections__semantic_search"}
    for definitions in SEEDED_AGENTS_AUGUST.values():
        for agent_id, definition in definitions.items():
            assert not retired & set(definition.tools), agent_id
            assert set(definition.tools) == set(SEEDED_AGENTS_V1[agent_id].tools), agent_id


def test_v2_is_the_core_kit_definition_migration_11_wrote():
    assert {k: definition_digest(v) for k, v in SEEDED_AGENTS_V2.items()} == {
        "operator": "934facec1314713b992b6793e98d6f09fad721226501a0ce22c3f9fc61ba7bd1",
        "builder": "f09b4c2df4612ebf8589731c1a48cabb9d329338a68ac8ed71fcb4a44bf49d97",
        "planner": "0f8739cd3a84e5d1adc16a518ea1a6fbda0273b243a274a7925f45bd4a5574e3",
        "explorer": "967cb562101ae11d52dab5ea50d20ddf8e3187d602e63c30ba2d0561cc42dc41",
        "tool-runner": "231b3d359b4165d72673b6b7c6a4c4890d098ca2ef5e390321e0732f6da9f602",
    }


def test_previous_shipped_holds_every_shipped_definition_once_in_shipping_order():
    builds = (*SEEDED_AGENTS_AUGUST.values(), SEEDED_AGENTS_V1, SEEDED_AGENTS_V2)
    assert set(PREVIOUS_SHIPPED) == set(SEEDED_AGENT_FACTORIES)
    for agent_id, lineage in PREVIOUS_SHIPPED.items():
        expected: list[str] = []
        for build in builds:
            if agent_id in build and definition_digest(build[agent_id]) not in expected:
                expected.append(definition_digest(build[agent_id]))
        assert [definition_digest(d) for d in lineage] == expected, agent_id


def test_the_current_definitions_are_pinned():
    """Audit G7-10: a change to a seeded definition must come with its upgrade path."""
    current = {k: definition_digest(v) for k, v in _current().items()}
    assert current == CURRENT_DIGESTS, (
        "A seeded agent's shipped definition changed. The ensure pass never "
        "overwrites a stored row, so every install seeded before this change "
        "keeps the old definition unless you: (1) freeze the OLD definition, "
        "verbatim, into PREVIOUS_SHIPPED in primer/bootstrap/seeded_agents_v1.py "
        "and pin its digest in this file; (2) add a migration that moves a row "
        "still equal to a previously shipped definition to the current one, as "
        "migration 15 does; (3) then update CURRENT_DIGESTS. Changed: "
        f"{sorted(k for k in current if current[k] != CURRENT_DIGESTS.get(k))}"
    )

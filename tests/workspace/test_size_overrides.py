"""Which workspace provider can honour which per-workspace size, and the 422 for one it cannot (platform revamp 2026-10,
plan 08 task 4; master Review Focus 5).

``overrides.resources`` sizes one workspace (``cpu_cores``, ``memory_bytes``, ``disk_bytes``). Kubernetes takes all three. Docker
takes CPU and memory but has no disk quota. A Local workspace runs on the server itself and can be sized by none. A size the
provider cannot honour is refused with a 422 that names the field, never silently ignored.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pydantic
import pytest

from primer.model.common import STORAGE_LOAD_CONTEXT
from primer.model.except_ import ValidationError
from primer.model.workspace import (
    ContainerTemplateConfig,
    disk_quantity_problem,
    parse_k8s_quantity,
    KubernetesTemplateConfig,
    ResourceLimits,
    WorkspaceProviderType,
    WorkspaceResourceOverrides,
    WorkspaceTemplate,
    WorkspaceTemplateOverrides,
)
from primer.workspace import size_overrides
from primer.workspace.size_overrides import (
    MAX_WORKSPACE_CPU_CORES,
    MAX_WORKSPACE_MEMORY_BYTES,
    UNSUPPORTED_RESOURCE_OVERRIDES,
    ResourceOverrideRefused,
    TemplateSizeRefused,
    refuse_oversized_template_sizes,
    refuse_unsupported_resource_overrides,
)

_ALL = WorkspaceResourceOverrides(cpu_cores=2, memory_bytes=2**30, disk_bytes=10 * 2**30)
_DOCKER_DISK = "Docker cannot limit a workspace's disk. Remove this override."
_LOCAL = "A Local workspace runs on the server itself and cannot be sized. Remove this override."


def _o(**sizes) -> WorkspaceTemplateOverrides:
    return WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(**sizes))


def test_kubernetes_honours_every_size_field():
    refuse_unsupported_resource_overrides("kubernetes", WorkspaceTemplateOverrides(resources=_ALL))   # does not raise


def test_docker_refuses_disk_only():
    with pytest.raises(ResourceOverrideRefused) as exc:
        refuse_unsupported_resource_overrides("container", WorkspaceTemplateOverrides(resources=_ALL))
    assert exc.value.fields == ["disk_bytes"]
    assert exc.value.provider_kind == "container"
    refuse_unsupported_resource_overrides("container", _o(cpu_cores=1.5))                     # cpu alone passes
    refuse_unsupported_resource_overrides("container", _o(memory_bytes=2**31))                # memory alone passes
    refuse_unsupported_resource_overrides("container", _o(cpu_cores=1, memory_bytes=2**30))   # both pass


def test_local_refuses_every_size_field():
    everything = ["cpu_cores", "memory_bytes", "disk_bytes"]
    with pytest.raises(ResourceOverrideRefused) as exc:
        refuse_unsupported_resource_overrides("local", WorkspaceTemplateOverrides(resources=_ALL))
    assert exc.value.fields == everything
    for field in everything:
        value = {"cpu_cores": 1, "memory_bytes": 2**30, "disk_bytes": 2**30}[field]
        with pytest.raises(ResourceOverrideRefused) as one:
            refuse_unsupported_resource_overrides("local", _o(**{field: value}))
        assert one.value.fields == [field]


def test_the_fields_come_in_the_models_order_whatever_order_they_were_given():
    with pytest.raises(ResourceOverrideRefused) as exc:
        refuse_unsupported_resource_overrides("local", _o(disk_bytes=2**30, cpu_cores=1))
    assert exc.value.fields == ["cpu_cores", "disk_bytes"]


@pytest.mark.parametrize("kind", sorted(UNSUPPORTED_RESOURCE_OVERRIDES))
def test_no_resources_is_never_refused(kind):
    refuse_unsupported_resource_overrides(kind, None)
    refuse_unsupported_resource_overrides(kind, WorkspaceTemplateOverrides())
    refuse_unsupported_resource_overrides(kind, WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides()))   # {} sets nothing


def test_the_refusal_is_a_422_with_one_error_per_field():
    with pytest.raises(ValidationError) as exc:
        refuse_unsupported_resource_overrides("container", WorkspaceTemplateOverrides(resources=_ALL))
    assert isinstance(exc.value, ResourceOverrideRefused)
    assert exc.value.problem_extensions == {"errors": [{
        "loc": ["body", "overrides", "resources", "disk_bytes"], "msg": _DOCKER_DISK, "type": "unsupported_by_provider",
    }]}
    assert exc.value.message == f"overrides.resources.disk_bytes: {_DOCKER_DISK}"

    with pytest.raises(ResourceOverrideRefused) as local:
        refuse_unsupported_resource_overrides("local", WorkspaceTemplateOverrides(resources=_ALL))
    assert [(e["loc"][-1], e["msg"], e["type"]) for e in local.value.problem_extensions["errors"]] == [
        ("cpu_cores", _LOCAL, "unsupported_by_provider"),
        ("memory_bytes", _LOCAL, "unsupported_by_provider"),
        ("disk_bytes", _LOCAL, "unsupported_by_provider"),
    ]
    assert local.value.message == (
        "overrides.resources.cpu_cores, overrides.resources.memory_bytes, overrides.resources.disk_bytes: " + _LOCAL
    )


def test_the_rules_cover_every_provider_kind_the_model_has():
    """A provider kind added to the model without a row here would let a size through silently."""
    assert set(UNSUPPORTED_RESOURCE_OVERRIDES) == {t.value for t in WorkspaceProviderType}


def test_the_rules_only_name_fields_the_override_model_has():
    named = set().union(*UNSUPPORTED_RESOURCE_OVERRIDES.values())
    assert named <= set(WorkspaceResourceOverrides.model_fields)
    assert size_overrides.SIZE_FIELDS == tuple(WorkspaceResourceOverrides.model_fields)


def test_a_provider_kind_with_no_rule_fails_loudly_instead_of_ignoring_the_size():
    from primer.model.except_ import ConfigError

    with pytest.raises(ConfigError, match="mystery"):
        refuse_unsupported_resource_overrides("mystery", _o(cpu_cores=1))
    refuse_unsupported_resource_overrides("mystery", None)   # nothing asked for, nothing to refuse


# ---- lead ruling 17 of plan 08 (from the review of #782): an absurd size the TEMPLATE sets ---------------------------------
#
# The override fields carry their caps in the model (a 422 with the field in ``loc``). A template's own CPU and memory are older
# fields, so a cap cannot go on the model without making a stored row fail to load; instead the size the workspace will really
# get (the override's, else the template's) is checked at create time, before any backend runs. A template that sets 1e308
# cores came back as a generic 500 after a rolled-back create.


def _k8s(cpu=None, memory=None) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="big", provider_id="k3s", description="d", backend=KubernetesTemplateConfig(image="rt:1"),
        resources=ResourceLimits(cpu_cores=cpu, memory_bytes=memory),
    )


def _docker(cpu=None, memory=None) -> WorkspaceTemplate:
    return WorkspaceTemplate(
        id="dock", provider_id="dock", description="d", backend=ContainerTemplateConfig(cpu_cores=cpu, memory_bytes=memory),
    )


def test_the_caps_are_re_exported_from_the_size_module():
    assert (MAX_WORKSPACE_CPU_CORES, MAX_WORKSPACE_MEMORY_BYTES) == (1024, 64 * 2**40)


def test_a_template_within_the_caps_is_never_refused():
    refuse_oversized_template_sizes(_k8s(), None)
    refuse_oversized_template_sizes(_k8s(cpu=1024, memory=64 * 2**40), None)             # the caps themselves are allowed
    refuse_oversized_template_sizes(_docker(cpu=0.5, memory=2**30), WorkspaceTemplateOverrides())
    refuse_oversized_template_sizes(WorkspaceTemplate(id="loc", provider_id="loc", description="d"), None)


@pytest.mark.parametrize("cpu", [1024.5, 1e308, float("inf"), float("nan")])
def test_a_kubernetes_template_with_an_absurd_cpu_is_refused_naming_the_field(cpu):
    # nan fails the model's gt=0 since the G5 audit, so a template can only hold it as a row stored before.
    template = _k8s(cpu=cpu) if cpu == cpu else WorkspaceTemplate.model_validate(
        {**_k8s().model_dump(), "resources": {"cpu_cores": cpu}}, context=STORAGE_LOAD_CONTEXT,
    )
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(template, None)
    assert isinstance(exc.value, ValidationError)
    assert exc.value.fields == ["resources.cpu_cores"] and exc.value.template_id == "big"
    assert "template 'big'" in exc.value.message and "resources.cpu_cores" in exc.value.message
    assert "1024 CPU cores" in exc.value.message and "Edit the template" in exc.value.message
    assert exc.value.problem_extensions == {}      # the value is in the template, not in the request: no `loc` to give


@pytest.mark.parametrize("memory", [64 * 2**40 + 1, 10**30])
def test_a_kubernetes_template_with_an_absurd_memory_is_refused(memory):
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_k8s(memory=memory), None)
    assert exc.value.fields == ["resources.memory_bytes"]
    assert "64 TiB" in exc.value.message


def test_a_docker_template_names_the_backend_fields():
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_docker(cpu=1e308, memory=10**30), None)
    assert exc.value.fields == ["backend.cpu_cores", "backend.memory_bytes"]
    assert "backend.cpu_cores" in exc.value.message and "backend.memory_bytes" in exc.value.message


def test_an_override_that_replaces_the_absurd_field_makes_it_irrelevant():
    """The workspace gets the override's size, so what the template says in that field is not what runs."""
    over = WorkspaceTemplateOverrides(resources=WorkspaceResourceOverrides(cpu_cores=2))

    refuse_oversized_template_sizes(_k8s(cpu=1e308), over)
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(_k8s(cpu=1e308, memory=10**30), over)      # memory is still the template's
    assert exc.value.fields == ["resources.memory_bytes"]


def test_a_local_template_is_not_checked_because_local_ignores_its_size():
    local = WorkspaceTemplate(id="loc", provider_id="loc", description="d", resources=ResourceLimits(cpu_cores=1e308))

    refuse_oversized_template_sizes(local, None)


# ---- the G5 audit: the template's own disk, and sizes of zero or less ---------------------------------------------------------
#
# ``backend.pvc_size`` was free text that no door checked, and a template CPU or memory of zero or less was accepted (the backend
# made of it what it did: a pod spec the API server refuses late, as a generic 500 after the rollback). The model now refuses them
# on every write; a row stored before still loads (the storage reads with STORAGE_LOAD_CONTEXT: read path tolerant, write path
# strict), so the create checks the size the workspace will really get, the same way it checks the caps.


def _stored(**template) -> WorkspaceTemplate:
    """A template as the storage reads one back: the rules added by the G5 audit let a stored value through."""
    return WorkspaceTemplate.model_validate(
        {"id": "old", "provider_id": "k3s", "description": "stored before the G5 audit", **template}, context=STORAGE_LOAD_CONTEXT,
    )


def _errors(exc: pydantic.ValidationError) -> list[tuple[tuple, str]]:
    return [(tuple(e["loc"]), e["msg"]) for e in exc.errors()]


@pytest.mark.parametrize("pvc_size", ["10 Gi", "lots", "", "10gi", "1e", " 10Gi", "\u0661Gi"])
def test_a_pvc_size_that_is_not_a_kubernetes_quantity_is_refused_by_the_model(pvc_size):
    with pytest.raises(pydantic.ValidationError) as exc:
        KubernetesTemplateConfig(image="rt:1", pvc_size=pvc_size)
    assert _errors(exc.value) == [(("pvc_size",), f"Value error, pvc_size {pvc_size!r} is not a Kubernetes quantity: write a number "
                                   "and an optional unit with no space, for example 10Gi, 500Mi or 1Ti")]


@pytest.mark.parametrize(("pvc_size", "why"), [
    ("-5Gi", "not more than zero"),
    ("0", "not more than zero"),
    ("1000Pi", "more than the 64 TiB a workspace may have"),
    ("64.5Ti", "more than the 64 TiB a workspace may have"),
    ("1E", "more than the 64 TiB a workspace may have"),          # E alone is the decimal exa unit, not an exponent
])
def test_a_pvc_size_that_is_not_a_disk_a_workspace_may_have_is_refused_by_the_model(pvc_size, why):
    with pytest.raises(pydantic.ValidationError) as exc:
        KubernetesTemplateConfig(image="rt:1", pvc_size=pvc_size)
    assert _errors(exc.value) == [(("pvc_size",), f"Value error, pvc_size {pvc_size!r} is {why}")]


@pytest.mark.parametrize("pvc_size", ["10Gi", "500Mi", "1Ti", "64Ti", "1.5Gi", ".5Gi", "1e9", "2E3", "100G", "+1Gi", "1048576"])
def test_a_kubernetes_quantity_of_at_most_64_tib_is_a_disk(pvc_size):
    assert KubernetesTemplateConfig(image="rt:1", pvc_size=pvc_size).pvc_size == pvc_size


def test_the_pvc_size_refusal_names_the_field_through_the_template():
    with pytest.raises(pydantic.ValidationError) as exc:
        WorkspaceTemplate.model_validate({
            "id": "t", "provider_id": "k3s", "description": "d", "backend": {"kind": "kubernetes", "image": "rt:1", "pvc_size": "lots"},
        })
    assert [tuple(e["loc"]) for e in exc.value.errors()] == [("backend", "kubernetes", "pvc_size")]


@pytest.mark.parametrize("model", [ResourceLimits, ContainerTemplateConfig])
@pytest.mark.parametrize(("field", "value"), [("cpu_cores", 0), ("cpu_cores", -1), ("memory_bytes", 0), ("memory_bytes", -2**30)])
def test_a_template_size_of_zero_or_less_is_refused_by_the_model(model, field, value):
    with pytest.raises(pydantic.ValidationError) as exc:
        model(**{field: value})
    assert [(tuple(e["loc"]), e["type"]) for e in exc.value.errors()] == [((field,), "greater_than")]
    assert model(**{field: None}) is not None          # null is still "no limit"


def test_a_stored_row_with_the_old_sizes_still_loads():
    """Read path tolerant: the row was accepted by the write that stored it, and refusing it on read would make it unreadable."""
    old = _stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": "lots"}, resources={"cpu_cores": -1, "memory_bytes": 0})
    assert (old.backend.pvc_size, old.resources.cpu_cores, old.resources.memory_bytes) == ("lots", -1, 0)
    docker = _stored(provider_id="dock", backend={"kind": "container", "cpu_cores": 0, "memory_bytes": -5})
    assert (docker.backend.cpu_cores, docker.backend.memory_bytes) == (0, -5)


def test_only_the_new_rules_are_tolerant_on_a_stored_row():
    """A stored value that fails for any other reason fails as before: the tolerance is the new rules', not a blanket one."""
    with pytest.raises(pydantic.ValidationError):
        _stored(backend={"kind": "kubernetes", "image": "rt:1"}, resources={"cpu_cores": "many"})
    with pytest.raises(pydantic.ValidationError):
        _stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": 10})


@pytest.mark.parametrize(("pvc_size", "why"), [
    ("lots", "not a Kubernetes quantity"),
    ("10 Gi", "not a Kubernetes quantity"),
    ("-5Gi", "not more than zero"),
    ("1000Pi", "more than the 64 TiB a workspace may have"),
])
def test_a_stored_template_disk_no_workspace_may_have_is_refused_at_create(pvc_size, why):
    old = _stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": pvc_size})
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(old, None)
    assert exc.value.fields == ["backend.pvc_size"]
    assert f"template 'old' sets backend.pvc_size to {pvc_size!r} ({why}" in exc.value.message
    assert exc.value.message.endswith("Edit the template.")


def test_a_disk_override_replaces_the_stored_template_disk():
    """The workspace gets the override's disk, so what the template says there is not what runs."""
    old = _stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": "lots"})
    refuse_oversized_template_sizes(old, _o(disk_bytes=10 * 2**30))
    with pytest.raises(TemplateSizeRefused):
        refuse_oversized_template_sizes(old, _o(cpu_cores=2))          # a CPU override leaves the disk the template's


def test_a_stored_template_size_of_zero_or_less_is_refused_at_create():
    k8s = _stored(backend={"kind": "kubernetes", "image": "rt:1"}, resources={"cpu_cores": -1, "memory_bytes": 0})
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(k8s, None)
    assert exc.value.fields == ["resources.cpu_cores", "resources.memory_bytes"]
    assert "resources.cpu_cores to -1 (not more than zero) and resources.memory_bytes to 0 (not more than zero)" in exc.value.message

    docker = _stored(provider_id="dock", backend={"kind": "container", "cpu_cores": 0})
    with pytest.raises(TemplateSizeRefused) as exc:
        refuse_oversized_template_sizes(docker, None)
    assert exc.value.fields == ["backend.cpu_cores"]
    refuse_oversized_template_sizes(docker, _o(cpu_cores=1))           # replaced by the override: not what runs


def test_a_local_or_docker_templates_unused_resources_are_not_capped_but_must_be_positive():
    """Local ignores ``resources`` and Docker reads ``backend.*``, so no cap applies to ``resources`` there; but it is copied into the
    workspace's snapshot, whose model refuses zero or less, so a stored value of zero or less is refused at create (the review of #869,
    F1: a Local template with ``cpu_cores: 0`` used to create a workspace whose snapshot the model refused)."""
    refuse_oversized_template_sizes(_stored(provider_id="loc", resources={"cpu_cores": 1e308}), None)
    for template, field in (
        (_stored(provider_id="loc", resources={"cpu_cores": 0}), "resources.cpu_cores"),
        (_stored(provider_id="loc", resources={"memory_bytes": -1}), "resources.memory_bytes"),
        (_stored(provider_id="dock", backend={"kind": "container"}, resources={"cpu_cores": -2}), "resources.cpu_cores"),
    ):
        with pytest.raises(TemplateSizeRefused) as exc:
            refuse_oversized_template_sizes(template, None)
        assert exc.value.fields == [field] and "(not more than zero)" in exc.value.message
    # A Docker CPU override replaces backend.cpu_cores, not the unused resources.cpu_cores: still refused.
    with pytest.raises(TemplateSizeRefused):
        refuse_oversized_template_sizes(
            _stored(provider_id="dock", backend={"kind": "container"}, resources={"cpu_cores": 0}), _o(cpu_cores=1),
        )


@pytest.mark.parametrize(("pvc_size", "refused"), [("1Ei", True), ("2Ei", True), ("0.00001Ei", False), ("1E", True), ("1E3", False)])
def test_ei_is_a_unit_and_never_an_exponent(pvc_size, refused):
    """``Ei`` (exbibytes) used to be read as an exponent and raised ValueError: a 500 at create from a stored ``2Ei`` template (the
    review of #869, F2)."""
    if refused:
        with pytest.raises(pydantic.ValidationError, match="more than the 64 TiB"):
            KubernetesTemplateConfig(image="rt:1", pvc_size=pvc_size)
        with pytest.raises(TemplateSizeRefused, match="more than the 64 TiB"):
            refuse_oversized_template_sizes(_stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": pvc_size}), None)
    else:
        assert KubernetesTemplateConfig(image="rt:1", pvc_size=pvc_size).pvc_size == pvc_size
        refuse_oversized_template_sizes(_stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": pvc_size}), None)


def test_the_disk_arithmetic_is_exact_at_the_cap():
    """A value a hair above 64 TiB is not rounded down onto the cap (Decimal's default 28 digits did)."""
    assert disk_quantity_problem("64Ti") is None and disk_quantity_problem("70368744177664") is None
    assert "more than" in disk_quantity_problem("70368744177664.00000000000000000000001")
    assert parse_k8s_quantity("0.00001Ei") == Decimal("11529215046068.46976")


async def test_the_storage_reads_a_stored_row_back_tolerantly_and_patch_if_still_applies(tmp_path):
    """The tolerance on a real SQLite storage: get and list read a template stored before the rules, a workspace whose snapshot
    holds one loads, and a field-scoped patch_if of that workspace (a status write, a backfill) still applies."""
    from datetime import UTC, datetime

    from primer.model.provider import SqliteConfig
    from primer.model.storage import OffsetPage
    from pydantic import SecretStr

    from primer.model.workspace import Workspace, WorkspaceRuntimeMeta
    from primer.storage.sqlite import SqliteStorageProvider

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "t.sqlite"))
    await sp.initialize()
    try:
        old = _stored(backend={"kind": "kubernetes", "image": "rt:1", "pvc_size": "lots"}, resources={"cpu_cores": -1})
        templates = sp.get_storage(WorkspaceTemplate)
        await templates.create(old)
        assert (await templates.get("old")).backend.pvc_size == "lots"
        assert [t.id for t in (await templates.list(OffsetPage(offset=0, length=10))).items] == ["old"]

        workspaces = sp.get_storage(Workspace)
        await workspaces.create(Workspace(
            id="w1", template_id="old", template_snapshot=old, provider_id="k3s", created_at=datetime.now(UTC),
            runtime_meta=WorkspaceRuntimeMeta(url="ws://runtime.test:5959/", token=SecretStr("token")),
        ))
        assert (await workspaces.get("w1")).template_snapshot.resources.cpu_cores == -1
        patched = await workspaces.patch_if("w1", {"name": "renamed"}, where={"name": [None]})
        assert patched is not None and patched.name == "renamed"
    finally:
        await sp.aclose()


def test_both_storage_backends_decode_a_row_as_a_stored_row():
    """The Postgres backend cannot run here; its decode is called directly (it reads only ``row["id"]`` and ``row["data"]``)."""
    from types import SimpleNamespace

    from primer.storage.postgres import PostgresStorage
    from primer.storage.sqlite import SqliteStorage

    document = {"provider_id": "k3s", "description": "d", "backend": {"kind": "kubernetes", "image": "rt:1", "pvc_size": "lots"}}
    pg = PostgresStorage(SimpleNamespace(schema="s"), WorkspaceTemplate)
    assert pg._from_row({"id": "old", "data": dict(document)}).backend.pvc_size == "lots"   # noqa: SLF001
    lite = SqliteStorage.__new__(SqliteStorage)
    lite._model = WorkspaceTemplate                                                           # noqa: SLF001
    assert lite._from_row("old", json.dumps(document)).backend.pvc_size == "lots"            # noqa: SLF001


async def test_each_stored_row_is_read_with_a_fresh_read_only_context(tmp_path):
    """Every validator of a row sees the context object: a shared, writable dict would let one row's validator change what the next row
    reads (the review of #869, F10). The backends pass a fresh read-only mapping per row."""
    from collections.abc import Mapping

    from pydantic import ValidationInfo, field_validator

    from primer.model.common import Identifiable, stored_row_context
    from primer.model.provider import SqliteConfig
    from primer.storage.sqlite import SqliteStorageProvider

    seen: list = []

    class G5ContextProbe(Identifiable):
        value: int = 0

        @field_validator("value")
        @classmethod
        def _record(cls, value: int, info: ValidationInfo) -> int:
            seen.append(info.context)
            return value

    with pytest.raises(TypeError):
        STORAGE_LOAD_CONTEXT["stored_row"] = False  # type: ignore[index]
    assert stored_row_context() is not stored_row_context()

    sp = SqliteStorageProvider(SqliteConfig(path=tmp_path / "ctx.sqlite"))
    await sp.initialize()
    try:
        rows = sp.get_storage(G5ContextProbe)
        await rows.create(G5ContextProbe(id="a", value=1))
        await rows.create(G5ContextProbe(id="b", value=2))
        seen.clear()
        await rows.get("a")
        await rows.get("b")
    finally:
        await sp.aclose()

    assert len(seen) == 2 and seen[0] is not seen[1]
    for context in seen:
        assert isinstance(context, Mapping) and dict(context) == {"stored_row": True}
        with pytest.raises(TypeError):
            context["stored_row"] = False

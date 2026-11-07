"""Service docs distinguish implemented REST surfaces from reserved phases."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_service_docs_name_rest_publish_as_the_current_surface() -> None:
    developer = _read("docs/dev/subsystems/services.md")
    wiki = _read("docs/wiki/not-agent-callable/index.md")

    assert "Publishing is available today through" in developer
    assert "`POST /v1/services/{id}/versions`" in developer
    assert "`Primer.tool` is planned" in developer
    assert "`publish_service`" in developer
    assert "agent/system tool is not implemented" in developer
    assert "There is no agent tool." in wiki


def test_unbuilt_service_surfaces_are_not_described_as_active() -> None:
    paths = (
        "docs/dev/README.md",
        "docs/dev/subsystems/services.md",
        "primer/api/routers/services.py",
        "primer/model/service.py",
        "primer/service/client_js.py",
        "primer/service/publish.py",
        "ui/components/services.jsx",
    )
    combined = "\n".join(_read(path) for path in paths)

    assert "agent-published web apps" not in combined
    assert "agents use the phase-4" not in combined
    assert "Primer.tool arrives" not in combined
    assert "publish_service tool reuses it" not in combined
    assert "Identifier of the granted Toolset" not in combined
    assert "None grants every tool" not in combined
    assert "a stale name 404s at" not in combined
    assert not (ROOT / "primer/toolset/publish.py").exists()

    model = _read("primer/model/service.py")
    assert "stored for planned phase-3 dispatch" in model
    assert "future whole-toolset form" in model

    router = _read("primer/api/routers/services.py")
    assert "planned phase-3 dispatch" in router
    assert "will return 404 for a stale name at call time" in router

    client = _read("primer/service/client_js.py")
    assert "async fn(name, args)" in client
    assert "async tool(" not in client

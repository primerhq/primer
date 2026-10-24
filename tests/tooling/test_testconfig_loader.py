from pathlib import Path
import re
import textwrap

import yaml

from tests._support.testconfig import load_config, Caps


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "testconfig.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_env_interpolation_and_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PG", "postgresql://u:p@h:5432/d")
    cfg = load_config(_write(tmp_path, """
        lanes: {hermetic: true, distributed: true}
        server: {storage: {backend: postgres, postgres_dsn: "${MY_PG}"}}
        llm: {mode: scripted}
    """))
    assert cfg["server"]["storage"]["postgres_dsn"].endswith("/d")
    caps = Caps(cfg)
    assert caps.has("postgres")
    assert caps.has("llm:scripted")
    assert caps.has("distributed")
    assert not caps.has("channels:slack")


def test_missing_file_defaults_to_hermetic_only(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    caps = Caps(cfg)
    assert caps.has("llm:scripted")  # scripted is always available
    assert not caps.has("postgres")
    assert not caps.has("llm:real")


def test_real_llm_capability_requires_base_url(tmp_path):
    cfg = load_config(_write(tmp_path, """
        llm: {mode: real, real: {base_url: "http://x/v1"}}
    """))
    assert Caps(cfg).has("llm:real")


def test_missing_helper_lists_unmet(tmp_path):
    caps = Caps(load_config(tmp_path / "nope.yaml"))
    assert caps.missing(("postgres", "llm:scripted")) == ["postgres"]


def test_e2e_workflow_enables_docker_workspaces_but_not_kubernetes():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "e2e.yml").read_text(encoding="utf-8")
    config_step = re.search(
        r"cat > tests/testconfig\.yaml <<'YAML'\n(?P<body>.*?)\n\s+YAML",
        workflow,
        re.DOTALL,
    )
    assert config_step, "the e2e job must write the capability reader's testconfig"
    config = yaml.safe_load(textwrap.dedent(config_step.group("body")))
    caps = Caps(config)
    assert caps.has("workspace:container")
    assert not caps.has("workspace:kubernetes")

    size_test = (root / "tests" / "e2e" / "test_workspace_size_overrides.py").read_text(encoding="utf-8")
    assert "workspace:container is enabled" in size_test
    assert "docker is not reachable from the test runner" not in size_test

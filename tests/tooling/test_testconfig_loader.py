from pathlib import Path
import textwrap

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

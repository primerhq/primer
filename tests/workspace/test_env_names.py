"""Which environment variable names a workspace create accepts (lead ruling 17 of plan 08, from the review of #782).

A name that is not a POSIX variable name (``1BAD``, ``a b``, ``A=B``) was accepted and came back as a generic 500 after the
create had been rolled back, when the cluster refused the manifest. The two names the platform sets itself,
``PRIMER_RUNTIME_TOKEN`` and ``RUNTIME_TOKEN``, were dropped from the Kubernetes pod with a line in the log, so a person who set
one saw nothing. Both are a 422 now that names the variable, raised before any backend runs.
"""

from __future__ import annotations

import re

import pytest
from pydantic import SecretStr

from primer.model.except_ import ValidationError
from primer.model.workspace import WorkspaceTemplate, WorkspaceTemplateOverrides
from primer.workspace import env_names
from primer.workspace.env_names import (
    ENV_NAME_PATTERN,
    RUNTIME_TOKEN_ENV_NAMES,
    EnvNameRefused,
    refuse_bad_env_names,
)

_INVALID = "An environment variable name may use letters, digits and underscores and must not start with a digit. Rename or remove it."
_RESERVED = "This name is reserved for the workspace runtime's token and cannot be set. Remove it."


def _t(env: dict[str, str] | None = None, template_id: str = "tpl") -> WorkspaceTemplate:
    return WorkspaceTemplate(id=template_id, provider_id="p", description="d", env={k: SecretStr(v) for k, v in (env or {}).items()})


def _o(env: dict[str, str]) -> WorkspaceTemplateOverrides:
    return WorkspaceTemplateOverrides(env={k: SecretStr(v) for k, v in env.items()})


def test_the_pattern_is_the_rulings():
    assert ENV_NAME_PATTERN == r"^[A-Za-z_][A-Za-z0-9_]*$"
    assert RUNTIME_TOKEN_ENV_NAMES == frozenset({"PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"})


@pytest.mark.parametrize("name", ["A", "a", "_", "_A1", "PATH", "my_var_2", "A" * 255])
def test_a_posix_name_is_accepted(name):
    assert re.fullmatch(ENV_NAME_PATTERN, name)
    refuse_bad_env_names(_t({name: "v"}), _o({name: "v"}))


@pytest.mark.parametrize("name", ["", "1BAD", "a b", "a-b", "a.b", "A=B", "a\nb", "a\x00b", " A", "A ", "café", "A\n"])
def test_a_name_that_is_not_posix_is_refused_in_the_overrides(name):
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({name: "v"}))
    assert isinstance(exc.value, ValidationError)
    assert exc.value.problem_extensions == {"errors": [{
        "loc": ["body", "overrides", "env", name], "msg": _INVALID, "type": "invalid_env_name",
    }]}


def test_the_message_names_the_variable_and_where_it_was_set():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({"1BAD": "v"}))
    assert exc.value.message == f"overrides.env.1BAD: {_INVALID}"


@pytest.mark.parametrize("name", sorted(RUNTIME_TOKEN_ENV_NAMES))
def test_a_runtime_token_name_in_the_overrides_is_a_422_that_names_it(name):
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({name: "stolen"}))
    assert exc.value.problem_extensions["errors"] == [{
        "loc": ["body", "overrides", "env", name], "msg": _RESERVED, "type": "reserved_env_name",
    }]
    assert exc.value.message == f"overrides.env.{name}: {_RESERVED}"
    assert "stolen" not in exc.value.message and "stolen" not in repr(exc.value.problem_extensions)    # never the value


def test_every_bad_name_in_the_overrides_is_listed_in_order():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({"OK": "v", "1BAD": "v", "RUNTIME_TOKEN": "v", "a b": "v"}))
    assert [(e["loc"][-1], e["type"]) for e in exc.value.problem_extensions["errors"]] == [
        ("1BAD", "invalid_env_name"), ("RUNTIME_TOKEN", "reserved_env_name"), ("a b", "invalid_env_name"),
    ]


def test_a_template_env_is_checked_too_and_names_the_template():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({"a b": "v"}, template_id="big"), None)
    assert exc.value.message == f"template 'big' sets env.a b: {_INVALID}"
    assert exc.value.problem_extensions == {}      # the name is stored in the template, not sent in this request: no `loc`

    with pytest.raises(EnvNameRefused) as token:
        refuse_bad_env_names(_t({"PRIMER_RUNTIME_TOKEN": "v"}, template_id="big"), _o({"FINE": "v"}))
    assert token.value.message == f"template 'big' sets env.PRIMER_RUNTIME_TOKEN: {_RESERVED}"


def test_the_request_s_own_names_are_named_first_because_the_person_can_fix_them_now():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({"a b": "v"}), _o({"1BAD": "v"}))
    assert exc.value.message.startswith("overrides.env.1BAD:")
    assert "template 'tpl' sets env.a b" in exc.value.message
    assert [e["loc"][-1] for e in exc.value.problem_extensions["errors"]] == ["1BAD"]


def test_no_overrides_and_no_env_is_never_refused():
    refuse_bad_env_names(_t(), None)
    refuse_bad_env_names(_t(), WorkspaceTemplateOverrides())


def test_a_very_long_name_is_cut_in_the_message_but_not_in_the_loc():
    name = "x y" * 100
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({name: "v"}))
    assert name not in exc.value.message and "..." in exc.value.message
    assert exc.value.problem_extensions["errors"][0]["loc"][-1] == name


def test_a_flood_of_bad_names_lists_the_first_few_and_counts_the_rest():
    bad = {f"bad {i}": "v" for i in range(env_names.MAX_LISTED + 5)}
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o(bad))
    assert len(exc.value.problem_extensions["errors"]) == env_names.MAX_LISTED
    assert "and 5 more" in exc.value.message


def test_the_kubernetes_backend_uses_the_same_token_names():
    """The manifest builder keeps its own drop (defence in depth, for a caller that skips the registry); one set of names."""
    from primer.workspace.k8s.backend import _RUNTIME_TOKEN_ENV_NAMES

    assert _RUNTIME_TOKEN_ENV_NAMES is RUNTIME_TOKEN_ENV_NAMES

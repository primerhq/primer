"""Which environment variable names a workspace create accepts (lead ruling 17 of plan 08, FINAL from the review of #789).

A name is accepted when Kubernetes accepts it, which is ``IsEnvVarName`` in ``k8s.io/apimachinery/pkg/util/validation`` exactly: it
matches ``^[-._a-zA-Z][-._a-zA-Z0-9]*$`` and it is not ``.``, not ``..`` and does not start with ``..`` (``hasChDirPrefix``). On top
of that it is at most 256 characters. That is wider than a POSIX name on purpose: ``my-var`` and ``a.b`` are valid on Kubernetes
(and on Docker and the local backend), and a stored template that has one must keep creating workspaces, the boot's default
workspace included. The first version of the rule was POSIX-only and turned such a template into a 422 on every create; those
tests are flipped here.

``1BAD``, ``a b``, ``A=B``, an empty name, ``café`` and a name over 256 characters were accepted and came back as a generic 500 after
the create had been rolled back, when the cluster refused the manifest. The two names the platform sets itself,
``PRIMER_RUNTIME_TOKEN`` and ``RUNTIME_TOKEN``, were dropped from the Kubernetes pod with a line in the log, so a person who set
one saw nothing. All of them are a 422 now that names the variable, raised before any backend runs.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import SecretStr

from primer.model.except_ import ValidationError
from primer.model.workspace import WorkspaceTemplate, WorkspaceTemplateOverrides
from primer.workspace import env_names
from primer.workspace.env_names import (
    ENV_NAME_PATTERN,
    MAX_ENV_NAME_LENGTH,
    RUNTIME_TOKEN_ENV_NAMES,
    EnvNameRefused,
    refuse_bad_env_names,
)

_INVALID = "An environment variable name is 1 to 256 characters of ASCII letters, digits, '_', '-' and '.'. It does not start with a digit or with '..', and it is not '.'. Rename or remove it."
_RESERVED = "This name is reserved for the workspace runtime's token and cannot be set. Remove it."

#: Names Kubernetes accepts: the plain ones, the ones the first (POSIX-only) version of the rule refused, a dot anywhere but at the
#: ``.`` / ``..`` / ``..x`` start, and the longest allowed.
_ACCEPTED = [
    "A", "a", "_", "_x", "_A1", "PATH", "my_var_2", "my-var", "a.b", "x-y.z", "MY_VAR", "a1", ".x", "x..", "a..b", "x.", ".a.b",
    "a" * 256, "a-" * 128,
]

#: Names Kubernetes refuses (or that are over the length limit): a digit first, a character outside the set, nothing at all, ``.``,
#: ``..`` and a name that starts with ``..``, a name too long, and a name of the right length that is not ASCII.
_REFUSED = [
    "", "1BAD", "9", "1-a", "a b", "A=B", "a\nb", "a\x00b", " A", "A ", "café", "A\n", "a/b", "a:b", "a$b", "日本語",
    ".", "..", "..x", "...", "..-", "..a.b", "a" * 257, "é" * 256,
]


def _t(env: dict[str, str] | None = None, template_id: str = "tpl") -> WorkspaceTemplate:
    return WorkspaceTemplate(id=template_id, provider_id="p", description="d", env={k: SecretStr(v) for k, v in (env or {}).items()})


def _o(env: dict[str, str]) -> WorkspaceTemplateOverrides:
    return WorkspaceTemplateOverrides(env={k: SecretStr(v) for k, v in env.items()})


def test_the_pattern_is_the_rulings():
    assert ENV_NAME_PATTERN == r"^[-._a-zA-Z][-._a-zA-Z0-9]*$"
    assert MAX_ENV_NAME_LENGTH == 256
    assert RUNTIME_TOKEN_ENV_NAMES == frozenset({"PRIMER_RUNTIME_TOKEN", "RUNTIME_TOKEN"})


@pytest.mark.parametrize("name", _ACCEPTED)
def test_a_kubernetes_name_is_accepted_in_the_overrides(name):
    assert re.fullmatch(ENV_NAME_PATTERN, name) and len(name) <= MAX_ENV_NAME_LENGTH
    refuse_bad_env_names(_t(), _o({name: "v"}))


@pytest.mark.parametrize("name", _ACCEPTED)
def test_a_kubernetes_name_is_accepted_in_a_stored_template(name):
    """The case the first version broke: a template that stores ``my-var`` or ``a.b`` got a 422 on every create."""
    refuse_bad_env_names(_t({name: "v"}), None)
    refuse_bad_env_names(_t({name: "v"}), _o({name: "other"}))


@pytest.mark.parametrize("name", _REFUSED)
def test_a_name_kubernetes_would_refuse_is_refused_in_the_overrides(name):
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({name: "v"}))
    assert isinstance(exc.value, ValidationError)
    assert exc.value.problem_extensions == {"errors": [{
        "loc": ["body", "overrides", "env", name], "msg": _INVALID, "type": "invalid_env_name",
    }]}


@pytest.mark.parametrize("name", _REFUSED)
def test_a_name_kubernetes_would_refuse_is_refused_in_a_stored_template(name):
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({name: "v"}, template_id="big"), None)
    assert exc.value.message.startswith("template 'big' sets env.") and exc.value.message.endswith(f": {_INVALID}")
    assert exc.value.template_items == [(name, "invalid_env_name")]
    assert exc.value.problem_extensions == {}      # the name is stored in the template, not sent in this request: no `loc`


@pytest.mark.parametrize("name", [".", "..", "..x", "...", "..-", "..a.b"])
def test_the_chdir_names_match_the_pattern_and_are_refused_all_the_same(name):
    """``IsEnvVarName`` is the pattern plus ``hasChDirPrefix``: ``.``, ``..`` and a name that starts with ``..`` match the pattern, so
    only the second check refuses them."""
    assert re.fullmatch(ENV_NAME_PATTERN, name)
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({name: "v"}))
    assert exc.value.problem_extensions["errors"] == [{
        "loc": ["body", "overrides", "env", name], "msg": _INVALID, "type": "invalid_env_name",
    }]
    with pytest.raises(EnvNameRefused) as stored:
        refuse_bad_env_names(_t({name: "v"}, template_id="big"), None)
    assert stored.value.message == f"template 'big' sets env.'{name}': {_INVALID}"


@pytest.mark.parametrize("name", [".x", "x..", "a.b", "a..b", "x.", ".a.b", ".-", "-.."])
def test_a_dot_is_fine_anywhere_but_at_the_chdir_prefix(name):
    assert re.fullmatch(ENV_NAME_PATTERN, name)
    refuse_bad_env_names(_t({name: "v"}), _o({name: "v"}))


def test_the_length_limit_is_256_characters_not_255_and_not_257():
    refuse_bad_env_names(_t({"a" * 256: "v"}), _o({"b" * 256: "v"}))
    for too_long in ("a" * 257, "a" * 1000):
        with pytest.raises(EnvNameRefused) as exc:
            refuse_bad_env_names(_t(), _o({too_long: "v"}))
        assert [(e["loc"][-1], e["type"]) for e in exc.value.problem_extensions["errors"]] == [(too_long, "invalid_env_name")]


def test_the_message_names_the_variable_and_where_it_was_set():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({"1BAD": "v"}))
    assert exc.value.message == f"overrides.env.'1BAD': {_INVALID}"


@pytest.mark.parametrize("name", sorted(RUNTIME_TOKEN_ENV_NAMES))
def test_a_runtime_token_name_in_the_overrides_is_a_422_that_names_it(name):
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({name: "stolen"}))
    assert exc.value.problem_extensions["errors"] == [{
        "loc": ["body", "overrides", "env", name], "msg": _RESERVED, "type": "reserved_env_name",
    }]
    assert exc.value.message == f"overrides.env.'{name}': {_RESERVED}"
    assert "stolen" not in exc.value.message and "stolen" not in repr(exc.value.problem_extensions)    # never the value


@pytest.mark.parametrize("name", sorted(RUNTIME_TOKEN_ENV_NAMES))
def test_a_runtime_token_name_in_a_stored_template_is_a_422_that_names_the_template(name):
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({name: "stolen"}, template_id="big"), _o({"FINE": "v"}))
    assert exc.value.message == f"template 'big' sets env.'{name}': {_RESERVED}"
    assert exc.value.template_items == [(name, "reserved_env_name")]
    assert exc.value.problem_extensions == {} and "stolen" not in exc.value.message


def test_every_bad_name_in_the_overrides_is_listed_in_order():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t(), _o({"OK": "v", "my-var": "v", "1BAD": "v", "RUNTIME_TOKEN": "v", "a b": "v"}))
    assert [(e["loc"][-1], e["type"]) for e in exc.value.problem_extensions["errors"]] == [
        ("1BAD", "invalid_env_name"), ("RUNTIME_TOKEN", "reserved_env_name"), ("a b", "invalid_env_name"),
    ]


def test_a_template_env_is_checked_too_and_names_the_template():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({"a b": "v", "my-var": "v"}, template_id="big"), None)
    assert exc.value.message == f"template 'big' sets env.'a b': {_INVALID}"
    assert exc.value.problem_extensions == {}      # the name is stored in the template, not sent in this request: no `loc`


def test_the_request_s_own_names_are_named_first_because_the_person_can_fix_them_now():
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({"a b": "v"}), _o({"1BAD": "v"}))
    assert exc.value.message.startswith("overrides.env.'1BAD':")
    assert "template 'tpl' sets env.'a b'" in exc.value.message
    assert [e["loc"][-1] for e in exc.value.problem_extensions["errors"]] == ["1BAD"]


def test_no_overrides_and_no_env_is_never_refused():
    refuse_bad_env_names(_t(), None)
    refuse_bad_env_names(_t(), WorkspaceTemplateOverrides())


def test_a_very_long_name_is_cut_in_the_message_but_not_in_the_loc():
    for name in ("a" * 257, "x y" * 100):
        with pytest.raises(EnvNameRefused) as exc:
            refuse_bad_env_names(_t(), _o({name: "v"}))
        assert name not in exc.value.message and "..." in exc.value.message and len(exc.value.message) < 300
        assert exc.value.problem_extensions["errors"][0]["loc"][-1] == name


@pytest.mark.parametrize("name", ["a\nb", "a\rb", "a\x1b[31mb", "a b", "a\x00b", "café", "日本語", "\ud800", "é" * 40])
def test_a_refused_name_is_shown_escaped_so_it_cannot_split_a_log_line(name):
    """The message is logged and echoes the caller's own input: ``ascii()`` leaves no newline, control or non-ASCII character in it."""
    with pytest.raises(EnvNameRefused) as exc:
        refuse_bad_env_names(_t({name: "v"}), _o({name: "v"}))
    message = exc.value.message
    assert message.isascii() and message.isprintable(), message
    assert message.count("overrides.env.") == 1 and message.count("sets env.") == 1
    # The loc keeps the name as it was sent, a lone surrogate as its JSON escape (it has no UTF-8 encoding, ticket 01a1506c)
    assert exc.value.problem_extensions["errors"][0]["loc"][-1] == re.sub(
        r"[\ud800-\udfff]", lambda m: f"\\u{ord(m.group(0)):04x}", name
    )


def test_a_lone_surrogate_name_escapes_in_the_loc_so_the_422_encodes():
    """A lone surrogate has no UTF-8 encoding and the problem body is written UTF-8 (the JSON does not escape it): the raw
    ``\\ud800`` in the ``loc`` made the 422 itself a 500 (ticket 01a1506c). The ``loc`` shows the name as its JSON escape, and
    the field path keeps its shape."""
    for name in ("\ud800", "a\ud800b", "\ud800" * 3):
        with pytest.raises(EnvNameRefused) as exc:
            refuse_bad_env_names(_t(), _o({name: "v"}))
        error = exc.value.problem_extensions["errors"][0]
        assert error["loc"][:3] == ["body", "overrides", "env"]
        assert error["loc"][-1] == name.encode("ascii", "backslashreplace").decode("ascii")
        assert error["type"] == "invalid_env_name"
        # The whole envelope renders to UTF-8 the way the response does
        json.dumps({"detail": exc.value.message, **exc.value.problem_extensions}, ensure_ascii=False).encode("utf-8")


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

"""The Hugging Face Hub model check (``primer/common/hf_hub.py``).

Registering a Hugging Face embedding or cross-encoder provider names models that are pulled from the Hub the first time they are used. ``check_hf_models`` asks the Hub the
question ``huggingface_hub``'s ``HfApi.auth_check`` asks (``GET {endpoint}/api/models/{name}/auth-check``) and maps the answer the way the library's
``hf_raise_for_status`` does. Every test mocks the Hub with ``respx``: nothing here reaches the real Hugging Face Hub.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from primer.common.hf_hub import (
    HF_ENDPOINT_DEFAULT,
    HF_REPO_ID_RE,
    HfModelCheck,
    check_hf_models,
    hf_endpoint,
)

HUB = "https://hub.test"
MODEL = "BAAI/bge-small-en-v1.5"
TOKEN = "hf_secret_1234"
UNSENDABLE_TOKEN = "The token has a space, a line break or another character that cannot be sent; paste it again"


def _url(name: str = MODEL, hub: str = HUB) -> str:
    return f"{hub}/api/models/{name}/auth-check"


async def _check_one(name: str = MODEL, *, token: str | None = None) -> HfModelCheck:
    [check] = await check_hf_models([name], token=token, endpoint=HUB)
    return check


# ---- what each answer of the Hub means -------------------------------------------------------------------------------------------------------------------------


@respx.mock
async def test_a_public_model_is_ok() -> None:
    route = respx.get(_url()).mock(return_value=httpx.Response(200, json={}))

    check = await _check_one()

    assert check == HfModelCheck(name=MODEL, ok=True, status="ok", detail="Can be pulled")
    assert route.call_count == 1


@respx.mock
async def test_the_token_is_sent_as_a_bearer_header() -> None:
    route = respx.get(_url()).mock(return_value=httpx.Response(200))

    check = await _check_one(token=TOKEN)

    assert check.ok is True
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("token", [None, ""])
@respx.mock
async def test_no_token_sends_no_authorization_header(token: str | None) -> None:
    route = respx.get(_url()).mock(return_value=httpx.Response(200))

    await _check_one(token=token)

    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_the_request_goes_to_the_models_auth_check_path_of_the_endpoint() -> None:
    respx.get(_url()).mock(return_value=httpx.Response(200))

    await _check_one()

    request = respx.calls.last.request
    assert request.method == "GET" and str(request.url) == _url()


@respx.mock
async def test_a_gated_model_without_access_is_gated() -> None:
    respx.get(_url()).mock(return_value=httpx.Response(403, headers={"X-Error-Code": "GatedRepo"}))

    check = await _check_one(token=TOKEN)

    assert (check.ok, check.status) == (False, "gated")
    assert check.detail == "Gated: accept the model's terms on the Hub with the token's account"


@respx.mock
async def test_a_gated_answer_wins_over_the_status_it_arrives_with() -> None:
    """The header, not the status, says which refusal it is: a gated repository can arrive as a 401 as well as a 403."""
    respx.get(_url()).mock(return_value=httpx.Response(401, headers={"X-Error-Code": "GatedRepo"}))

    assert (await _check_one()).status == "gated"


@respx.mock
async def test_a_refused_token_is_bad_token() -> None:
    respx.get(_url()).mock(
        return_value=httpx.Response(401, headers={"X-Error-Message": "Invalid credentials in Authorization header"}),
    )

    check = await _check_one(token=TOKEN)

    assert (check.ok, check.status, check.detail) == (False, "bad_token", "The Hub refused the token")


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (401, {"X-Error-Code": "RepoNotFound"}),
        (404, {"X-Error-Code": "RepoNotFound"}),
        (404, {}),
        (401, {}),
    ],
    ids=["401 RepoNotFound", "404 RepoNotFound", "plain 404", "plain 401"],
)
@respx.mock
async def test_a_missing_or_private_model_is_not_found(status: int, headers: dict[str, str]) -> None:
    respx.get(_url()).mock(return_value=httpx.Response(status, headers=headers))

    check = await _check_one()

    assert (check.ok, check.status) == (False, "not_found")
    assert check.detail == "Not found, or private and the token cannot see it"


@respx.mock
async def test_a_disabled_repo_is_disabled() -> None:
    respx.get(_url()).mock(return_value=httpx.Response(403, headers={"X-Error-Message": "Access to this resource is disabled."}))

    check = await _check_one(token=TOKEN)

    assert (check.ok, check.status, check.detail) == (False, "disabled", "Disabled on the Hub")


@pytest.mark.parametrize("status", [403, 429, 500, 503])
@respx.mock
async def test_any_other_status_is_an_error_that_names_it(status: int) -> None:
    respx.get(_url()).mock(return_value=httpx.Response(status))

    check = await _check_one()

    assert (check.ok, check.status, check.detail) == (False, "error", f"The Hub answered HTTP {status}")


@respx.mock
async def test_an_unreachable_hub_is_an_error_not_an_exception() -> None:
    respx.get(_url()).mock(side_effect=httpx.ConnectError("connection refused"))

    check = await _check_one()

    assert (check.ok, check.status, check.detail) == (False, "error", "Could not reach hub.test (ConnectError)")


@respx.mock
async def test_a_timeout_is_an_error_not_an_exception() -> None:
    respx.get(_url()).mock(side_effect=httpx.ReadTimeout("slow"))

    check = await _check_one()

    assert (check.status, check.detail) == ("error", "Could not reach hub.test (ReadTimeout)")


@respx.mock
async def test_any_failure_of_the_request_is_a_result_and_never_a_raise() -> None:
    """The helper serves a diagnostic route and a tool: a failure nobody foresaw is still one result per name."""
    respx.get(_url()).mock(side_effect=RuntimeError("something nobody foresaw"))

    check = await _check_one()

    assert (check.ok, check.status) == (False, "error")
    assert "something nobody foresaw" not in check.detail


@respx.mock
async def test_a_renamed_model_is_followed_to_its_new_name() -> None:
    """The Hub redirects a renamed repository, and so do the libraries that later pull it."""
    respx.get(_url()).mock(return_value=httpx.Response(307, headers={"Location": _url("BAAI/bge-small-en-v1.6")}))
    respx.get(_url("BAAI/bge-small-en-v1.6")).mock(return_value=httpx.Response(200))

    assert (await _check_one()).status == "ok"


# ---- names the Hub is never asked about -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../../api/whoami", "a/b/c", "bad--name", "a..b", "", " ", "BAAI/bge small", "BAAI/bge\n", "-leading/x", "x/" + "y" * 97, "?x=1", "BAAI/bge%2Fsmall"],
)
@respx.mock
async def test_a_path_like_name_is_refused_without_a_request(name: str) -> None:
    check = await _check_one(name)

    assert (check.name, check.ok, check.status) == (name, False, "invalid_name")
    assert check.detail == "Not a Hugging Face model id (namespace/name)"
    assert respx.calls.call_count == 0


@pytest.mark.parametrize(
    "name",
    ["BAAI/bge-small-en-v1.5", "bert-base-uncased", "cross-encoder/ms-marco-MiniLM-L-6-v2", "Qwen/Qwen3-Embedding-0.6B", "a/b", "x_y/z.w", "A1", "y" * 96],
)
def test_the_names_the_hub_serves_match_the_repo_id_rule(name: str) -> None:
    assert HF_REPO_ID_RE.match(name)


@respx.mock
async def test_a_bad_name_does_not_stop_the_names_after_it() -> None:
    route = respx.get(_url()).mock(return_value=httpx.Response(200))

    results = await check_hf_models(["../x", MODEL], token=None, endpoint=HUB)

    assert [(r.name, r.status) for r in results] == [("../x", "invalid_name"), (MODEL, "ok")]
    assert route.call_count == 1


@respx.mock
async def test_no_names_make_no_request_and_give_no_results() -> None:
    assert await check_hf_models([], token=TOKEN, endpoint=HUB) == []
    assert respx.calls.call_count == 0


# ---- order, and what a result may say ---------------------------------------------------------------------------------------------------------------------------


@respx.mock
async def test_results_keep_the_input_order() -> None:
    names = ["z-org/last-alphabetically", "BAAI/bge-small-en-v1.5", "a-org/missing"]
    respx.get(_url(names[0])).mock(return_value=httpx.Response(200))
    respx.get(_url(names[1])).mock(return_value=httpx.Response(403, headers={"X-Error-Code": "GatedRepo"}))
    respx.get(_url(names[2])).mock(return_value=httpx.Response(404))

    results = await check_hf_models(names, token=None, endpoint=HUB)

    assert [(r.name, r.status) for r in results] == [(names[0], "ok"), (names[1], "gated"), (names[2], "not_found")]
    assert [str(call.request.url) for call in respx.calls] == [_url(n) for n in names], "one request after another, in the order given"


@respx.mock
async def test_the_token_never_appears_in_a_detail() -> None:
    respx.get(_url()).mock(side_effect=httpx.ConnectError(f"Illegal header value b'Bearer {TOKEN}'"))

    check = await _check_one(token=TOKEN)

    assert check.status == "error"
    assert TOKEN not in check.model_dump_json()


@respx.mock
async def test_an_endpoint_with_credentials_names_only_its_host() -> None:
    mirror = "https://svc:s3cr3t-pw@mirror.test:8443"
    respx.route(host="mirror.test").mock(side_effect=httpx.ConnectError("no route"))

    [check] = await check_hf_models([MODEL], token=None, endpoint=mirror)

    assert check.detail == "Could not reach mirror.test (ConnectError)"
    assert "s3cr3t" not in check.model_dump_json()


@pytest.mark.parametrize("token", [f"{TOKEN} ", f" {TOKEN}", f"{TOKEN}\n", f"{TOKEN}\r\nX-Evil: 1", "hf_töken", "   "])
@respx.mock
async def test_a_token_that_cannot_be_sent_is_a_bad_token_before_any_request(token: str) -> None:
    """A key pasted with a space or a line break is not a valid header value; left to the HTTP library it would read as a network failure."""
    check = await _check_one(token=token)

    assert (check.ok, check.status, check.detail) == (False, "bad_token", UNSENDABLE_TOKEN)
    assert respx.calls.call_count == 0


@respx.mock
async def test_a_bad_name_is_reported_as_a_bad_name_whatever_the_token() -> None:
    results = await check_hf_models(["../x", MODEL], token="bad token", endpoint=HUB)

    assert [(r.name, r.status) for r in results] == [("../x", "invalid_name"), (MODEL, "bad_token")]


# ---- the endpoint -----------------------------------------------------------------------------------------------------------------------------------------------


def test_hf_endpoint_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", "https://mirror.example/")

    assert hf_endpoint() == "https://mirror.example"


def test_hf_endpoint_defaults_to_the_public_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    assert hf_endpoint() == HF_ENDPOINT_DEFAULT == "https://huggingface.co"

    monkeypatch.setenv("HF_ENDPOINT", "  ")
    assert hf_endpoint() == HF_ENDPOINT_DEFAULT


@respx.mock
async def test_without_an_endpoint_argument_the_environment_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_ENDPOINT", HUB + "/")
    route = respx.get(_url()).mock(return_value=httpx.Response(200))

    [check] = await check_hf_models([MODEL], token=None)

    assert check.ok is True and route.call_count == 1

"""A ``kind=url`` file source fetches the stored URL with its credential and never writes the password in an error (ticket 01a11d32).

The fetch reads the real URL (aiohttp sends ``user:password@`` as Basic auth), so a masked template still materialises. Every error text it raises used to carry ``url!r`` whole: a 500 from the
host, a redirect without a Location, a timeout, too many redirects and the SSRF refusal all reached the person who created the workspace (a 422 or 500 body, a log line) with the password in it.
"""

from __future__ import annotations

import asyncio

import pytest

from primer.model.workspace import FileMount, _UrlSource
from primer.workspace.files import UrlSourceRefusedError, resolve_file_sources

URL = "https://reader:s3cr3t@files.example.com/seed.txt"
MASKED = "https://reader:**********@files.example.com/seed.txt"


def _mount(url: str = URL) -> FileMount:
    return FileMount(path="x", source=_UrlSource(url=url))


class _Resp:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b"content") -> None:
        self.status, self.headers, self._body = status, headers or {}, body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _Session:
    def __init__(self, responses: list[_Resp] | None = None, hang: bool = False) -> None:
        self.fetched: list[str] = []
        self._responses = list(responses or [])
        self._hang = hang

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def get(self, url, **_):
        self.fetched.append(str(url))
        if self._hang:
            return _Hang()
        return self._responses.pop(0)


class _Hang:
    async def __aenter__(self):
        await asyncio.sleep(30)

    async def __aexit__(self, *a):
        return None


def _patch(monkeypatch, session: _Session) -> _Session:
    monkeypatch.setattr("primer.workspace.files._http_session", lambda: session)
    return session


@pytest.mark.asyncio
async def test_the_fetch_reads_the_stored_url_with_its_password(monkeypatch) -> None:
    session = _patch(monkeypatch, _Session([_Resp(200)]))

    out = await resolve_file_sources([_mount()])

    assert session.fetched == [URL], "the fetch authenticates: the password is not the served mask"
    assert out[0].content == b"content"


@pytest.mark.asyncio
async def test_a_failed_status_does_not_name_the_password(monkeypatch) -> None:
    _patch(monkeypatch, _Session([_Resp(500)]))

    with pytest.raises(RuntimeError) as caught:
        await resolve_file_sources([_mount()])

    assert "s3cr3t" not in str(caught.value) and MASKED in str(caught.value) and "500" in str(caught.value)


@pytest.mark.asyncio
async def test_a_redirect_without_a_location_does_not_name_the_password(monkeypatch) -> None:
    _patch(monkeypatch, _Session([_Resp(302)]))

    with pytest.raises(RuntimeError) as caught:
        await resolve_file_sources([_mount()])

    assert "s3cr3t" not in str(caught.value) and "Location" in str(caught.value)


@pytest.mark.asyncio
async def test_a_redirect_to_a_url_with_its_own_credential_does_not_name_it_either(monkeypatch) -> None:
    session = _patch(monkeypatch, _Session([_Resp(302, {"Location": "https://other:hunter2@cdn.example.net/seed.txt"}), _Resp(404)]))

    with pytest.raises(RuntimeError) as caught:
        await resolve_file_sources([_mount()])

    assert session.fetched[1] == "https://other:hunter2@cdn.example.net/seed.txt", "the hop is fetched as given"
    assert "hunter2" not in str(caught.value) and "s3cr3t" not in str(caught.value)
    assert "https://other:**********@cdn.example.net/seed.txt" in str(caught.value)


@pytest.mark.asyncio
async def test_too_many_redirects_does_not_name_the_password(monkeypatch) -> None:
    _patch(monkeypatch, _Session([_Resp(302, {"Location": "https://files.example.com/next"}) for _ in range(10)]))

    with pytest.raises(RuntimeError) as caught:
        await resolve_file_sources([_mount()])

    assert "s3cr3t" not in str(caught.value) and "redirects" in str(caught.value)


@pytest.mark.asyncio
async def test_a_redirect_loop_through_a_relative_location_keeps_the_password_on_every_hop_and_names_it_nowhere(monkeypatch) -> None:
    """A relative ``Location`` is joined onto the current URL, which keeps its userinfo: the password survives EVERY hop, so the "more than N redirects" text names a URL that still holds it."""
    session = _patch(monkeypatch, _Session([_Resp(302, {"Location": "/next"}) for _ in range(10)]))

    with pytest.raises(RuntimeError) as caught:
        await resolve_file_sources([_mount()])

    assert len(session.fetched) == 6 and all("reader:s3cr3t@files.example.com" in u for u in session.fetched), session.fetched
    assert "s3cr3t" not in str(caught.value) and "redirects" in str(caught.value) and "reader:**********@files.example.com" in str(caught.value)


@pytest.mark.asyncio
async def test_a_timeout_does_not_name_the_password(monkeypatch) -> None:
    _patch(monkeypatch, _Session(hang=True))
    monkeypatch.setattr("primer.workspace.files._FETCH_TIMEOUT_S", 0.05)

    with pytest.raises(RuntimeError) as caught:
        await resolve_file_sources([_mount()])

    assert "s3cr3t" not in str(caught.value) and "timed out" in str(caught.value)


@pytest.mark.asyncio
async def test_a_refused_destination_does_not_name_the_password(monkeypatch) -> None:
    def _no_session():
        raise AssertionError("the fetch opened a connection to a refused destination")

    monkeypatch.setattr("primer.workspace.files._http_session", _no_session)

    with pytest.raises(UrlSourceRefusedError) as caught:
        await resolve_file_sources([_mount("http://reader:s3cr3t@127.0.0.1:1/seed.txt")])

    assert "s3cr3t" not in str(caught.value) and "127.0.0.1" in str(caught.value) and "refused" in str(caught.value)


@pytest.mark.asyncio
async def test_a_hash_mismatch_does_not_name_the_password(monkeypatch) -> None:
    _patch(monkeypatch, _Session([_Resp(200)]))
    mount = FileMount(path="x", source=_UrlSource(url=URL, sha256="0" * 64))

    with pytest.raises(Exception) as caught:
        await resolve_file_sources([mount])

    assert "s3cr3t" not in str(caught.value)

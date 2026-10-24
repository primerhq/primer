"""Capability URLs for the workspace files a canvas shows (pivot P5, Task 14; plan Decision 12): the token, its expiry, what is referenced."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from primer.canvas.files import (
    FILE_TOKEN_SALT,
    FILE_URL_TTL_S,
    MEDIA_TYPES,
    canvas_file_secret,
    media_type_of,
    mint_file_url,
    referenced_files,
    safe_relative_path,
    verify_file_token,
)


def _token(url: str) -> str:
    return url.rsplit("/", 1)[1]


def test_a_token_round_trips_and_expires():
    url = mint_file_url("s3cret", "s1", "assets/c.png")
    assert url.startswith("/canvas/sessions/s1/files/")
    token = _token(url)
    assert verify_file_token("s3cret", token) == ("s1", "assets/c.png")
    assert verify_file_token("other", token) is None and verify_file_token("s3cret", token, max_age=-1) is None


def test_a_token_lives_fifteen_minutes(monkeypatch):
    assert (FILE_TOKEN_SALT, FILE_URL_TTL_S) == ("primer.canvas.file.v1", 900)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - FILE_URL_TTL_S - 5)
    old = _token(mint_file_url("s3cret", "s1", "assets/c.png"))
    monkeypatch.setattr(time, "time", lambda: real() - FILE_URL_TTL_S + 30)
    fresh = _token(mint_file_url("s3cret", "s1", "assets/c.png"))
    monkeypatch.setattr(time, "time", real)
    assert verify_file_token("s3cret", old) is None, "a URL older than 900 s is refused"
    assert verify_file_token("s3cret", fresh) == ("s1", "assets/c.png")


@pytest.mark.parametrize("token", ["", "x", "not.a.token", "a" * 400, "eyJzIjoiczEiLCJwIjoiYS5wbmcifQ.AAAA.BBBB"])
def test_a_tampered_or_made_up_token_is_refused(token):
    assert verify_file_token("s3cret", token) is None


def test_a_token_signed_for_another_purpose_is_refused():
    from itsdangerous import URLSafeTimedSerializer

    other = URLSafeTimedSerializer("s3cret", salt="primer.session").dumps({"s": "s1", "p": "assets/c.png"})
    assert verify_file_token("s3cret", other) is None


@pytest.mark.parametrize("payload", [["s1", "a.png"], {"s": "s1"}, {"s": 1, "p": "a.png"}, {"s": "s1", "p": None}, "s1"])
def test_a_signed_payload_of_the_wrong_shape_is_refused(payload):
    from itsdangerous import URLSafeTimedSerializer

    token = URLSafeTimedSerializer("s3cret", salt=FILE_TOKEN_SALT).dumps(payload)
    assert verify_file_token("s3cret", token) is None


def test_referenced_files_are_the_file_sources_of_images_and_audio():
    elements = {
        "root": {"type": "Column", "props": {}, "children": ["a", "b", "c", "d", "e"]},
        "a": {"type": "Image", "props": {"src": "file:assets/c.png", "alt": "c"}},
        "b": {"type": "Audio", "props": {"src": "FILE:music/song.mp3"}},
        "c": {"type": "Image", "props": {"src": "https://cdn.example.com/x.png", "alt": "x"}},
        "d": {"type": "Html", "props": {"html": "<img src='file:secret.png'>"}},
        "e": {"type": "Link", "props": {"label": "go", "href": "file:notes.png"}},
    }
    assert referenced_files(elements) == {"assets/c.png", "music/song.mp3"}


@pytest.mark.parametrize("path, safe", [
    ("assets/c.png", True),
    ("c.png", True),
    ("../c.png", False),
    ("assets/../../c.png", False),
    ("/etc/c.png", False),
    ("assets//c.png", False),
    ("./c.png", False),
    ("assets\\c.png", False),
    ("assets/c.png\x00", False),
    ("", False),
])
def test_a_safe_path_stays_inside_the_workspace(path, safe):
    assert safe_relative_path(path) is safe


def test_media_types_are_an_allowlist():
    assert media_type_of("a/b.PNG") == "image/png" and media_type_of("x.jpeg") == "image/jpeg" and media_type_of("s.weba") == "audio/webm"
    assert media_type_of("a.svg") is None and media_type_of("a.html") is None and media_type_of("noext") is None
    assert set(MEDIA_TYPES) == {"png", "jpg", "jpeg", "gif", "webp", "avif", "mp3", "ogg", "oga", "wav", "m4a", "aac", "flac", "weba"}


def test_the_secret_is_the_session_secret_else_one_per_process():
    assert canvas_file_secret(SimpleNamespace(session_secret="cookie-secret")) == "cookie-secret"
    state = SimpleNamespace(session_secret=None)
    first = canvas_file_secret(state)
    assert len(first) >= 32 and canvas_file_secret(state) == first, "auth-disabled installs keep one secret for the process"

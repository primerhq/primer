"""``origin_of``: the scheme, host and port a credential is sent to (ticket 01a1212a).

Two URLs have the same origin when scheme, host and port are equal after the normalisation a client does anyway (the case of the scheme and the host, the default port of the scheme spelled out or
left out). The path, query, fragment and userinfo are not part of an origin. A value that is not ``scheme://host...`` is its own origin, compared as the text.
"""

from __future__ import annotations

import pytest

from primer.common.origin import origin_of


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param("https://home.example/v1", "https://home.example/v2?x=1#f", id="path, query and fragment"),
        pytest.param("https://HOME.example/v1", "https://home.example", id="case of the host"),
        pytest.param("HTTPS://home.example", "https://home.example", id="case of the scheme"),
        pytest.param("https://home.example:443/v1", "https://home.example/v1", id="https default port spelled out"),
        pytest.param("http://home.example:80", "http://home.example", id="http default port spelled out"),
        pytest.param("wss://home.example:443/s", "wss://home.example/t", id="wss default port"),
        pytest.param("ws://home.example:80/s", "ws://home.example/t", id="ws default port"),
        pytest.param("https://svc:pw@home.example/v1", "https://home.example/v1", id="userinfo is not part of an origin"),
        pytest.param("https://svc:pw@home.example/v1", "https://other:x@home.example/v1", id="another user, same origin"),
        pytest.param("tcp://docker:2375", "tcp://DOCKER:2375/x", id="a scheme with no default port"),
        pytest.param("http://[::1]:8080/x", "http://[::1]:8080/y", id="an IPv6 literal"),
        pytest.param("  https://home.example  ", "https://home.example", id="surrounding whitespace"),
        pytest.param("not a url", "not a url", id="the same text"),
    ],
)
def test_the_same_origin(a: str, b: str) -> None:
    assert origin_of(a) == origin_of(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param("https://home.example", "http://home.example", id="scheme"),
        pytest.param("https://home.example", "https://home.example:8443", id="port"),
        pytest.param("https://home.example:443", "https://home.example:444", id="another explicit port"),
        pytest.param("https://home.example", "https://attacker.example", id="host"),
        pytest.param("https://home.example", "https://home.example.attacker.example", id="a host that begins with the stored one"),
        pytest.param("https://home.example", "https://attacker.example/home.example", id="the stored host in the path"),
        pytest.param("https://home.example", "https://home.example@attacker.example", id="the stored host in the userinfo"),
        pytest.param("https://home.example", "https://home.example:pw@attacker.example", id="the stored host as the user"),
        pytest.param("ws://home.example", "http://home.example", id="ws is not http"),
        pytest.param("tcp://docker:2375", "tcp://docker:2376", id="a scheme with no default port: the port counts"),
        pytest.param("not a url", "not another url", id="two different texts"),
        pytest.param("not a url", "https://home.example", id="a text and a URL"),
        pytest.param("https://home.example", "", id="empty"),
    ],
)
def test_another_origin(a: str, b: str) -> None:
    assert origin_of(a) != origin_of(b)


@pytest.mark.parametrize("value", ["", "   ", "home.example", "//home.example/x", "unix:///var/run/docker.sock", "http://h:99999", "http://h:notaport", "http://[::1"])
def test_a_value_without_a_usable_host_is_its_own_origin_and_never_raises(value: str) -> None:
    assert origin_of(value) == ("text", value.strip())


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("https://attacker.example\\@home.example", id="a backslash before the @: urlsplit reads home.example, urllib3 and browsers read attacker.example"),
        pytest.param("https://home.example\\.attacker.example/x", id="a backslash inside the host"),
        pytest.param("https://home.example attacker.example/x", id="a space inside the authority"),
        pytest.param("https://home.example\tattacker.example/x", id="a tab inside the authority"),
        pytest.param("https://home.example\n.attacker.example/", id="a newline inside the authority"),
    ],
)
def test_an_authority_that_parsers_read_differently_is_its_own_origin(value: str) -> None:
    """A backslash or whitespace in the authority is read differently by ``urlsplit`` and by the client that makes the request, so the host it would name is not trusted: the value is compared as text
    (the same text keeps the secret, anything else is another origin)."""
    assert origin_of(value) == ("text", value.strip())
    assert origin_of(value) != origin_of("https://home.example")
    assert origin_of(value) != origin_of("https://attacker.example")


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("https:\t//attacker.example\\@home.example/x://y", id="a tab in the scheme separator hides the authority from a scan for the first ://"),
        pytest.param("https:\t//attacker.example\\@home.example/?next=http://z", id="the same, with another :// after the path"),
        pytest.param("https:/\t/attacker.example\\@home.example/a://b", id="a tab between the two slashes"),
        pytest.param("https:\n//attacker.example\\@home.example/a://b", id="a newline in the scheme separator"),
        pytest.param("https:\r//attacker.example\\@home.example/a://b", id="a carriage return in the scheme separator"),
        pytest.param("https://home.example/a\tb", id="a tab anywhere, even in the path, is not worth trusting a parse for"),
    ],
)
def test_a_control_character_in_the_text_is_its_own_origin(value: str) -> None:
    """``urlsplit`` drops a tab, CR or LF from anywhere in the text, WHATWG parsers (``HttpUrl``) drop them too and read a backslash as a slash, so a tab in the scheme separator hid the backslash
    authority from the scan for the first ``://`` while ``urlsplit`` still read the stored host: the secret was restored next to a value the client reads as ``attacker.example`` (round 2 review,
    N4b). Such a value is compared as text."""
    assert origin_of(value) == ("text", value.strip())
    assert origin_of(value) != origin_of("https://home.example")


def test_a_backslash_or_space_after_the_authority_is_only_part_of_the_path() -> None:
    assert origin_of("https://home.example/a\\b c") == origin_of("https://home.example")
    assert origin_of("https://home.example?q=a b") == origin_of("https://home.example")

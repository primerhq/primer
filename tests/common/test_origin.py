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

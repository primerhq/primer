"""Source registry."""

import pytest

from primer.trigger.sources import SOURCES, get_source


def test_registry_has_delayed_and_scheduled():
    assert "delayed" in SOURCES
    assert "scheduled" in SOURCES


def test_get_source_unknown_raises():
    with pytest.raises(KeyError):
        get_source("bogus")

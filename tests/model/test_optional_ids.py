import re

import pytest
from typing import ClassVar

from primer.model.common import Identifiable


class _WithPrefix(Identifiable):
    _id_prefix: ClassVar[str] = "thing"


class _NoPrefix(Identifiable):
    pass


def test_autogen_when_id_omitted():
    e = _WithPrefix()
    assert re.fullmatch(r"thing-[0-9a-f]{12}", e.id), e.id


def test_supplied_id_is_kept():
    e = _WithPrefix(id="my-id")
    assert e.id == "my-id"


def test_empty_string_id_autogenerates():
    e = _WithPrefix(id="")
    assert re.fullmatch(r"thing-[0-9a-f]{12}", e.id), e.id


def test_no_prefix_subclass_requires_id():
    with pytest.raises(Exception):  # ValidationError: id is required
        _NoPrefix()
    assert _NoPrefix(id="x").id == "x"

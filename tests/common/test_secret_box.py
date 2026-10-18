"""SecretBox: Fernet sealing of a secret value at rest (plan 07, Task 4; design 12.6).

The OAuth tokens and client secrets primer stores are sealed with a key derived from one piece of key material (``PRIMER_ENCRYPTION_KEY``, or the session
secret as a fallback). Every server process derives the same key from the same material, so a token one pod sealed another pod opens. What these tests
pin: the round trip, that two boxes with one key interoperate, that another key or a damaged value is a ``SecretBoxError`` (never a different exception,
never the plaintext), and the derivation itself: a stored value must stay readable across releases, so the salt, the info string and the framing are
fixed by a known-answer vector.
"""

from __future__ import annotations

import base64

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from primer.common.secret_box import SecretBox, SecretBoxError, is_sealed

KEY = "k" * 32

# Sealed once by this implementation with KEY: a value written by an earlier build must still open, so this vector may only ever be ADDED to.
KNOWN_ANSWER = "v1.gAAAAABq1RBu8lmPmi2ZbHu7SgwEXiqBfYKpFuR9oy9cSP8nSO8KvH1tWM8M2nPBkfxKY4Qs-R5HD8XdYDO9sy-p8V_kAxZdnzPw2ymKGF14asp1F9UV_v4="
KNOWN_PLAINTEXT = "refresh-token-0123456789"


def test_round_trip():
    box = SecretBox.from_key_material(KEY)
    sealed = box.encrypt("tok")
    assert box.decrypt(sealed) == "tok"
    assert sealed.startswith("v1.")
    assert "tok" not in sealed


def test_two_boxes_with_one_key_interoperate():
    sealed = SecretBox.from_key_material(KEY).encrypt("tok")
    assert SecretBox.from_key_material(KEY).decrypt(sealed) == "tok"


def test_another_key_cannot_decrypt():
    sealed = SecretBox.from_key_material(KEY).encrypt("tok")
    with pytest.raises(SecretBoxError):
        SecretBox.from_key_material("j" * 32).decrypt(sealed)


@pytest.mark.parametrize("garbage", ["v1.zzz", "plain", "", "v1.", "v2.abc", "gAAAAAB", "v1.éé", "v1." + "A" * 200])
def test_garbage_is_a_secret_box_error(garbage):
    with pytest.raises(SecretBoxError):
        SecretBox.from_key_material(KEY).decrypt(garbage)


def test_a_flipped_character_is_a_secret_box_error():
    box = SecretBox.from_key_material(KEY)
    sealed = box.encrypt("tok")
    flipped = sealed[:-4] + ("A" if sealed[-4] != "A" else "B") + sealed[-3:]
    with pytest.raises(SecretBoxError):
        box.decrypt(flipped)


def test_an_unsealed_fernet_token_is_not_accepted_without_the_version_prefix():
    """Only what this class sealed is opened: a bare Fernet token made with the same key is refused, so a value without the framing is never mistaken for a sealed one."""
    box = SecretBox.from_key_material(KEY)
    with pytest.raises(SecretBoxError):
        box.decrypt(box.encrypt("tok").removeprefix("v1."))


def test_empty_key_material_is_refused():
    with pytest.raises(ValueError):
        SecretBox.from_key_material("")


def test_sealing_twice_gives_two_different_values():
    box = SecretBox.from_key_material(KEY)
    assert box.encrypt("tok") != box.encrypt("tok")


@pytest.mark.parametrize("plaintext", ["", "x", "tok with spaces and éè and 中文", "a" * 10_000])
def test_any_string_round_trips(plaintext):
    box = SecretBox.from_key_material(KEY)
    assert box.decrypt(box.encrypt(plaintext)) == plaintext


def test_the_error_names_neither_the_plaintext_nor_the_sealed_value():
    box = SecretBox.from_key_material(KEY)
    sealed = box.encrypt("super-secret-plaintext")
    with pytest.raises(SecretBoxError) as caught:
        SecretBox.from_key_material("j" * 32).decrypt(sealed)
    text = f"{caught.value} {caught.value!r}"
    assert "super-secret-plaintext" not in text and sealed not in text and sealed.removeprefix("v1.") not in text


def test_the_key_is_hkdf_sha256_of_the_material_with_the_pinned_salt_and_info():
    """The derivation is part of the on-disk format: re-deriving it independently opens what the box sealed, and the other way round."""
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"primer-secret-box-v1", info=b"primer/mcp-oauth").derive(KEY.encode("utf-8"))
    independent = Fernet(base64.urlsafe_b64encode(derived))
    box = SecretBox.from_key_material(KEY)

    assert independent.decrypt(box.encrypt("tok").removeprefix("v1.").encode("ascii")).decode("utf-8") == "tok"
    assert box.decrypt("v1." + independent.encrypt(b"tok").decode("ascii")) == "tok"


def test_a_value_sealed_by_an_earlier_build_still_opens():
    assert SecretBox.from_key_material(KEY).decrypt(KNOWN_ANSWER) == KNOWN_PLAINTEXT


@pytest.mark.parametrize("plaintext", ["", "x", "a long token " * 50, "\u4e2d\u6587"])
def test_what_the_box_seals_has_the_sealed_shape(plaintext):
    assert is_sealed(SecretBox.from_key_material(KEY).encrypt(plaintext))
    assert is_sealed(KNOWN_ANSWER)


@pytest.mark.parametrize(
    "not_sealed",
    ["", "tok", "v1.", "v1.abc", "v1.gAAAA", "gAAAAAB" + "A" * 100, "v2.gAAAA" + "A" * 100, "Bearer " + "A" * 120, "v1.gAAAA" + "A" * 100 + "\n", None, 7],
)
def test_plaintext_and_junk_do_not_have_the_sealed_shape(not_sealed):
    """The shape check is a tripwire for plaintext in a column that wants ciphertext; it must not wave through the things a token looks like."""
    assert not is_sealed(not_sealed)


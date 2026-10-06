"""The encryption of an application backup (issue #226): scrypt and AES-256-GCM, bounded and authenticated."""

import base64
import json
import struct

import pytest

pytest.importorskip("cryptography")

from homelab_probe.backup import BackupError  # noqa: E402
from homelab_probe.server import backup_crypto as crypto  # noqa: E402

PASS = "correct horse battery"


@pytest.fixture(autouse=True)
def cheap_key(monkeypatch):
    """A small cost for the many tests; one test below uses the real parameters."""
    monkeypatch.setattr(crypto, "SCRYPT_N", 2 ** 10)


def code(call):
    with pytest.raises(BackupError) as caught:
        call()
    return caught.value.code


def parts(blob):
    (length,) = struct.unpack(">I", blob[10:14])
    return blob[:10], json.loads(blob[14:14 + length]), blob[14 + length:]


def rebuild(header, ciphertext):
    text = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    return crypto.MAGIC + struct.pack(">I", len(text)) + text + ciphertext


def test_a_sealed_file_opens_with_the_passphrase_and_only_with_it():
    blob = crypto.seal(b"the archive", PASS)
    assert crypto.open_sealed(blob, PASS) == b"the archive"
    assert code(lambda: crypto.open_sealed(blob, PASS + "x")) == "decrypt"
    assert b"the archive" not in blob and blob.startswith(crypto.MAGIC)


def test_the_real_parameters_are_scrypt_65536_8_1_and_the_header_names_them(monkeypatch):
    monkeypatch.undo()
    blob = crypto.seal(b"x", PASS)
    header = parts(blob)[1]
    assert (header["kdf"], header["n"], header["r"], header["p"], header["cipher"], header["format"]) == (
        "scrypt", 65536, 8, 1, "aes-256-gcm", 1)
    assert crypto.open_sealed(blob, PASS) == b"x"


def test_every_sealing_uses_a_fresh_salt_and_nonce():
    one, two = crypto.seal(b"same", PASS), crypto.seal(b"same", PASS)
    assert one != two and parts(one)[1]["salt"] != parts(two)[1]["salt"] and parts(one)[1]["nonce"] != parts(two)[1]["nonce"]


def test_the_passphrase_is_not_in_the_file():
    assert PASS.encode() not in crypto.seal(PASS.encode() * 3, PASS)


def test_changing_any_byte_of_the_file_fails_and_never_returns_other_data():
    blob = crypto.seal(b"the archive", PASS)
    for index in range(len(blob)):
        broken = bytearray(blob)
        broken[index] ^= 0x01
        with pytest.raises(BackupError) as caught:
            crypto.open_sealed(bytes(broken), PASS)
        assert caught.value.code in {"decrypt", "not_a_backup", "unsupported_format"}, index


def test_a_header_that_was_changed_is_refused_even_when_it_still_parses():
    prefix, header, ciphertext = parts(crypto.seal(b"x", PASS))
    other = dict(header, salt=base64.b64encode(b"s" * 16).decode())
    assert code(lambda: crypto.open_sealed(rebuild(other, ciphertext), PASS)) == "decrypt"
    # the same key and nonce with a header that differs only by an extra field: authenticated, so refused
    extra = dict(header, note="added")
    assert code(lambda: crypto.open_sealed(rebuild(extra, ciphertext), PASS)) == "decrypt"


def test_a_truncated_or_extended_file_fails():
    blob = crypto.seal(b"x" * 100, PASS)
    assert code(lambda: crypto.open_sealed(blob[:-1], PASS)) == "decrypt"
    assert code(lambda: crypto.open_sealed(blob + b"\0", PASS)) == "decrypt"
    assert code(lambda: crypto.open_sealed(blob[:30], PASS)) == "not_a_backup"


@pytest.mark.parametrize("blob", [b"", b"HLPBACKUP", b"not a backup at all", b"HLPBACKUP\n\x00\x00", b"PK\x03\x04" + b"0" * 40,
                                  b"HLPBACKUP\n" + struct.pack(">I", 10 ** 6) + b"{}" * 20,
                                  b"HLPBACKUP\n" + struct.pack(">I", 5) + b"\xff\xfe\xfd\xfc\xfb" + b"0" * 40,
                                  b"HLPBACKUP\n" + struct.pack(">I", 2) + b"[]" + b"0" * 40])
def test_something_that_is_not_a_backup_is_said_so(blob):
    assert code(lambda: crypto.open_sealed(blob, PASS)) == "not_a_backup"


@pytest.mark.parametrize("change", [
    {"format": 2}, {"format": True}, {"kdf": "pbkdf2"}, {"cipher": "chacha20"}, {"n": 2 ** 30}, {"n": 1000}, {"n": 1},
    {"n": 0}, {"n": "x"}, {"n": True}, {"r": 10 ** 6}, {"p": 100}, {"p": 0}, {"salt": "!!!"}, {"salt": ""},
    {"salt": 5}, {"nonce": base64.b64encode(b"x").decode()}])
def test_parameters_out_of_bounds_are_refused_before_any_key_is_derived(change, monkeypatch):
    prefix, header, ciphertext = parts(crypto.seal(b"x", PASS))
    monkeypatch.setattr(crypto, "_key", lambda *args: pytest.fail("a key was derived"))
    assert code(lambda: crypto.open_sealed(rebuild({**header, **change}, ciphertext), PASS)) == "unsupported_format"


def test_the_messages_are_fixed_text_that_never_holds_the_passphrase_or_a_header_value():
    blob = crypto.seal(b"x", PASS)
    with pytest.raises(BackupError) as caught:
        crypto.open_sealed(blob, "a very wrong passphrase")
    assert str(caught.value) == BackupError.__init__.__globals__["MESSAGES"]["decrypt"]
    assert "passphrase" in str(caught.value) and "a very wrong" not in str(caught.value)

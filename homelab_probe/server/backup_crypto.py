"""The encryption of an application backup (issue #226): a passphrase, scrypt and AES-256-GCM from ``cryptography``.

The sealed file is::

    HLPBACKUP\\n                    ten bytes of magic
    <header length>                 4 bytes, big-endian
    <header>                        JSON: format, kdf, n, r, p, salt, cipher, nonce (base64 where binary)
    <ciphertext and tag>            AES-256-GCM of the archive

The key is scrypt of the passphrase and the salt, with the parameters written in the header (like the account
hashes, so a later release can raise them and still open an old file). The magic, the length and the header are the
**associated data** of the cipher, so a changed header fails the same way a changed byte does. A wrong passphrase and
a modified file are the same failure on purpose (``BackupError("decrypt")``): AES-GCM cannot tell them apart, and the
message does not pretend to. Nothing here is custom cryptography, and nothing keeps the passphrase.

The parameters read from a file are bounded (``MAX_N``...) before they are used, so a crafted file cannot make the
server allocate gigabytes. The core never imports this module: the ``cryptography`` package is of the ``web`` extra.
"""

import base64
import binascii
import json
import os
import struct
from typing import Any, Dict, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ..backup import BackupError

MAGIC = b"HLPBACKUP\n"
FORMAT = 1                                   # the version of this container (not of the archive inside it)
CIPHER = "aes-256-gcm"
KDF = "scrypt"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 16, 8, 1       # about 64 MiB and a fraction of a second
MAX_N, MAX_R, MAX_P = 2 ** 17, 16, 4               # what a file may ask for: more is refused, not computed
SALT_BYTES, NONCE_BYTES, KEY_BYTES = 16, 12, 32
MAX_HEADER = 4096
MIN_PASSPHRASE, MAX_PASSPHRASE = 12, 1024


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=KEY_BYTES, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def seal(plain: bytes, passphrase: str) -> bytes:
    """``plain`` encrypted with ``passphrase`` (a fresh salt and nonce every time)."""
    salt, nonce = os.urandom(SALT_BYTES), os.urandom(NONCE_BYTES)
    header = json.dumps({"format": FORMAT, "kdf": KDF, "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                         "salt": _b64(salt), "cipher": CIPHER, "nonce": _b64(nonce)},
                        sort_keys=True, separators=(",", ":")).encode("ascii")
    prefix = MAGIC + struct.pack(">I", len(header)) + header
    return prefix + AESGCM(_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(nonce, plain, prefix)


def _split(blob: bytes) -> Tuple[bytes, Dict[str, Any], bytes]:
    """(the prefix that is authenticated, the header, the ciphertext) of ``blob``; ``not_a_backup`` for a file that is
    not shaped like one."""
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 4:
        raise BackupError("not_a_backup")
    (length,) = struct.unpack(">I", blob[len(MAGIC):len(MAGIC) + 4])
    end = len(MAGIC) + 4 + length
    if length > MAX_HEADER or len(blob) < end + 16:
        raise BackupError("not_a_backup")
    try:
        header = json.loads(blob[len(MAGIC) + 4:end].decode("ascii"))
    except (ValueError, UnicodeError):
        raise BackupError("not_a_backup") from None
    if not isinstance(header, dict):
        raise BackupError("not_a_backup")
    return blob[:end], header, blob[end:]


def _int(header: Dict[str, Any], name: str, highest: int) -> int:
    value = header.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= highest:
        raise BackupError("unsupported_format")
    return value


def _bytes(header: Dict[str, Any], name: str, length: int) -> bytes:
    value = header.get(name)
    try:
        raw = base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != length:
        raise BackupError("unsupported_format")
    return raw


def open_sealed(blob: bytes, passphrase: str) -> bytes:
    """The archive inside ``blob``. ``BackupError``: ``not_a_backup`` (no magic or a damaged header),
    ``unsupported_format`` (another container version, cipher or key derivation, or parameters out of bounds: said
    before any key is derived) or ``decrypt`` (wrong passphrase, or any byte of the file changed)."""
    prefix, header, ciphertext = _split(blob)
    version = header.get("format")
    if isinstance(version, bool) or version != FORMAT or header.get("kdf") != KDF or header.get("cipher") != CIPHER:
        raise BackupError("unsupported_format")
    n, r, p = _int(header, "n", MAX_N), _int(header, "r", MAX_R), _int(header, "p", MAX_P)
    if n < 2 or n & (n - 1):
        raise BackupError("unsupported_format")             # scrypt wants a power of two
    salt, nonce = _bytes(header, "salt", SALT_BYTES), _bytes(header, "nonce", NONCE_BYTES)
    try:
        return AESGCM(_key(passphrase, salt, n, r, p)).decrypt(nonce, ciphertext, prefix)
    except InvalidTag:
        raise BackupError("decrypt") from None

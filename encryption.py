# -----------------------------------------------------------------------------
# encryption.py
# Argon2id key derivation and AES-256-GCM field encryption.
#
# The key is never written to disk and is released on lock, but it is not
# scrubbed from RAM and cannot be: Python strings and bytes are immutable, and
# further copies live inside argon2-cffi, cryptography and Qt's password field.
# Full-disk encryption and a short auto-lock cover what the OS pages out.
# Say "release", not "wipe", anywhere this is described.
# -----------------------------------------------------------------------------


import os
import base64
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from argon2.low_level import hash_secret_raw, Type


_key_instance: AESGCM | None = None

_NONCE_LEN = 12  # 96-bit nonce — GCM recommended size
_TAG_LEN = 16    # GCM authentication tag, appended to the ciphertext

_MIN_TOKEN_LEN = _NONCE_LEN + _TAG_LEN

# Returned in place of a field that will not decrypt. Handing back the raw
# token instead would let ciphertext pass as the value: it would render as the
# password, export into backups as the password, and be written back as the
# password by the next save.
UNREADABLE = '⚠ unreadable'


class VaultLockedError(RuntimeError):
    """Raised when a crypto operation is attempted while the vault is locked.
    Subclasses RuntimeError so existing `except RuntimeError` guards still apply."""


def generate_salt() -> bytes:
    return os.urandom(32)


# Above OWASP's floor of 19 MiB / t=2, and shared with the master-password
# verifier in auth.py: both recover the same password from a stolen database,
# so an attacker works on whichever is cheaper and the pair is only as strong
# as its weaker half.
#
# These are the values *new* key material is derived under, not the values every
# existing field was derived under. A vault records the parameters it was built
# with (app_config.kdf_params) and a .vault backup carries its own copy, so
# raising these here no longer orphans anything: auth.migrate_vault_format
# re-keys the vault to the new figures at the next login, and derive_key opens
# older material by being handed the parameters it was written with.
ARGON2_TIME_COST   = 4
ARGON2_MEMORY_COST = 131072  # KiB — 128 MiB
ARGON2_PARALLELISM = 2


# The work factors everything written before parameters were recorded was
# derived under. History, not configuration: they are the only way to reproduce
# a key for a vault or a .vault backup from before the figures were written
# down, so they must never be edited to follow the constants above. Raising
# those is what triggers a re-key; this is what still opens what came before.
LEGACY_KDF_PARAMS = {'t': 4, 'm': 131072, 'p': 2}


def current_kdf_params() -> dict:
    """The work factors new key material is derived under.

    A function rather than a module-level dict because the constants above are
    substituted in tests, and a dict built at import time would hold the real
    128 MiB figures for the whole run."""
    return {'t': ARGON2_TIME_COST, 'm': ARGON2_MEMORY_COST, 'p': ARGON2_PARALLELISM}


# What a stored or imported parameter set has to fall inside to be believed.
#
# The ceiling is the point: these arrive from app_config and from the header of
# a .vault file, and both are read *before* anything is authenticated. Without a
# bound, a hand-built backup naming 64 GiB of memory cost is a one-line denial
# of service against whoever opens it. The upper limits are far above the real
# figures above and far below anything that would wedge a machine.
_KDF_BOUNDS = {
    't': (1, 64),
    'm': (8, 1024 * 1024),  # KiB — 8 KiB (argon2's own floor) to 1 GiB
    'p': (1, 16),
}


def valid_kdf_params(params) -> dict | None:
    """params reduced to the three Argon2id work factors, or None if it is not a
    usable set. Booleans are rejected explicitly: bool is a subclass of int, so
    True would otherwise pass as t=1."""
    if not isinstance(params, dict):
        return None
    clean = {}
    for key, (low, high) in _KDF_BOUNDS.items():
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            return None
        clean[key] = value
    return clean


def derive_key(password: str, salt: bytes, params: dict | None = None) -> bytes:
    """Returns 32 raw bytes via Argon2id. Suitable as an AES-256-GCM key.

    params defaults to current_kdf_params(); pass the stored set to reproduce a
    key that existing material was written under."""
    params = params or current_kdf_params()
    return hash_secret_raw(
        secret=password.encode(),
        salt=salt,
        time_cost=params['t'],
        memory_cost=params['m'],
        parallelism=params['p'],
        hash_len=32,
        type=Type.ID
    )


# Lets anything holding decrypted data drop it at the moment the vault can no
# longer decrypt. Registration is one-way and process-wide.
_key_listeners = []


def on_key_change(callback):
    """Registers a callback fired on every unlock and every lock.

    A listener must be cheap and must not raise."""
    _key_listeners.append(callback)


def _notify_key_change():
    # A listener that raises part-way through a lock would leave the remaining
    # listeners holding decrypted data while the key is already gone.
    for callback in _key_listeners:
        try:
            callback()
        except Exception:
            pass


def install_key(password: str, salt: bytes, params: dict | None = None):
    """Derives the session key and installs it for the process.

    params is the work-factor set the vault's material was written under; it
    defaults to the current one for a vault being created."""
    global _key_instance
    _key_instance = AESGCM(derive_key(password, salt, params))
    # Also fires on unlock: a password change re-keys the vault and lands here,
    # so anything cached under the old key is stale rather than unreadable.
    _notify_key_change()


def release_key():
    """Releases the in-memory key. Called on lock.

    Drops the only reference the app holds, so nothing here can decrypt
    afterwards. The bytes survive in freed heap — see the file header."""
    global _key_instance
    _key_instance = None
    _notify_key_change()


def is_unlocked() -> bool:
    return _key_instance is not None


def field_aad(table: str, column: str) -> bytes:
    """Associated data binding a field's ciphertext to the column it lives in.

    AES-GCM authenticates a token against tampering but not against relocation:
    with no associated data, a token lifted out of `notes` and dropped into
    `password` decrypts perfectly, and the vault then shows — and copies to the
    clipboard — one field's contents as another's. Anyone who can write the
    database file can do that without knowing the key.

    Binding the column name makes the tag depend on where the value is stored,
    so a moved token fails to open. It is deliberately not bound to the row id
    as well: ids are reassigned by an import, which would make every restored
    backup unreadable to buy protection against shuffling values between rows of
    the same column — the far less useful half of the attack."""
    return f'{table}.{column}'.encode()


def encrypt(value: str, aad: bytes = b'') -> str:
    """Encrypts a string under the session key. Returns
    base64url(nonce || ciphertext || tag)."""
    if _key_instance is None:
        raise VaultLockedError("Encryption key not initialized — app is locked")
    return encrypt_with(_key_instance, value.encode(), aad)


def _split_token(value: str) -> tuple[bytes, bytes]:
    """Splits a token into (nonce, ciphertext||tag), or raises InvalidTag.

    InvalidTag rather than the ValueError a short nonce would raise out of
    AESGCM: a truncated token has failed authentication just as surely as a
    tampered one, and callers already handle that exception."""
    raw = base64.urlsafe_b64decode(value)
    if len(raw) < _MIN_TOKEN_LEN:
        raise InvalidTag()
    return raw[:_NONCE_LEN], raw[_NONCE_LEN:]


def decrypt(value: str, aad: bytes = b'') -> str:
    """Decrypts a token produced by encrypt(), under the session key."""
    if _key_instance is None:
        raise VaultLockedError("Encryption key not initialized — app is locked")
    return decrypt_with(_key_instance, value, aad).decode()


def cipher_for(key: bytes) -> AESGCM:
    """The AES-GCM cipher for a raw 32-byte key.

    Building one runs the AES key schedule, so a re-key walking every field in
    the vault should build one cipher and reuse it rather than one per field."""
    return AESGCM(key)


def _as_cipher(key) -> AESGCM:
    return key if isinstance(key, AESGCM) else AESGCM(key)


def encrypt_with(key, plaintext: bytes, aad: bytes = b'') -> str:
    """Encrypts raw bytes under an explicit key or cipher, in the same
    base64url(nonce || ciphertext || tag) envelope encrypt() produces.

    Every AES-GCM envelope in the app is built here: the guarantee that no
    nonce repeats under one key holds only while one site chooses them.

    aad defaults to empty, which AES-GCM treats exactly as it treats absent —
    the tag is byte-identical either way. That is what keeps tokens written
    before field binding existed readable by an unbound call."""
    aesgcm = _as_cipher(key)
    nonce = os.urandom(_NONCE_LEN)
    return base64.urlsafe_b64encode(nonce + aesgcm.encrypt(nonce, plaintext, aad)).decode()


def decrypt_with(key, token: str, aad: bytes = b'') -> bytes:
    """Decrypts a token under an explicit key or cipher."""
    nonce, ct = _split_token(token)
    return _as_cipher(key).decrypt(nonce, ct, aad)


def reencrypt(value: str, new_key, aad: bytes = b'', new_aad: bytes = b'') -> str:
    """Decrypts with the current key and re-encrypts with new_key, which may be
    raw 32 bytes or a cipher from cipher_for.

    The two associated-data values differ only while a vault is being moved onto
    a format that binds its fields differently — see auth._rekey_fields. Both
    are spelled out at that one call site rather than one defaulting to the
    other, because a re-key that reads and writes the wrong binding is a vault
    nothing opens again."""
    if _key_instance is None:
        raise VaultLockedError("Encryption key not initialized — app is locked")
    if not value:
        return value
    nonce, ct = _split_token(value)
    plaintext = _key_instance.decrypt(nonce, ct, aad)
    return encrypt_with(new_key, plaintext, new_aad)


def _b64decode_strict(value: str) -> bytes:
    """base64url-decodes value, rejecting anything encrypt() would not have
    written. Raises binascii.Error on bad input.

    base64.urlsafe_b64decode discards characters outside the alphabet, so a
    long enough piece of prose can decode to something token-sized and be
    mistaken for ciphertext. Such a field is then skipped by
    migrate_plaintext_fields as already encrypted and left in the clear, and
    fails to decrypt during the next password change, aborting every future
    rotation."""
    return base64.b64decode(value, altchars=b'-_', validate=True)


def looks_encrypted(value) -> bool:
    """True when value has the shape encrypt() produces.

    Separates "never encrypted" from "encrypted and broken", which a failed
    decrypt alone cannot. Genuine ciphertext always satisfies it, so nothing
    real is mistaken for plaintext and wrapped a second time; a plaintext note
    long enough to pass only ever costs a refusal to touch it."""
    if not isinstance(value, str) or not value:
        return False
    try:
        raw = _b64decode_strict(value)
    except Exception:
        return False
    return len(raw) >= _MIN_TOKEN_LEN


def is_unreadable(value) -> bool:
    """True for the sentinel safe_decrypt returns in place of a broken field."""
    return value == UNREADABLE


def safe_decrypt(value: str, aad: bytes = b'') -> str:
    """Decrypt value, tolerating rows this vault did not write.

    Legacy plaintext is returned as-is and undecryptable ciphertext returns
    UNREADABLE, so one corrupt field degrades to a placeholder instead of
    passing itself off as the value. A locked vault re-raises, so callers
    cannot stamp placeholders over a vault that is merely closed."""
    if not looks_encrypted(value):
        return value
    try:
        return decrypt(value, aad)
    except VaultLockedError:
        raise
    except Exception:
        return UNREADABLE


def safe_reencrypt(value: str, new_key, aad: bytes = b'', new_aad: bytes = b'') -> str:
    """Reencrypt with new_key, encrypting legacy plaintext directly.

    Ciphertext that will not decrypt raises rather than being treated as
    plaintext: wrapping it would encrypt the base64 token and lose the real
    value for good, since the new key would return the token and the old
    password would no longer open anything."""
    if not looks_encrypted(value):
        return encrypt_with(new_key, value.encode(), new_aad)
    return reencrypt(value, new_key, aad, new_aad)

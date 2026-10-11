# -----------------------------------------------------------------------------
# core/encryption.py
# Argon2id key derivation and AES-256-GCM field encryption.
#
# The key is never written to disk and is released on lock, but it is not
# scrubbed from RAM and cannot be: Python strings and bytes are immutable, and
# further copies live inside argon2-cffi, cryptography and Qt's password field.
# Full-disk encryption and a short auto-lock cover what the OS pages out.
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

# Returned in place of a field that will not decrypt. Returning the raw token
# instead would let ciphertext pass as the value — shown, exported and saved
# back as if it were the password.
UNREADABLE = '⚠ unreadable'


class VaultLockedError(RuntimeError):
    """Raised when a crypto operation is attempted while the vault is locked."""


def generate_salt() -> bytes:
    return os.urandom(32)


# Above OWASP's floor of 19 MiB / t=2, and shared with the master-password
# verifier in auth.py: an attacker holding the database attacks whichever is
# cheaper, so the two must cost the same.
#
# These are the values *new* key material is derived under. A vault records the
# parameters it was built with (app_config.kdf_params), so raising these does
# not orphan anything: auth.migrate_vault_format re-keys at the next login.
ARGON2_TIME_COST   = 4
ARGON2_MEMORY_COST = 131072  # KiB — 128 MiB
ARGON2_PARALLELISM = 2


# The work factors everything written before parameters were recorded was
# derived under. Never edit these to follow the constants above: they are the
# only way to reproduce the key for a vault from before the figures were stored.
LEGACY_KDF_PARAMS = {'t': 4, 'm': 131072, 'p': 2}


def current_kdf_params() -> dict:
    """The work factors new key material is derived under.

    A function rather than a module-level dict so tests can substitute the
    constants above."""
    return {'t': ARGON2_TIME_COST, 'm': ARGON2_MEMORY_COST, 'p': ARGON2_PARALLELISM}


# Bounds a stored or imported parameter set must fall inside to be believed.
# Parameters are read from app_config before anything is authenticated, so the
# ceiling stops a hand-built vault naming 64 GiB of memory from wedging the
# machine that opens it.
_KDF_BOUNDS = {
    't': (1, 64),
    'm': (8, 1024 * 1024),  # KiB — 8 KiB (argon2's own floor) to 1 GiB
    'p': (1, 16),
}


def valid_kdf_params(params) -> dict | None:
    """params reduced to the three Argon2id work factors, or None if it is not a
    usable set. Booleans are rejected explicitly: True would otherwise pass as
    t=1."""
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


# Lets anything holding decrypted data drop it the moment the key changes.
_key_listeners = []


def on_key_change(callback):
    """Registers a callback fired on every unlock and every lock.

    A listener must be cheap and must not raise."""
    _key_listeners.append(callback)


def _notify_key_change():
    # One listener raising must not leave the rest holding decrypted data.
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
    # Fires on unlock too: a password change re-keys, so anything cached under
    # the old key is stale.
    _notify_key_change()


def release_key():
    """Releases the in-memory key. Called on lock. See the file header for why
    this is a release rather than a wipe."""
    global _key_instance
    _key_instance = None
    _notify_key_change()


def is_unlocked() -> bool:
    return _key_instance is not None


def field_aad(table: str, column: str) -> bytes:
    """Associated data binding a field's ciphertext to the column it lives in.

    Without it, a token moved from `notes` into `password` decrypts perfectly,
    and anyone who can write the database file can make the vault show — and
    copy to the clipboard — one field's contents as another's.

    Deliberately not bound to the row id: ids are reassigned by a restore, which
    would make every restored record unreadable."""
    return f'{table}.{column}'.encode()


def encrypt(value: str, aad: bytes = b'') -> str:
    """Encrypts a string under the session key. Returns
    base64url(nonce || ciphertext || tag)."""
    if _key_instance is None:
        raise VaultLockedError("Encryption key not initialized — app is locked")
    return encrypt_with(_key_instance, value.encode(), aad)


def _split_token(value: str) -> tuple[bytes, bytes]:
    """Splits a token into (nonce, ciphertext||tag), or raises InvalidTag.

    A truncated token has failed authentication as surely as a tampered one, so
    it raises the same exception callers already handle."""
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
    """The AES-GCM cipher for a raw 32-byte key. Build one and reuse it across a
    re-key rather than one per field."""
    return AESGCM(key)


def _as_cipher(key) -> AESGCM:
    return key if isinstance(key, AESGCM) else AESGCM(key)


def encrypt_with(key, plaintext: bytes, aad: bytes = b'') -> str:
    """Encrypts raw bytes under an explicit key or cipher, in the same envelope
    encrypt() produces.

    Every AES-GCM envelope in the app is built here, so nonces are chosen in one
    place. Empty aad is byte-identical to no aad, which keeps tokens written
    before field binding readable."""
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

    The two associated-data values differ only while a vault moves onto a format
    that binds fields differently (see auth._rekey_fields). Neither defaults to
    the other: a re-key that reads or writes the wrong binding is unrecoverable."""
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

    The lenient decoder discards characters outside the alphabet, so long prose
    could decode to something token-sized and be mistaken for ciphertext — then
    skipped by the plaintext migration and left unencrypted."""
    return base64.b64decode(value, altchars=b'-_', validate=True)


def looks_encrypted(value) -> bool:
    """True when value has the shape encrypt() produces.

    Separates "never encrypted" from "encrypted and broken", which a failed
    decrypt alone cannot. Real ciphertext always passes, so nothing is ever
    wrapped a second time."""
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
    UNREADABLE, so one corrupt field degrades to a placeholder. A locked vault
    re-raises, so callers cannot stamp placeholders over a vault that is merely
    closed."""
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
    plaintext: wrapping the token would lose the real value for good."""
    if not looks_encrypted(value):
        return encrypt_with(new_key, value.encode(), new_aad)
    return reencrypt(value, new_key, aad, new_aad)

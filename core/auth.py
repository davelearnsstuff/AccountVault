# -----------------------------------------------------------------------------
# core/auth.py
# Master password verification, login rate limiting, and lock state.
# Coordinates with encryption.py to install and release the AES-256-GCM key.
#
# Rate limiting is enforced inside verify_master_password and
# change_master_password rather than by their callers, so a new caller cannot
# omit it.
# -----------------------------------------------------------------------------

import json
import time
import sqlite3
from argon2 import PasswordHasher
from core import categories
from core import preferences
from core.database import ENCRYPTED_FIELDS, connect, read_config, vacuum, write_config
from core.encryption import (
    ARGON2_MEMORY_COST, ARGON2_PARALLELISM, ARGON2_TIME_COST, LEGACY_KDF_PARAMS,
    VaultLockedError, cipher_for, current_kdf_params, decrypt, encrypt_with,
    field_aad, generate_salt, derive_key, install_key, release_key, is_unlocked,
    looks_encrypted, on_key_change, safe_reencrypt, encrypt, valid_kdf_params,
)

# Session state — mutable so the settings dialog can update it at runtime.
_lock_timeout_seconds = 900  # 15 minutes default

MIN_PASSWORD_LENGTH = 15

# -----------------------------------------------------------------------------
# Vault crypto format
#
# How the vault's fields are encrypted, as opposed to the key they are under:
#
#   0  no associated data on any field, and the Argon2id work factors implied
#      by whatever encryption.py said at the time.
#   1  every field bound to its column via encryption.field_aad, the work
#      factors recorded in app_config, and a key-check token stored so a key
#      that does not open this vault is caught at the door.
#
# migrate_vault_format moves a vault forward at login, the one moment the
# plaintext password is in hand. It runs before any field is read, and a login
# it cannot finish is a login that fails.
VAULT_FORMAT = 1

_FORMAT_KEY = 'vault_format'
_KDF_KEY    = 'kdf_params'
_CANARY_KEY = 'key_check'

# A known plaintext stored encrypted under the session key and checked at every
# unlock. The master hash and the salt are separate rows: if they fall out of
# step (a half-restored backup, a forged master_hash) the password verifies but
# the derived key opens nothing, every field reads as unreadable, and the first
# save writes that placeholder over the real value. This makes that failure loud
# instead of destructive.
_CANARY_TEXT = 'accountvault key check'
_CANARY_AAD  = b'app_config.key_check'

_MISMATCHED_KEY_MESSAGE = (
    "This vault's password and encryption salt do not belong to each other, so "
    "the key derived from them opens nothing. Restore from a backup."
)

# Slows guessing at the keyboard. It is not what makes brute force infeasible:
# every attempt runs an Argon2 verify first.
#
# The failure count and the current lockout live on disk, so quitting the app
# resets neither.
_lockout_count         = 0   # escalates backoff: 30s, 60s, 120s … up to the cap
_MAX_ATTEMPTS          = 5
_LOCKOUT_SECONDS       = 30
# Five minutes already defeats a dictionary run by someone briefly alone with
# the machine; longer mostly punishes the owner for mistyping.
_LOCKOUT_CAP_SECONDS   = 300

# Monotonic so no clock change escapes or extends a lockout within one run. The
# persisted copy is wall-clock and clamped instead.
_lockout_deadline_monotonic = 0.0

_last_activity = None

# Held at the same cost as the key derivation: a stolen database exposes both,
# and either recovers the password.
ph = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST,
    parallelism=ARGON2_PARALLELISM,
)


class LoginRateLimited(RuntimeError):
    """Raised when a password attempt arrives during a lockout."""

    def __init__(self, seconds_remaining: int):
        self.seconds_remaining = seconds_remaining
        super().__init__(
            f"Too many failed attempts. Try again in {seconds_remaining} seconds."
        )


class VaultConfigError(RuntimeError):
    """Raised when the vault's stored key material cannot produce a working key.

    Distinct from a wrong password: reporting it as one would leave the user
    retyping a correct password forever."""


class VaultLocked(VaultLockedError):
    """Raised when an operation requiring an unlocked vault is attempted while
    locked. Subclasses VaultLockedError so the UI handles an idle timeout and an
    absent key the same way."""


def is_first_launch() -> bool:
    """True if no master password has been set yet."""
    return read_config('master_hash') is None


# -----------------------------------------------------------------------------
# Vault crypto format: reading it, checking the key against it, moving it on
# -----------------------------------------------------------------------------

def read_vault_crypto(db=None) -> tuple[int, dict]:
    """The format the stored fields are written in, and the work factors their
    key is derived under.

    A missing or unreadable value falls back to the format-0 answer, because a
    vault that recorded neither is an old one. A wrong guess is safe: the key
    opens nothing and the key check refuses the unlock before anything is
    written."""
    try:
        format_version = int(read_config(_FORMAT_KEY, db) or 0)
    except (TypeError, ValueError):
        format_version = 0

    params = None
    try:
        stored = read_config(_KDF_KEY, db)
        if stored:
            params = valid_kdf_params(json.loads(stored))
    except (sqlite3.Error, ValueError, TypeError):
        params = None

    return format_version, params or dict(LEGACY_KDF_PARAMS)


def _stored_aad(table: str, column: str, format_version: int) -> bytes:
    """The associated data a stored field carries at this format. Format 0
    predates field binding and carries none."""
    return field_aad(table, column) if format_version >= 1 else b''


def _key_check_token(cipher) -> str:
    """The check token for a key, rewritten whenever key material changes."""
    return encrypt_with(cipher, _CANARY_TEXT.encode(), _CANARY_AAD)


def require_working_key(format_version: int):
    """Raises VaultConfigError unless the installed session key opens this vault.

    Does not release the key itself; verify_master_password releases it for
    every way the login can fail.

    A format-0 vault has no token yet: migrate_vault_format is about to re-read
    every field, which is a stronger check, and writes the token afterwards."""
    stored = read_config(_CANARY_KEY)
    if stored is None:
        if format_version < VAULT_FORMAT:
            return
        raise VaultConfigError(
            "This vault's key check is missing, so there is no way to confirm "
            "that the password opens it. Restore from a backup."
        )

    try:
        opened = decrypt(stored, _CANARY_AAD) == _CANARY_TEXT
    except Exception:
        opened = False
    if not opened:
        raise VaultConfigError(_MISMATCHED_KEY_MESSAGE)


def _rekey_fields(db, new_cipher, from_format: int, *,
                  skip_unreadable: bool, consequence: str = '') -> tuple[int, int]:
    """Re-encrypts every field in ENCRYPTED_FIELDS under new_cipher, reading them
    as a vault at from_format stores them.

    Returns (ciphertext seen, ciphertext opened), counting only fields that were
    already encrypted — legacy plaintext re-encrypts under any key at all, so it
    says nothing about whether the key is right.

    skip_unreadable is the difference between the two callers. The login-time
    upgrade steps over a field that will not decrypt (it is already lost) rather
    than shutting the owner out of the whole vault. A password rotation refuses,
    raising ReencryptionError: it can wait until the field is repaired, and a
    vault half under each password has no password that opens all of it."""
    seen = opened = 0
    for table, fields in ENCRYPTED_FIELDS.items():
        for row in _encrypted_rows(db, table, fields):
            updates = {}
            for field in fields:
                value = row[field]
                if not value:
                    continue
                was_encrypted = looks_encrypted(value)
                if was_encrypted:
                    seen += 1
                try:
                    updates[field] = safe_reencrypt(
                        value, new_cipher,
                        aad=_stored_aad(table, field, from_format),
                        new_aad=field_aad(table, field),
                    )
                except VaultLockedError:
                    # The vault closed underneath the re-key; nothing is corrupt.
                    raise
                except Exception as exc:
                    if skip_unreadable:
                        continue
                    raise ReencryptionError(
                        f"A stored field in '{table}' (row {row['id']}) could not be "
                        f"decrypted, so {consequence} and your vault is unchanged. "
                        f"Restore from a backup, or delete the affected entry, then "
                        f"try again."
                    ) from exc
                if was_encrypted:
                    opened += 1
            _write_field_updates(db, table, row['id'], updates)
    return seen, opened


def _write_crypto_config(db, key_check: str, salt: bytes, params: dict, extra=()):
    """Records the key material and format a re-key has just written the vault
    under, plus any config rows that must commit alongside it.

    Every write is checked: an upsert that silently stored nothing would commit
    fields re-encrypted under a salt that was never saved."""
    rows = (
        ('salt',      salt.hex()),
        (_KDF_KEY,    json.dumps(params, sort_keys=True)),
        (_FORMAT_KEY, str(VAULT_FORMAT)),
        (_CANARY_KEY, key_check),
        *extra,
    )
    for key, value in rows:
        if write_config(key, value, db) != 1:
            raise ReencryptionError(
                f"The new {key} could not be stored, so the vault is unchanged."
            )


def _rekeyed_config(new_cipher) -> tuple:
    """The encrypted app_config rows outside ENCRYPTED_FIELDS, re-issued under
    new_cipher for a re-key to commit in the same transaction.

    Read under the session key, so this runs before install_key swaps it. Left
    out of the transaction, these rows would stay under a key nothing can derive
    any more: the warning settings would read as the defaults, and a stale
    backup seal would turn the user's automatic backups off and report them as
    tampered with."""
    rows = (preferences.rekeyed_warning_settings(new_cipher),
            preferences.resealed_backup_config(new_cipher))
    return tuple(row for row in rows if row)


def migrate_vault_format(password: str) -> bool:
    """Brings the vault onto VAULT_FORMAT, re-keyed under the current work
    factors. Returns whether anything was rewritten.

    A field that will not decrypt is left as it is rather than failing the
    login. A vault where *nothing* decrypts is the exception: that is the wrong
    key, and rewriting under it would make a recoverable vault unrecoverable."""
    format_version, params = read_vault_crypto()
    target = current_kdf_params()
    if format_version == VAULT_FORMAT and params == target:
        return False

    new_salt = generate_salt()
    new_cipher = cipher_for(derive_key(password, new_salt, target))
    rekeyed = _rekeyed_config(new_cipher)

    with connect() as db:
        seen, opened = _rekey_fields(db, new_cipher, format_version,
                                     skip_unreadable=True)
        if seen and not opened:
            raise VaultConfigError(_MISMATCHED_KEY_MESSAGE)
        _write_crypto_config(db, _key_check_token(new_cipher), new_salt, target,
                             extra=rekeyed)

    install_key(password, new_salt, target)

    # secure_delete only scrubs pages freed after it is turned on, so whatever
    # this vault freed before — deleted accounts, the plaintext the legacy
    # migration replaced — is still in the freelist. VACUUM rewrites the file
    # from live pages only, once, as the vault crosses onto the current format.
    vacuum()
    return True


def setup_master_password(password: str):
    """Called on first launch only. Hashes the master password, generates a
    salt, stores both, and installs the key.

    Raises ValueError, before anything is written, for a password that does not
    clear PASSWORD_RULES."""
    require_password_strength(password)
    password = password.strip()
    salt = generate_salt()
    params = current_kdf_params()
    password_hash = ph.hash(password)

    # Before the config write, so the key check can be built with encrypt().
    install_key(password, salt, params)

    with connect() as db:
        write_config('master_hash', password_hash, db)
        _write_crypto_config(db, encrypt(_CANARY_TEXT, _CANARY_AAD), salt, params)

    apply_saved_settings()
    _migrate_plaintext_fields_quietly()
    # Clears any lockout left in app_config by an earlier vault at this path.
    record_successful_login()
    reset_activity_timer()


def verify_master_password(password: str) -> bool:
    """Verifies the entered password against the stored hash and, if correct,
    installs the key and starts the session timer.

    Enforces the login rate limit itself, raising LoginRateLimited, and records
    the outcome."""
    allowed, wait_seconds = check_login_rate_limit()
    if not allowed:
        raise LoginRateLimited(wait_seconds)

    password = password.strip()

    with connect() as db:
        stored_hash = read_config('master_hash', db)
        if stored_hash is None:
            # Answering in microseconds where a real attempt takes a second would
            # reveal to anyone timing it that this database holds no vault.
            _burn_verification_time(password)
            record_failed_login()
            return False

        stored_salt = read_config('salt', db)
    if stored_salt is None:
        raise VaultConfigError(
            "This vault has a master password recorded but no encryption salt, "
            "so it cannot be opened. Restore from a backup."
        )
    try:
        salt = bytes.fromhex(stored_salt)
    except ValueError as exc:
        raise VaultConfigError(
            "This vault's encryption salt is corrupt, so it cannot be opened. "
            "Restore from a backup."
        ) from exc

    try:
        ph.verify(stored_hash, password)
    except Exception:
        record_failed_login()
        return False

    record_successful_login()
    _upgrade_stored_hash(stored_hash, password)

    # The stored work factors, not today's: they are what the existing fields
    # were derived under. migrate_vault_format moves them on afterwards.
    format_version, params = read_vault_crypto()
    install_key(password, salt, params)
    try:
        require_working_key(format_version)
        migrate_vault_format(password)
    except Exception as exc:
        # The key goes, whatever went wrong. This build addresses every field
        # with its column binding, so a vault left at an older format would read
        # as entirely unreadable — and the first save would write that back.
        release_key()
        if isinstance(exc, VaultConfigError):
            raise
        raise VaultConfigError(str(exc)) from exc

    apply_saved_settings()
    _migrate_plaintext_fields_quietly()
    reset_activity_timer()
    return True


def _burn_verification_time(password: str):
    """Spends roughly what a real verification spends, so a path that has
    already decided to fail takes as long as the others."""
    try:
        ph.hash(password)
    except Exception:
        pass


def _upgrade_stored_hash(stored_hash: str, password: str):
    """Rewrites a master hash stored under weaker Argon2 parameters. A
    successful login is the only moment the password is in hand to re-hash it.
    Never blocks a valid login."""
    try:
        if not ph.check_needs_rehash(stored_hash):
            return
        write_config('master_hash', ph.hash(password))
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Lock state and the idle timer
# -----------------------------------------------------------------------------

def reset_activity_timer():
    global _last_activity
    _last_activity = time.time()


def record_activity():
    """Records user activity to hold off the idle lock. No-op while locked."""
    if is_unlocked():
        reset_activity_timer()


def seconds_until_lock() -> float:
    """Seconds of idle time remaining before the vault auto-locks.
    Returns 0 when locked or already past the timeout."""
    if not is_unlocked() or _last_activity is None:
        return 0.0
    return max(0.0, _lock_timeout_seconds - (time.time() - _last_activity))


def check_session() -> bool:
    """True if the vault is unlocked and within the idle window.
    Locks the vault as a side effect once the timeout is exceeded."""
    if not is_unlocked() or _last_activity is None:
        return False

    if time.time() - _last_activity > _lock_timeout_seconds:
        lock_app()
        return False

    return True


def lock_app():
    """Releases the session key. Every vault operation fails until the master
    password is entered again."""
    global _last_activity
    release_key()
    _last_activity = None


def require_session():
    """Raises VaultLocked if the vault is locked. Call before any vault operation."""
    if not check_session():
        raise VaultLocked("Vault is locked — please log in")


# -----------------------------------------------------------------------------
# Login rate limiting
#
# Stored as a start time plus a duration rather than an absolute deadline, which
# the system clock defeats in both directions.
#
# Unencrypted, because it has to be legible while the vault is locked — so
# anyone who can write the database file can clear it between guesses. That
# cannot be closed: a counter the vault cannot read while locked cannot
# rate-limit the unlock. The Argon2 verify is what actually bounds guessing.
# -----------------------------------------------------------------------------

_LOCKOUT_STARTED_KEY  = 'lockout_started'
_LOCKOUT_DURATION_KEY = 'lockout_duration'
_FAILED_ATTEMPTS_KEY  = 'failed_login_attempts'


def _read_float(key: str) -> float:
    """A stored float, or 0.0. Fails open: a database this cannot read is one the
    login cannot happen against either."""
    try:
        value = read_config(key)
        return float(value) if value is not None else 0.0
    except (sqlite3.Error, ValueError, TypeError):
        return 0.0


def _read_failed_attempts() -> int:
    return int(_read_float(_FAILED_ATTEMPTS_KEY))


def _write_lockout(started: float, duration: float, attempts: int):
    """Persists the lockout and the consecutive-failure count together. A failed
    write costs only survival across a restart; the monotonic deadline still
    holds the limit for this run."""
    try:
        with connect() as db:
            for key, value in ((_LOCKOUT_STARTED_KEY, started),
                               (_LOCKOUT_DURATION_KEY, duration),
                               (_FAILED_ATTEMPTS_KEY, attempts)):
                write_config(key, str(value), db)
    except sqlite3.Error:
        pass


def _persisted_seconds_remaining() -> float:
    """Seconds left on the stored lockout, clamped to the cap so a backward
    clock can impose no more than the cap. A forward clock still expires it
    early; within a session the monotonic deadline covers that."""
    started = _read_float(_LOCKOUT_STARTED_KEY)
    duration = _read_float(_LOCKOUT_DURATION_KEY)
    if started <= 0 or duration <= 0:
        return 0.0
    elapsed = time.time() - started
    if elapsed < 0:
        # Clock moved backwards; treat the lockout as having just begun.
        elapsed = 0.0
    return max(0.0, min(duration - elapsed, _LOCKOUT_CAP_SECONDS))


def check_login_rate_limit() -> tuple[bool, int]:
    """Returns (allowed, seconds_to_wait). Locks out after 5 consecutive failures.

    The escalation counter is deliberately in memory only, so a restart drops
    the *next* lockout back to 30 seconds — bounding how long a mistyped
    password can lock the owner out."""
    monotonic_left = max(0.0, _lockout_deadline_monotonic - time.monotonic())
    remaining = max(monotonic_left, _persisted_seconds_remaining())
    if remaining > 0:
        return False, int(remaining) + 1
    return True, 0


def record_failed_login():
    global _lockout_deadline_monotonic, _lockout_count
    attempts = _read_failed_attempts() + 1
    if attempts < _MAX_ATTEMPTS:
        _write_lockout(_read_float(_LOCKOUT_STARTED_KEY),
                       _read_float(_LOCKOUT_DURATION_KEY), attempts)
        return

    _lockout_count += 1
    duration = min(_LOCKOUT_SECONDS * (2 ** (_lockout_count - 1)), _LOCKOUT_CAP_SECONDS)
    _lockout_deadline_monotonic = time.monotonic() + duration
    _write_lockout(time.time(), duration, 0)


def record_successful_login():
    reset_rate_limit_memory()
    _write_lockout(0.0, 0.0, 0)


def reset_rate_limit_memory():
    """Forgets the in-memory half of the rate limiter: the monotonic deadline and
    the escalation count. The persisted half belongs to the vault file.

    Called when the open vault changes, so one vault's failed logins do not lock
    the user out of another."""
    global _lockout_deadline_monotonic, _lockout_count
    _lockout_deadline_monotonic = 0.0
    _lockout_count = 0


# -----------------------------------------------------------------------------
# Walking the encrypted columns
# -----------------------------------------------------------------------------

def _encrypted_rows(db, table: str, fields: list) -> list:
    """Every row of table, with its id and its encrypted columns."""
    col_list = ', '.join(['id'] + fields)
    return db.execute(f"SELECT {col_list} FROM {table}").fetchall()


def _write_field_updates(db, table: str, row_id, updates: dict):
    """Writes back only the columns that changed. Table and column names come
    from ENCRYPTED_FIELDS, never from user input; values are bound."""
    if not updates:
        return
    set_clause = ', '.join(f"{f} = ?" for f in updates)
    db.execute(
        f"UPDATE {table} SET {set_clause} WHERE id = ?",
        (*updates.values(), row_id),
    )


# -----------------------------------------------------------------------------
# Legacy plaintext migration
# -----------------------------------------------------------------------------

def migrate_plaintext_fields():
    """Encrypts any fields in ENCRYPTED_FIELDS still stored as plaintext.
    Idempotent, so safe to run on every login."""
    for table, fields in ENCRYPTED_FIELDS.items():
        # A connection per table, so one table that fails cannot roll back the
        # others. A failed table is retried on the next login.
        try:
            with connect() as db:
                for row in _encrypted_rows(db, table, fields):
                    updates = {}
                    for field in fields:
                        value = row[field]
                        if not value or looks_encrypted(value):
                            # The value's shape, not whether it decrypts:
                            # encrypting a broken field would wrap the token
                            # and destroy the original.
                            continue
                        updates[field] = encrypt(value, field_aad(table, field))

                    _write_field_updates(db, table, row['id'], updates)
        except sqlite3.Error:
            continue


def _migrate_plaintext_fields_quietly():
    """migrate_plaintext_fields, unable to fail a login that has already
    succeeded. A locked vault still re-raises: that is a fault in the caller."""
    try:
        migrate_plaintext_fields()
    except VaultLockedError:
        raise
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Session lock timeout
#
# Lives here rather than in preferences.py: once loaded it is the policy
# check_session enforces.
# -----------------------------------------------------------------------------

def set_lock_timeout_seconds(value: int):
    """Sets the idle timeout, never below a minute — a zero timeout would lock
    the vault on the first tick after it opens."""
    global _lock_timeout_seconds
    _lock_timeout_seconds = max(60, value)


def apply_saved_settings():
    """Puts the saved lock timeout and warning settings in force. Called after
    login, and by the UI after preferences.save_settings or
    save_warning_settings, which persist but do not apply."""
    set_lock_timeout_seconds(preferences.get_settings()['lock_timeout_minutes'] * 60)
    if is_unlocked():
        categories.set_warning_settings(preferences.get_warning_settings())


def _drop_warning_settings():
    """Back to the default warnings on lock. They name the vault's custom
    categories, and the next vault opened must not inherit them."""
    if not is_unlocked():
        categories.set_warning_settings(None)


on_key_change(_drop_warning_settings)


# -----------------------------------------------------------------------------
# Password strength and rotation
# -----------------------------------------------------------------------------

# Every rule a password being set has to clear, as (caption, test, error).
# ui.widgets builds the live checklist from this table, so the screen and the
# submit-time check cannot disagree. The caption describes a good password; the
# error describes what is wrong with this one.
PASSWORD_RULES = (
    (f"At least {MIN_PASSWORD_LENGTH} characters",
     lambda password: len(password.strip()) >= MIN_PASSWORD_LENGTH,
     f"password must be at least {MIN_PASSWORD_LENGTH} characters"),

    # Refused rather than trimmed. The password is stripped where it is checked
    # (rescuing a paste with a trailing newline), so a password set with a
    # trailing space would not be the one the user believes opens the vault.
    ("No spaces at the start or end",
     lambda password: password == password.strip(),
     "password must not begin or end with a space"),
)


def validate_password_strength(password: str) -> list:
    """Returns a list of unmet requirement strings. Empty means the password passes.

    Only for a password being *set*: a password chosen before a rule existed
    still has to open the vault."""
    return [error for _, met, error in PASSWORD_RULES if not met(password)]


def require_password_strength(password: str, noun: str = 'Master'):
    """Raises ValueError unless password clears every rule. Called by the
    functions that set the master password rather than by the dialogs, so a new
    caller cannot skip it."""
    errors = validate_password_strength(password)
    if errors:
        raise ValueError(f"{noun} " + ', '.join(errors) + '.')


class ReencryptionError(RuntimeError):
    """Raised when a field cannot be re-encrypted, so the rotation was abandoned.
    Rotation is all-or-nothing: a vault half under each key has no password that
    opens all of it."""


def change_master_password(current_password: str, new_password: str) -> bool:
    """Verifies the current password, then re-encrypts every encrypted field
    under the new one and stores the new hash and salt. The in-memory key is
    swapped only after the database commit succeeds. Returns False if the
    current password is wrong.

    Raises ReencryptionError, leaving the vault untouched, if any field will
    not decrypt."""
    allowed, wait_seconds = check_login_rate_limit()
    if not allowed:
        raise LoginRateLimited(wait_seconds)

    # Before the strip below — the space rule checks what the user typed — and
    # before the verify, so a password that will be refused costs neither a
    # second of Argon2 nor a failed-login record.
    require_password_strength(new_password, 'New')

    current_password = current_password.strip()
    new_password = new_password.strip()

    # The old key is what reads the existing fields.
    if not is_unlocked():
        raise VaultLocked("Vault is locked — please log in")

    stored_hash = read_config('master_hash')
    if stored_hash is None:
        record_failed_login()
        return False

    try:
        ph.verify(stored_hash, current_password)
    except Exception:
        # A second door onto the master password; unmetered, it would be the
        # cheaper one to guess at.
        record_failed_login()
        return False

    record_successful_login()

    # Read rather than assumed: assuming the current format is what would
    # silently re-key a vault out of every password's reach.
    format_version, _ = read_vault_crypto()

    new_salt = generate_salt()
    params = current_kdf_params()
    new_cipher = cipher_for(derive_key(new_password, new_salt, params))
    # Outside the write transaction: a second of CPU with nothing gained by
    # holding a write lock across it.
    new_hash = ph.hash(new_password)
    rekeyed = _rekeyed_config(new_cipher)

    with connect() as db:
        _rekey_fields(db, new_cipher, format_version, skip_unreadable=False,
                      consequence='the password was not changed')
        _write_crypto_config(db, _key_check_token(new_cipher), new_salt, params,
                             extra=(('master_hash', new_hash), *rekeyed))

    install_key(new_password, new_salt, params)
    reset_activity_timer()
    return True

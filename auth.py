# -----------------------------------------------------------------------------
# auth.py
# Master password verification, login rate limiting, and lock state.
# Coordinates with encryption.py to initialize and clear the AES-256-GCM key.
#
# The vault runs in-process, so there is no HTTP surface to defend: no session
# tokens, origin checks or shutdown token. What protects the data at rest is
# Argon2id key derivation, the in-memory-only key, the idle lock, and login
# rate limiting.
#
# Rate limiting is enforced inside verify_master_password and
# change_master_password rather than by their callers, so it cannot be omitted
# by a new one.
# -----------------------------------------------------------------------------

import json
import time
import sqlite3
from argon2 import PasswordHasher
import preferences
from database import ENCRYPTED_FIELDS, connect, read_config, vacuum, write_config
from encryption import (
    ARGON2_MEMORY_COST, ARGON2_PARALLELISM, ARGON2_TIME_COST, LEGACY_KDF_PARAMS,
    VaultLockedError, cipher_for, current_kdf_params, decrypt, encrypt_with,
    field_aad, generate_salt, derive_key, install_key, release_key, is_unlocked,
    looks_encrypted, safe_reencrypt, encrypt, valid_kdf_params,
)

# Session state — mutable so the settings dialog can update it at runtime.
_lock_timeout_seconds = 900  # 15 minutes default

MIN_PASSWORD_LENGTH = 15

# -----------------------------------------------------------------------------
# Vault crypto format
#
# The version of *how* the vault's fields are encrypted, as opposed to the key
# they are encrypted under. It exists so that the two things which used to be
# frozen for the life of a vault can move:
#
#   0  the original — no associated data on any field, and the Argon2id work
#      factors implied by whatever encryption.py happened to say at the time.
#   1  every field bound to its own column via encryption.field_aad, the work
#      factors recorded in app_config, and a key-check token stored so that a
#      key which does not open this vault is caught at the door.
#
# migrate_vault_format moves a vault forward at login, the one moment the
# plaintext password is in hand and a new key can be derived from it. Nothing
# ever reads a vault at more than one format: the migration runs before any
# field is read, and a login it cannot finish is a login that fails.
VAULT_FORMAT = 1

_FORMAT_KEY = 'vault_format'
_KDF_KEY    = 'kdf_params'
_CANARY_KEY = 'key_check'

# Stored encrypted under the session key, and read back at every unlock.
#
# The master hash and the encryption salt are separate rows, and verifying a
# password against one says nothing about whether the key derived from the other
# opens anything. Out of step — a half-restored backup, a hand-edited
# app_config, or someone who replaced master_hash with the hash of a password
# they know — the login succeeds and every field reads as "⚠ unreadable". The
# vault survives being looked at; the first *save* then writes that placeholder
# back over the real value, and it is gone. This token is what makes that
# failure loud and immediate instead of silent and destructive.
_CANARY_TEXT = 'accountvault key check'
_CANARY_AAD  = b'app_config.key_check'

# Said in two places — the key check at the door, and a re-key that finds it can
# read nothing — because they are the same discovery reached two ways.
_MISMATCHED_KEY_MESSAGE = (
    "This vault's password and encryption salt do not belong to each other, so "
    "the key derived from them opens nothing. Restore from a backup."
)

# Slows repeat typing and casual guessing at the keyboard. It is not what makes
# brute force infeasible: every attempt runs an Argon2 verify first, which caps
# guessing at roughly 30 a second regardless.
#
# The failure count and the current lockout both live on disk, so quitting the
# app resets neither.
_lockout_count         = 0   # escalates backoff: 30s, 60s, 120s … up to the cap
_MAX_ATTEMPTS          = 5
_LOCKOUT_SECONDS       = 30
# Minutes, not hours: five minutes already defeats a dictionary run by someone
# briefly alone with the machine, and a longer wall mostly punishes the owner
# for mistyping — inescapably so, now that lockouts survive a restart.
_LOCKOUT_CAP_SECONDS   = 300

# Monotonic so no clock change escapes or extends a lockout within one run.
# It cannot be persisted — the epoch differs between boots — so the stored copy
# is wall-clock and clamped instead.
_lockout_deadline_monotonic = 0.0

_last_activity = None

# Held at the same cost as the key derivation in encryption.py. A stolen
# database exposes both and either recovers the password, so an attacker works
# on whichever is cheaper; at the library defaults this hash was ~4x cheaper to
# attack than derive_key.
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
    """Raised when app_config holds a master hash but not the salt that goes
    with it, so no key can ever be derived.

    Distinct from a wrong password: reporting this as one would leave the user
    retyping a correct password forever, while is_first_launch still reports the
    vault as set up, so there is no way back to setup either."""


class VaultLocked(VaultLockedError):
    """Raised when an operation requiring an unlocked vault is attempted while locked.

    The inheritance is load-bearing: an idle timeout and an absent key both mean
    "lock the window, drop the decrypted data", and the UI handles both by
    catching VaultLockedError. Breaking the relationship would silently stop
    those handlers firing for the timeout case."""


def is_first_launch() -> bool:
    """True if no master password has been set yet."""
    return read_config('master_hash') is None


# -----------------------------------------------------------------------------
# Vault crypto format: reading it, checking the key against it, moving it on
# -----------------------------------------------------------------------------

def read_vault_crypto(db=None) -> tuple[int, dict]:
    """The format the stored fields are written in, and the work factors the key
    for them is derived under.

    A missing or unreadable value falls back to the format-0 answer rather than
    to today's, because a vault that recorded neither is by definition an old
    one. Guessing wrong is not dangerous: the key that comes out opens nothing,
    and the key check below refuses the unlock before anything is written."""
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
    """The associated data a stored field carries in a vault at this format.
    Format 0 predates field binding and carries none."""
    return field_aad(table, column) if format_version >= 1 else b''


def _key_check_token(cipher) -> str:
    """The check token for a key, rewritten whenever key material changes."""
    return encrypt_with(cipher, _CANARY_TEXT.encode(), _CANARY_AAD)


def require_working_key(format_version: int):
    """Raises VaultConfigError unless the installed session key opens this vault.

    Does not release the key itself — verify_master_password releases it for
    every way this stretch of the login can fail, so that "the key is gone if we
    did not get in" lives in one place rather than being remembered separately
    here.

    A format-0 vault has no token yet and gets no verdict here:
    migrate_vault_format is about to re-read every field it holds, which is a
    stronger check than this one, and writes the token once that succeeds."""
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

    Returns (ciphertext seen, ciphertext opened) — counting only fields that
    were already encrypted, because they are the only ones that say anything
    about whether the key is right. Legacy plaintext re-encrypts happily under
    any key at all, so counting it would make the caller's check meaningless in
    exactly the vaults most likely to fail it.

    skip_unreadable is the whole difference between the two callers. A field
    that will not decrypt is already lost, and leaving its bytes untouched
    changes nothing the user can see — so the login-time upgrade steps over it
    rather than shutting the owner out of an entire vault over one damaged row.
    A password rotation refuses instead, and should: it is optional, it can wait
    until the field is repaired, and a vault half under each password has no
    password that opens all of it.

    When it does refuse it raises ReencryptionError, which rolls the caller's
    transaction back."""
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
                    # Not a corrupt field: the vault closed underneath the
                    # re-key. Reported as corruption, it would send the user to
                    # restore a backup over data that is perfectly intact.
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
    under, plus any config rows that have to commit alongside it.

    Every write is checked. An upsert against a table that will not take it
    reports zero rows and raises nothing, which would commit a vault whose every
    field was re-encrypted under a salt that was never stored — unopenable with
    any password, and with no exception to roll it back."""
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


def migrate_vault_format(password: str) -> bool:
    """Brings the vault onto VAULT_FORMAT, re-keyed under the current work
    factors. Returns whether anything was rewritten.

    Runs at login, the only moment the plaintext password is in hand and a new
    key can be derived from it — the same window _upgrade_stored_hash uses to
    restate the verifier hash at its current cost.

    A field that will not decrypt is left exactly as it is rather than failing
    the login. It is already unreadable, so skipping it loses nothing, while
    refusing would shut the owner out of an entire vault over one damaged row.
    Rotation still refuses, and should: that one is optional and can wait until
    the field has been repaired or removed.

    A vault where *nothing* decrypts is the exception. That is not damage, it is
    the wrong key, and rewriting under it would turn a recoverable vault into an
    unrecoverable one."""
    format_version, params = read_vault_crypto()
    target = current_kdf_params()
    if format_version == VAULT_FORMAT and params == target:
        return False

    new_salt = generate_salt()
    new_cipher = cipher_for(derive_key(password, new_salt, target))

    with connect() as db:
        seen, opened = _rekey_fields(db, new_cipher, format_version,
                                     skip_unreadable=True)
        # Every encrypted field in the vault refused this key. That is not
        # damage spread across all of them, it is the wrong key, and carrying on
        # would overwrite the salt and orphan them for good. A vault holding no
        # ciphertext at all — new, or wholly plaintext — has nothing to check
        # and nothing to lose either way.
        if seen and not opened:
            raise VaultConfigError(_MISMATCHED_KEY_MESSAGE)
        _write_crypto_config(db, _key_check_token(new_cipher), new_salt, target)

    install_key(password, new_salt, target)

    # The one moment worth compacting the file. secure_delete scrubs pages as
    # they are freed, but it only does so from the connection that sets it —
    # which means every page this vault freed over its whole life before this
    # release is still sitting in the freelist, legible. Deleted accounts and
    # the plaintext the legacy migration replaced both end up there. VACUUM
    # rewrites the file from the live pages only, and this runs once per vault,
    # at exactly the point it crosses from "written without those protections"
    # to "written with them".
    vacuum()
    return True


def setup_master_password(password: str):
    """
    Called on first launch only.
    Hashes the master password, generates a unique salt, stores both,
    and initializes encryption.

    Raises ValueError, before anything is written, for a password that does not
    clear PASSWORD_RULES — see require_password_strength.
    """
    require_password_strength(password)
    password = password.strip()
    salt = generate_salt()
    params = current_kdf_params()
    password_hash = ph.hash(password)

    # Before the config write, so the key check below can be built with
    # encrypt() rather than by deriving the key a second time.
    install_key(password, salt, params)

    with connect() as db:
        write_config('master_hash', password_hash, db)
        _write_crypto_config(db, encrypt(_CANARY_TEXT, _CANARY_AAD), salt, params)

    apply_saved_settings()
    _migrate_plaintext_fields_quietly()
    # Clears any lockout left in app_config, which outlives the vault that
    # earned it.
    record_successful_login()
    reset_activity_timer()


def verify_master_password(password: str) -> bool:
    """
    Verifies the entered password against the stored hash.
    If correct, initializes encryption and starts the session timer.

    Enforces the login rate limit itself, raising LoginRateLimited, and records
    the outcome. Callers do not need to — and should not — do either.
    """
    allowed, wait_seconds = check_login_rate_limit()
    if not allowed:
        raise LoginRateLimited(wait_seconds)

    password = password.strip()

    with connect() as db:
        stored_hash = read_config('master_hash', db)
        if stored_hash is None:
            # Returning immediately would answer in microseconds where a real
            # attempt takes about a second, revealing to anyone timing it
            # whether this database holds a vault.
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
        # Verification failed — return False without revealing why
        record_failed_login()
        return False

    record_successful_login()
    _upgrade_stored_hash(stored_hash, password)

    # The stored work factors, not today's: they are what this vault's existing
    # fields were derived under, and migrate_vault_format is what moves it onto
    # today's afterwards.
    format_version, params = read_vault_crypto()
    install_key(password, salt, params)
    try:
        require_working_key(format_version)
        migrate_vault_format(password)
    except Exception as exc:
        # The key goes, whatever went wrong. A vault left at an older format
        # cannot be read by this build — every field is addressed with its
        # column binding now — so opening one anyway would show a full vault as
        # entirely unreadable, and the first save would write that back.
        release_key()
        if isinstance(exc, VaultConfigError):
            raise
        raise VaultConfigError(str(exc)) from exc

    apply_saved_settings()
    _migrate_plaintext_fields_quietly()
    reset_activity_timer()
    return True


def _burn_verification_time(password: str):
    """Spends roughly what a real verification spends, and discards the result,
    so a path that has already decided to fail takes as long as the others."""
    try:
        ph.hash(password)
    except Exception:
        pass


def _upgrade_stored_hash(stored_hash: str, password: str):
    """Rewrites a master hash that was stored under weaker Argon2 parameters.

    Argon2 stores its work factors inside the hash, so raising them leaves
    existing hashes verifiable but stuck at their old cost. A successful login is
    the only moment the plaintext password is in hand to re-hash it.

    Never blocks a valid login: a hash left at the old cost is exactly what the
    user had a moment ago."""
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
    """Releases the AES-256-GCM key. Every vault operation fails until the master
    password is entered again.

    Releases rather than wipes — see release_key in encryption.py. The running
    app can no longer decrypt anything; the key is not scrubbed from RAM."""
    global _last_activity
    release_key()
    _last_activity = None


def require_session():
    """Raises VaultLocked if the vault is locked. Call before any vault operation."""
    if not check_session():
        raise VaultLocked("Vault is locked — please log in")


# -----------------------------------------------------------------------------
# Login rate limiting
# -----------------------------------------------------------------------------

# Stored as a start time plus a duration rather than an absolute deadline. A
# deadline is defeated in both directions by the system clock, which is exactly
# what an attacker at the keyboard controls: moved forward it expires instantly,
# moved backward it imposes an arbitrary wait the owner cannot escape.
#
# Unencrypted, because it has to be legible while the vault is locked — which
# also means anyone who can write the database file can clear the lockout
# between guesses. That is not a gap this can close: a counter the vault cannot
# read while locked cannot rate-limit the unlock. It is the reason the Argon2
# verify above is the control that actually bounds guessing, at roughly thirty
# attempts a second whatever these rows say, and the reason this exists only to
# slow someone typing at the keyboard.
_LOCKOUT_STARTED_KEY  = 'lockout_started'
_LOCKOUT_DURATION_KEY = 'lockout_duration'
_FAILED_ATTEMPTS_KEY  = 'failed_login_attempts'


def _read_float(key: str) -> float:
    """A stored float, or 0.0. Fails open: a database this cannot read is one the
    login cannot happen against either, since the master hash is in the same
    table."""
    try:
        value = read_config(key)
        return float(value) if value is not None else 0.0
    except (sqlite3.Error, ValueError, TypeError):
        return 0.0


def _read_failed_attempts() -> int:
    return int(_read_float(_FAILED_ATTEMPTS_KEY))


def _write_lockout(started: float, duration: float, attempts: int):
    """Persists the lockout and the consecutive-failure count together.

    A failed write costs only survival across a restart, since
    _lockout_deadline_monotonic still holds the limit for this run. Silent
    because the vault is locked and the alternative is refusing to run."""
    try:
        with connect() as db:
            for key, value in ((_LOCKOUT_STARTED_KEY, started),
                               (_LOCKOUT_DURATION_KEY, duration),
                               (_FAILED_ATTEMPTS_KEY, attempts)):
                write_config(key, str(value), db)
    except sqlite3.Error:
        pass


def _persisted_seconds_remaining() -> float:
    """Seconds left on the stored lockout, clamped to the cap.

    The clamp is what makes a wall-clock copy safe: a backward clock can then do
    no worse than the five minutes the lockout could already impose. A forward
    clock still expires it early, which no persisted wall-clock value can
    prevent; within a session the monotonic deadline covers that."""
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

    Both the lockout and the failure count are persisted, so quitting the app
    clears neither.

    The escalation counter is deliberately left in memory, so a restart drops
    the *next* lockout back to 30 seconds. That bounds how far a mistyped
    password can lock the owner out while still denying free attempts. Takes the
    longer of the two remaining times, so the limit holds in-process even if the
    write failed."""
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
    global _lockout_deadline_monotonic, _lockout_count
    _lockout_deadline_monotonic = 0.0
    _lockout_count = 0
    _write_lockout(0.0, 0.0, 0)


# -----------------------------------------------------------------------------
# Walking the encrypted columns
#
# Shared by the legacy migration and the re-key in change_master_password. Only
# the SQL building is shared: the two differ on everything else, one taking a
# connection per table and swallowing failures, the other holding a single
# transaction and aborting the lot.
# -----------------------------------------------------------------------------

def _encrypted_rows(db, table: str, fields: list) -> list:
    """Every row of table, with its id and its encrypted columns."""
    col_list = ', '.join(['id'] + fields)
    return db.execute(f"SELECT {col_list} FROM {table}").fetchall()


def _write_field_updates(db, table: str, row_id, updates: dict):
    """Writes back only the columns that changed. A no-op when none did.

    Table and column names are interpolated because they come from
    ENCRYPTED_FIELDS in database.py, never from user input. Values are bound."""
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
    """
    Encrypts any fields in ENCRYPTED_FIELDS still stored as plaintext.
    Safe to run on every login — idempotent, skips already-encrypted values.
    Needed when upgrading from a version that encrypted fewer fields.
    """
    for table, fields in ENCRYPTED_FIELDS.items():
        # A connection per table, so one table that cannot be migrated cannot
        # roll back the tables that migrated cleanly. Idempotent, so a failed
        # table is retried on the next login.
        try:
            with connect() as db:
                for row in _encrypted_rows(db, table, fields):
                    updates = {}
                    for field in fields:
                        value = row[field]
                        if not value or looks_encrypted(value):
                            # Asks about the value's shape, not whether it
                            # decrypts: encrypting a broken field would wrap the
                            # token and destroy the original, silently and on
                            # every login.
                            continue
                        updates[field] = encrypt(value, field_aad(table, field))

                    _write_field_updates(db, table, row['id'], updates)
        except sqlite3.Error:
            # Covers the UPDATE as well as the SELECT, so a write failure cannot
            # propagate out of a login that has already succeeded.
            continue


def _migrate_plaintext_fields_quietly():
    """migrate_plaintext_fields, with the guarantee that it cannot fail a login.

    By the time this runs the user is already in, and no caller of
    verify_master_password expects an exception on the way out. A failed pass
    leaves the rows untouched and is retried next login.

    A locked vault re-raises: encrypt() cannot run without the key, and
    swallowing that would hide a fault in the caller rather than a stubborn
    row."""
    try:
        migrate_plaintext_fields()
    except VaultLockedError:
        raise
    except Exception:
        pass


# -----------------------------------------------------------------------------
# Session lock timeout
#
# Lives here rather than in preferences.py with the other settings: once loaded
# it is the session policy check_session enforces, beside the lock state it
# governs.
# -----------------------------------------------------------------------------

def set_lock_timeout_seconds(value: int):
    """Sets the idle timeout, never below a minute.

    preferences.get_settings validates what it reads, but any caller can set
    this directly, and a zero or negative timeout makes check_session lock the
    vault on the first tick after it opens."""
    global _lock_timeout_seconds
    _lock_timeout_seconds = max(60, value)


def apply_saved_settings():
    """Loads the saved settings and applies the lock timeout to this session.

    Called after login, and by the UI after preferences.save_settings, which
    persists but does not apply."""
    set_lock_timeout_seconds(preferences.get_settings()['lock_timeout_minutes'] * 60)


# -----------------------------------------------------------------------------
# Password strength and rotation
# -----------------------------------------------------------------------------

# Every rule a password being set has to clear, as (caption, test, error).
#
# One table rather than a chain of ifs because the UI needs the same rules in
# the other direction: the live checklist under a password field used to
# restate the length rule as its own lambda, in two dialogs, and showed nothing
# at all for the space rule. A user could therefore watch the checklist go
# green and still be refused on submit — which is exactly what happened when
# the space rule was added and the captions were not. ui.widgets builds the
# checklist from this table, so a rule added here shows up and fails with the
# same words in both places.
#
# The caption states what a *good* password looks like; the error states what
# is wrong with this one. They are worded separately on purpose — a checklist
# reading "password must be at least 15 characters" before anything is typed
# is an accusation rather than a hint.
PASSWORD_RULES = (
    (f"At least {MIN_PASSWORD_LENGTH} characters",
     lambda password: len(password.strip()) >= MIN_PASSWORD_LENGTH,
     f"password must be at least {MIN_PASSWORD_LENGTH} characters"),

    # Refused rather than trimmed. setup_master_password strips before hashing,
    # so the user would walk away believing in a password that is not the one
    # opening the vault, with no recovery once they find out.
    #
    # Stripping stays where a password is checked, which rescues a paste that
    # picked up a trailing newline.
    ("No spaces at the start or end",
     lambda password: password == password.strip(),
     "password must not begin or end with a space"),
)


def validate_password_strength(password: str) -> list:
    """Returns a list of unmet requirement strings. Empty means the password passes.

    Only ever applied to a password being *set*. Verifying an existing one must
    not run these rules — a password chosen before a rule existed still has to
    open the vault."""
    return [error for _, met, error in PASSWORD_RULES if not met(password)]


def require_password_strength(password: str, noun: str = 'Master'):
    """Raises ValueError unless password clears every rule. Returns nothing.

    Called by the two functions that *set* the master password rather than by
    the dialogs that collect it: this is the boundary where a weak password
    becomes a weak vault, and the rate limiter one screen up is enforced the
    same way and for the same reason. Both had validated only in the dialog,
    which made the password protecting everything the one rule a new caller
    could omit — and with backups now being copies of the vault, this password
    is the only one there is.

    Not applied to verification — see validate_password_strength."""
    errors = validate_password_strength(password)
    if errors:
        raise ValueError(f"{noun} " + ', '.join(errors) + '.')


class ReencryptionError(RuntimeError):
    """Raised when a field cannot be re-encrypted, so the rotation was abandoned.

    Rotation is all-or-nothing on purpose: a vault half under the old key and
    half under the new one has no password that opens all of it."""


def change_master_password(current_password: str, new_password: str) -> bool:
    """
    Verifies the current password, then re-encrypts every encrypted field
    under the new password and updates the stored hash and salt.
    The in-memory key is swapped only after the database commit succeeds.
    Returns False if the current password is wrong.

    Raises ReencryptionError, leaving the vault untouched, if any field will
    not decrypt. That field is already unreadable; carrying on would re-encrypt
    the unreadable token and lose the last copy of whatever it protected.
    """
    allowed, wait_seconds = check_login_rate_limit()
    if not allowed:
        raise LoginRateLimited(wait_seconds)

    # Before the strip below, not after: the space rule refuses a password with
    # a leading or trailing space rather than trimming it, and checking the
    # stripped copy is checking a password the user did not type — the rule
    # could never fail. Before the verify below too, so a new password that is
    # going to be refused costs neither a second of Argon2 nor a failed-login
    # record against the current one.
    require_password_strength(new_password, 'New')

    current_password = current_password.strip()
    new_password = new_password.strip()

    # The old key is what reads the existing fields, so without it the rotation
    # cannot start. Refusing here also saves a second of key derivation.
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
        # cheaper of the two to guess at.
        record_failed_login()
        return False

    record_successful_login()

    # The format the existing fields are stored in, which is what says how to
    # read them. Normally VAULT_FORMAT — the login this rotation follows brought
    # the vault up to date — but read rather than assumed, because assuming it
    # is what would silently re-key a vault out of every password's reach.
    format_version, _ = read_vault_crypto()

    # Derive new key material before touching the database
    new_salt = generate_salt()
    params = current_kdf_params()
    # One cipher for the whole rotation rather than one per field.
    new_cipher = cipher_for(derive_key(new_password, new_salt, params))
    # Outside the write transaction below: a second of CPU at these work
    # factors, with nothing gained by holding a write lock across it.
    new_hash = ph.hash(new_password)

    with connect() as db:
        _rekey_fields(db, new_cipher, format_version, skip_unreadable=False,
                      consequence='the password was not changed')
        _write_crypto_config(db, _key_check_token(new_cipher), new_salt, params,
                             extra=(('master_hash', new_hash),))

    # Swap the in-memory key now that the database is consistent
    install_key(new_password, new_salt, params)

    # The backup seal is a token under the session key, and the session key has
    # just changed. Renewed here rather than left to fail: a stale seal reads as
    # a tampered-with configuration, so without this, changing the master
    # password would turn the user's own backups off and accuse them of it.
    preferences.seal_backup_config()

    reset_activity_timer()
    return True

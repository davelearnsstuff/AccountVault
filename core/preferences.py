# -----------------------------------------------------------------------------
# core/preferences.py
# User settings and window view state for AccountVault.
#
# Nearly all of it is stored in app_config, unencrypted and readable while the
# vault is locked, because the window has to be built and themed before anyone
# logs in. Two exceptions: the backup rows are sealed, and the warning settings
# are encrypted — see their sections below. The vault's crypto rows
# (master_hash, salt, kdf_params, vault_format, key_check) belong to auth.py.
#
# Named "preferences" rather than "settings" because `settings` is the name of
# the dict this returns throughout the UI.
# -----------------------------------------------------------------------------

import json
import ntpath
import posixpath
import sqlite3

from core import categories
from core import encryption
from core.database import connect, read_config, write_config

# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

VALID_LOCK_TIMEOUTS     = (5, 10, 15, 30, 60)
VALID_CLIPBOARD_CLEARS  = (15, 30, 60, 120)
VALID_SHUTDOWN_TIMEOUTS = (30, 60, 120, 240, 0)
# Order drives the swatch row in Settings. Must stay in step with THEMES in
# ui/theme.py.
VALID_THEMES            = ('dark', 'light')
# Smallest first: Ctrl+= and Ctrl+- step along this order, and Settings lists
# it with these labels.
FONT_SIZE_CHOICES       = (('small', 'Small'), ('default', 'Default'),
                           ('large', 'Large'), ('xl', 'XL'))
VALID_FONT_SIZES        = tuple(key for key, _ in FONT_SIZE_CHOICES)
# 'compact' folds the balance, updated and due captions onto one line;
# 'comfortable' gives each its own.
VALID_LIST_DENSITIES    = ('compact', 'comfortable')

VALID_BACKUP_FREQUENCIES = ('daily', 'weekly', 'monthly')
# How long backups are kept, in months. 0 means forever.
VALID_BACKUP_RETENTION_MONTHS = (1, 3, 6, 12, 24, 0)

# Stored instead of a resolved path when the user has not chosen a folder, so
# "beside the vault" keeps meaning that after the vault is moved.
BACKUP_LOCATION_DEFAULT = 'default'

_SETTING_DEFAULTS = {
    'lock_timeout_minutes':        15,
    'clipboard_clear_seconds':     30,
    'shutdown_after_lock_minutes': 60,
    'theme':                       'dark',
    'font_size':                   'default',
    'list_density':                'compact',
    'auto_backup_enabled':         0,
    'backup_frequency':            'weekly',
    'backup_retention_months':     12,
    'backup_location':             BACKUP_LOCATION_DEFAULT,
}

_INT_SETTINGS = ('lock_timeout_minutes', 'clipboard_clear_seconds',
                 'shutdown_after_lock_minutes', 'auto_backup_enabled',
                 'backup_retention_months')


def get_settings() -> dict:
    """Reads all user settings from app_config, falling back to defaults."""
    settings = dict(_SETTING_DEFAULTS)
    try:
        with connect() as db:
            placeholders = ','.join('?' * len(_SETTING_DEFAULTS))
            rows = db.execute(
                f"SELECT key, value FROM app_config WHERE key IN ({placeholders})",
                tuple(_SETTING_DEFAULTS),
            ).fetchall()
        for key, value in rows:
            if key in _INT_SETTINGS:
                settings[key] = int(value)
            else:
                settings[key] = value
    except (sqlite3.Error, ValueError, TypeError):
        pass

    # A stored value outside the valid set would match nothing in the Settings
    # dialog and be refused by save_settings, so it falls back to the default.
    for key, valid in (('lock_timeout_minutes',        VALID_LOCK_TIMEOUTS),
                       ('clipboard_clear_seconds',     VALID_CLIPBOARD_CLEARS),
                       ('shutdown_after_lock_minutes', VALID_SHUTDOWN_TIMEOUTS),
                       ('auto_backup_enabled',         (0, 1)),
                       ('backup_frequency',            VALID_BACKUP_FREQUENCIES),
                       ('backup_retention_months',     VALID_BACKUP_RETENTION_MONTHS),
                       ('theme',                       VALID_THEMES),
                       ('font_size',                   VALID_FONT_SIZES),
                       ('list_density',                VALID_LIST_DENSITIES)):
        if settings[key] not in valid:
            settings[key] = _SETTING_DEFAULTS[key]

    # A blank path would resolve against whatever directory the app launched in.
    if not isinstance(settings['backup_location'], str) or not settings['backup_location'].strip():
        settings['backup_location'] = BACKUP_LOCATION_DEFAULT
    return settings


def save_settings(lock_timeout_minutes: int, clipboard_clear_seconds: int,
                  shutdown_after_lock_minutes: int, theme: str, font_size: str,
                  list_density: str = 'compact', auto_backup_enabled: int = 0,
                  backup_frequency: str = 'weekly',
                  backup_retention_months: int = 12,
                  backup_location: str = BACKUP_LOCATION_DEFAULT):
    """Validates and persists all settings.

    Persists only; callers follow with auth.apply_saved_settings to put the lock
    timeout in force (importing auth here would be a cycle)."""
    if lock_timeout_minutes not in VALID_LOCK_TIMEOUTS:
        raise ValueError("Invalid lock timeout value")
    if clipboard_clear_seconds not in VALID_CLIPBOARD_CLEARS:
        raise ValueError("Invalid clipboard clear value")
    if shutdown_after_lock_minutes not in VALID_SHUTDOWN_TIMEOUTS:
        raise ValueError("Invalid shutdown timeout value")
    if theme not in VALID_THEMES:
        raise ValueError("Invalid theme value")
    if font_size not in VALID_FONT_SIZES:
        raise ValueError("Invalid font size value")
    if list_density not in VALID_LIST_DENSITIES:
        raise ValueError("Invalid list density value")
    if int(auto_backup_enabled) not in (0, 1):
        raise ValueError("Invalid automatic backup value")
    if backup_frequency not in VALID_BACKUP_FREQUENCIES:
        raise ValueError("Invalid backup frequency value")
    if backup_retention_months not in VALID_BACKUP_RETENTION_MONTHS:
        raise ValueError("Invalid backup retention value")
    if not isinstance(backup_location, str) or not backup_location.strip():
        raise ValueError("Invalid backup location")

    pairs = [
        ('lock_timeout_minutes',        str(lock_timeout_minutes)),
        ('clipboard_clear_seconds',     str(clipboard_clear_seconds)),
        ('shutdown_after_lock_minutes', str(shutdown_after_lock_minutes)),
        ('theme',                       theme),
        ('font_size',                   font_size),
        ('list_density',                list_density),
        ('auto_backup_enabled',         str(int(auto_backup_enabled))),
        ('backup_frequency',            backup_frequency),
        ('backup_retention_months',     str(backup_retention_months)),
        ('backup_location',             backup_location.strip()),
    ]

    def write():
        with connect() as db:
            for key, value in pairs:
                write_config(key, value, db)

    # Four of those rows are backup instructions, so the seal covers them.
    _write_and_reseal(write)


# -----------------------------------------------------------------------------
# Backup bookkeeping
#
# Not settings: the backup run's own record of whether one is due, which files
# it wrote (and so may delete), and the outcome Settings shows. It lives in
# app_config, so it travels with the vault — services/backups.py checks the
# vault it is running in is the one this was written for before acting on it.
# -----------------------------------------------------------------------------

_BACKUP_STATE_KEYS = ('last_backup_date', 'last_backup_outcome', 'backup_files',
                      'backup_source')

# -----------------------------------------------------------------------------
# Sealing the backup configuration
#
# app_config is writable by anyone holding the vault *file*, no password
# needed, and the backup rows are instructions: where to copy the whole vault,
# and which files retention may delete. Unauthenticated, they would let someone
# who can write a shared or synced vault point the victim's next backup at a
# destination of their choosing.
#
# So they are sealed: a token over the exact bytes of every backup row,
# encrypted under the session key. The rows stay readable (a locked window still
# needs them), but only someone who can open the vault can produce a seal.
#
# Failure is closed: no valid seal means the configuration is reset to the
# defaults — automatic backups off — and resealed. Adopting what is there would
# defeat the control, since deleting the seal row is no harder than forging the
# rows it covers. For the same reason a write only re-seals rows that were
# already this vault's own; see _write_and_reseal.
# -----------------------------------------------------------------------------

_BACKUP_AUTH_KEY = 'backup_auth'

# Every row the backup run reads. A key missed here is one an attacker gets to
# write, so check this against _SETTING_DEFAULTS whenever either changes.
_AUTHENTICATED_BACKUP_KEYS = (
    'auto_backup_enabled', 'backup_frequency', 'backup_retention_months',
    'backup_location', 'backup_confirmed_dir', *_BACKUP_STATE_KEYS,
)


def _backup_aad() -> bytes:
    return encryption.field_aad('app_config', _BACKUP_AUTH_KEY)


def _backup_fingerprint() -> str:
    """Canonical text over every backup row. None and '' fold together: a row
    never written and one written empty mean the same thing here."""
    return json.dumps({key: (_read_config(key) or '')
                       for key in _AUTHENTICATED_BACKUP_KEYS},
                      sort_keys=True, separators=(',', ':'))


def seal_backup_config() -> bool:
    """Seals the backup rows as they now stand, unconditionally. False when the
    vault is locked.

    Only for rows the caller has already vouched for — check_seal after it has
    verified or reset them. Ordinary writes go through _write_and_reseal."""
    try:
        token = encryption.encrypt(_backup_fingerprint(), _backup_aad())
    except Exception:
        return False
    _write_config(_BACKUP_AUTH_KEY, token)
    return True


def backup_config_is_sealed() -> bool:
    """Whether the stored backup rows carry this vault's own seal."""
    stored = _read_config(_BACKUP_AUTH_KEY)
    if not stored:
        return False
    try:
        return encryption.decrypt(stored, _backup_aad()) == _backup_fingerprint()
    except Exception:
        return False


def backup_config_is_pristine() -> bool:
    """Whether every backup row is still at its default.

    A vault that has never configured a backup has nothing to lose to a reset,
    so it is sealed silently rather than reported as tampered with."""
    for key in _AUTHENTICATED_BACKUP_KEYS:
        stored = _read_config(key)
        if stored in (None, ''):
            continue
        if key in _SETTING_DEFAULTS and stored == str(_SETTING_DEFAULTS[key]):
            continue
        return False
    return True


def _write_and_reseal(write):
    """Runs write(), then re-seals the backup rows — but only if they were this
    vault's own before it ran.

    Re-sealing unconditionally would launder rows written behind the app's back
    (a vault on a shared drive edited while open): the next settings save would
    vouch for them. Left stale instead, the next check_seal resets them."""
    trusted = backup_config_is_sealed() or backup_config_is_pristine()
    write()
    if trusted:
        seal_backup_config()


def resealed_backup_config(cipher) -> tuple[str, str] | None:
    """The seal row re-issued under cipher, as a (key, value) pair for a re-key
    to commit alongside the fields it rewrites. None when the current rows do
    not carry a valid seal — a stale seal stays stale, so check_seal still
    catches whatever it was protecting against."""
    if not backup_config_is_sealed():
        return None
    token = encryption.encrypt_with(cipher, _backup_fingerprint().encode(), _backup_aad())
    return _BACKUP_AUTH_KEY, token


def reset_backup_config():
    """Puts the backup rows back to their defaults and seals them. The record of
    written files is emptied so retention has nothing it believes it may
    delete."""
    pairs = [(key, str(_SETTING_DEFAULTS[key]))
             for key in _AUTHENTICATED_BACKUP_KEYS if key in _SETTING_DEFAULTS]
    pairs += [(key, '') for key in _AUTHENTICATED_BACKUP_KEYS
              if key not in _SETTING_DEFAULTS]
    with connect() as db:
        for key, value in pairs:
            write_config(key, value, db)
    seal_backup_config()


def get_backup_state() -> dict:
    """The recorded backup history for this vault.

    `files` lists the backups this app created, each {'name', 'date'}. Pruning
    deletes nothing that is not on it. `source` is the vault path the record was
    written for; see services/backups.py for what a mismatch means."""
    state = {'last_backup_date': _read_config('last_backup_date') or '',
             'outcome': {}, 'files': [], 'source': _read_config('backup_source') or '',
             'confirmed_dir': _read_config('backup_confirmed_dir') or ''}

    raw_outcome = _read_config('last_backup_outcome')
    if raw_outcome:
        try:
            parsed = json.loads(raw_outcome)
            if isinstance(parsed, dict):
                state['outcome'] = parsed
        except ValueError:
            pass

    raw_files = _read_config('backup_files')
    if raw_files:
        try:
            parsed = json.loads(raw_files)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            state['files'] = [entry for entry in parsed
                              if isinstance(entry, dict)
                              and _is_bare_filename(entry.get('name'))
                              and isinstance(entry.get('date'), str)]
    return state


def _is_bare_filename(name) -> bool:
    """Whether a recorded backup name is a filename and only a filename.

    These names are handed to unlink(), and `Path(folder) / 'C:/…'` drops the
    folder entirely, so a name carrying a separator, a drive or '..' would let a
    forged row delete a file the app never wrote. ntpath as well as posixpath:
    a vault written on Windows can be opened on Linux, where a backslash is an
    ordinary filename character."""
    if not isinstance(name, str) or name in ('', '.', '..'):
        return False
    return (name == ntpath.basename(name) == posixpath.basename(name)
            and not ntpath.splitdrive(name)[0])


def save_backup_files(entries: list):
    _write_backup_config('backup_files', json.dumps(entries))


def save_backup_date(value: str):
    _write_backup_config('last_backup_date', value)


def save_backup_outcome(outcome: dict):
    _write_backup_config('last_backup_outcome', json.dumps(outcome))


def save_backup_source(path: str):
    _write_backup_config('backup_source', path)


def save_backup_confirmed_dir(path: str):
    """Records the folder the user has agreed backups may be written to. Sealed
    like the rest, so it says what the user approved rather than what the file
    claims they approved."""
    _write_backup_config('backup_confirmed_dir', path)


def _write_backup_config(key: str, value: str):
    _write_and_reseal(lambda: _write_config(key, value))


def clear_backup_state():
    """Forgets this vault's backup history without touching a file. Used when a
    vault turns out to be a copy of another: the record it inherited describes
    the original's backups."""
    def write():
        for key in _BACKUP_STATE_KEYS:
            _write_config(key, '')
    _write_and_reseal(write)


# -----------------------------------------------------------------------------
# Warning settings
#
# When due dates and last-updated stamps turn orange and red, per category —
# see "Warning settings" in categories.py for the shape.
#
# Stored encrypted: it is keyed by category name, and a custom category's name
# is vault content. It is only needed while the vault is open, so nothing is
# lost by the locked window being unable to read it. Encryption also
# authenticates it — a forged value fails to decrypt and reads as the defaults.
# -----------------------------------------------------------------------------

_WARNINGS_KEY = 'warning_settings'


def _warnings_aad() -> bytes:
    return encryption.field_aad('app_config', _WARNINGS_KEY)


def get_warning_settings() -> dict | None:
    """The saved warning settings, or None for the built-in defaults — including
    for a value that will not decrypt or validate."""
    stored = _read_config(_WARNINGS_KEY)
    if not stored:
        return None
    try:
        return categories.validate_warning_settings(
            json.loads(encryption.decrypt(stored, _warnings_aad())))
    except Exception:
        return None


def save_warning_settings(settings: dict):
    """Validates and stores the warning settings, encrypted.

    Raises ValueError for settings that do not validate and VaultLockedError
    when there is no key. Persists only; callers follow with
    auth.apply_saved_settings."""
    clean = categories.validate_warning_settings(settings)
    token = encryption.encrypt(json.dumps(clean, sort_keys=True), _warnings_aad())
    write_config(_WARNINGS_KEY, token)


def rekeyed_warning_settings(cipher) -> tuple[str, str] | None:
    """The warning settings row re-encrypted under cipher, as a (key, value)
    pair for a re-key to commit alongside the fields it rewrites. None when
    there is no row; emptied when it will not decrypt, since nothing could open
    it after the re-key anyway."""
    stored = _read_config(_WARNINGS_KEY)
    if not stored:
        return None
    aad = _warnings_aad()
    try:
        plain = encryption.decrypt(stored, aad)
    except Exception:
        return _WARNINGS_KEY, ''
    return _WARNINGS_KEY, encryption.encrypt_with(cipher, plain.encode(), aad)


# -----------------------------------------------------------------------------
# View state
#
# Stored per item rather than through save_settings, because these change as
# the window is sorted, resized and dragged. All cosmetic: every path here fails
# silently to "no stored value" rather than blocking a launch.
# -----------------------------------------------------------------------------

def _read_config(key: str) -> str | None:
    """database.read_config with database errors silenced."""
    try:
        return read_config(key)
    except sqlite3.Error:
        return None


def _write_config(key: str, value: str):
    """database.write_config with database errors silenced."""
    try:
        write_config(key, value)
    except sqlite3.Error:
        pass


VALID_SORT_KEYS = ('name', 'due_date', 'last_updated')

# Direction each sort starts in. A–Z and the nearest due date read ascending;
# "Last Updated" means most recent first.
SORT_DEFAULT_DESCENDING = {
    'name': False,
    'due_date': False,
    'last_updated': True,
}


def get_sort_key() -> str:
    """The account list's saved sort order."""
    value = _read_config('sort_key')
    return value if value in VALID_SORT_KEYS else VALID_SORT_KEYS[0]


def save_sort_key(value: str):
    if value in VALID_SORT_KEYS:
        _write_config('sort_key', value)


def get_sort_descending(sort_key: str) -> bool:
    """Whether the list is sorted descending, falling back to the natural
    direction for this key when nothing has been stored."""
    stored = _read_config('sort_descending')
    if stored in ('0', '1'):
        return stored == '1'
    return SORT_DEFAULT_DESCENDING.get(sort_key, False)


def save_sort_descending(descending: bool):
    _write_config('sort_descending', '1' if descending else '0')


# The sidebar's drag range, and the bounds a stored width must fall inside to be
# believed. Defined here because this module reads the value and cannot import
# from ui/.
SIDEBAR_MIN = 240
SIDEBAR_MAX = 560


def get_window_geometry() -> bytes | None:
    """The stored result of QMainWindow.saveGeometry(), or None. Qt's own blob
    carries the screen and maximised state, and restoreGeometry refuses one
    saved on a monitor that is no longer attached."""
    stored = _read_config('window_geometry')
    if not stored:
        return None
    try:
        return bytes.fromhex(stored)
    except ValueError:
        return None


def save_window_geometry(blob: bytes):
    _write_config('window_geometry', bytes(blob).hex())


def get_sidebar_width() -> int | None:
    """The width the user last dragged the sidebar to, or None if they never
    have."""
    stored = _read_config('sidebar_width')
    try:
        width = int(stored)
    except (TypeError, ValueError):
        return None
    if not SIDEBAR_MIN <= width <= SIDEBAR_MAX:
        return None
    return width


def save_sidebar_width(width: int):
    if SIDEBAR_MIN <= width <= SIDEBAR_MAX:
        _write_config('sidebar_width', str(int(width)))

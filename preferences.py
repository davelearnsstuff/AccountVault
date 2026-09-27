# -----------------------------------------------------------------------------
# preferences.py
# User settings and window view state for AccountVault.
#
# All of it is stored in app_config, unencrypted and readable while the vault is
# locked, because the window has to be built and themed before anyone logs in.
# Nothing here is a secret and nothing here should become one — app_config's
# vault-related rows (master_hash, salt, kdf_params, vault_format and key_check)
# are all owned by auth.py, and none of them is written from here.
#
# Named "preferences" rather than "settings" because 'settings' is the name of
# the dict this returns and of the local holding it in much of the UI, where a
# module of that name would be shadowed.
# -----------------------------------------------------------------------------

import json
import ntpath
import posixpath
import sqlite3

import encryption
from database import connect, read_config, write_config

# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------

VALID_LOCK_TIMEOUTS     = (5, 10, 15, 30, 60)
VALID_CLIPBOARD_CLEARS  = (15, 30, 60, 120)
VALID_SHUTDOWN_TIMEOUTS = (30, 60, 120, 240, 0)
# Order drives the swatch row in Settings. Must stay in step with THEMES in
# ui/theme.py — the dialog iterates this and looks each key up there.
VALID_THEMES            = ('dark', 'light')
VALID_FONT_SIZES        = ('small', 'default', 'large', 'xl')
# 'compact' folds the balance, updated and due captions onto one line;
# 'comfortable' gives each its own.
VALID_LIST_DENSITIES    = ('compact', 'comfortable')

VALID_BACKUP_FREQUENCIES = ('daily', 'weekly', 'monthly')
# How long backups are kept, in months. 0 means forever, matching the way
# VALID_SHUTDOWN_TIMEOUTS already spells "never" — an idiom worth having only
# one of.
VALID_BACKUP_RETENTION_MONTHS = (1, 3, 6, 12, 24, 0)

# Stored instead of the resolved path when the user has not chosen a folder.
# A sentinel rather than the directory it stands for, so that "beside the
# vault" keeps meaning that after the vault is moved: storing the resolved
# path would leave a moved vault writing its backups back to where it used
# to live.
BACKUP_LOCATION_DEFAULT = 'default'

_SETTING_DEFAULTS = {
    'lock_timeout_minutes':        15,
    'clipboard_clear_seconds':     30,
    'shutdown_after_lock_minutes': 60,
    'theme':                       'dark',
    'font_size':                   'default',
    'list_density':                'compact',
    # Off until asked for. An app that starts copying the vault somewhere on
    # its own is not a decision to make on the user's behalf.
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
            # Keys bound from _SETTING_DEFAULTS rather than spelled out a second
            # time. The two lists had to agree and nothing enforced it: a setting
            # added to the defaults dict but missed here would be written to disk
            # by save_settings and never read back, so the dialog would open on
            # the default every time and quietly save it over the stored value.
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

    # A stored value outside the valid set matches no entry in the Settings
    # combo and is rejected by save_settings, so the dialog could not be saved
    # until it changed — the one thing it could not do. A zero or negative lock
    # timeout also reaches set_lock_timeout_seconds, whose floor would then be
    # all that stands between a hand-edited row and a vault that locks itself
    # the instant it opens.
    for key, valid in (('lock_timeout_minutes',        VALID_LOCK_TIMEOUTS),
                       ('clipboard_clear_seconds',     VALID_CLIPBOARD_CLEARS),
                       ('shutdown_after_lock_minutes', VALID_SHUTDOWN_TIMEOUTS),
                       ('auto_backup_enabled',         (0, 1)),
                       ('backup_frequency',            VALID_BACKUP_FREQUENCIES),
                       ('backup_retention_months',     VALID_BACKUP_RETENTION_MONTHS)):
        if settings[key] not in valid:
            settings[key] = _SETTING_DEFAULTS[key]

    # No valid set to check against — it is a path the user typed or picked.
    # An empty one falls back rather than resolving to the working directory,
    # which is wherever the shortcut happened to launch from.
    if not isinstance(settings['backup_location'], str) or not settings['backup_location'].strip():
        settings['backup_location'] = BACKUP_LOCATION_DEFAULT

    if settings['theme'] not in VALID_THEMES:
        settings['theme'] = _SETTING_DEFAULTS['theme']
    if settings['font_size'] not in VALID_FONT_SIZES:
        settings['font_size'] = _SETTING_DEFAULTS['font_size']
    if settings['list_density'] not in VALID_LIST_DENSITIES:
        settings['list_density'] = _SETTING_DEFAULTS['list_density']
    return settings


def save_settings(lock_timeout_minutes: int, clipboard_clear_seconds: int,
                  shutdown_after_lock_minutes: int, theme: str, font_size: str,
                  list_density: str = 'compact', auto_backup_enabled: int = 0,
                  backup_frequency: str = 'weekly',
                  backup_retention_months: int = 12,
                  backup_location: str = BACKUP_LOCATION_DEFAULT):
    """Validates and persists all settings.

    Persists only; callers follow with auth.apply_saved_settings to apply the
    lock timeout. That timeout is session state owned by auth, and reaching into
    it from here would create an import cycle."""
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
    with connect() as db:
        for key, value in pairs:
            write_config(key, value, db)
    # Four of those rows are backup instructions; the seal covers them all.
    seal_backup_config()


# -----------------------------------------------------------------------------
# Backup bookkeeping
#
# Not settings: the user never types any of it and the Settings dialog never
# shows it as a field. It is what the backup run reads to decide whether one is
# due and which files it is allowed to delete, plus the one line Settings shows
# back — a backup feature that has been failing silently for four months is
# worse than not having one.
#
# Kept in app_config beside the settings, so it travels with the vault. A copy
# of a vault therefore carries the record of the original's backups, which is
# why services/backups.py checks that the vault it is running in is the one the
# settings were written for before it acts on any of this.
# -----------------------------------------------------------------------------

_BACKUP_STATE_KEYS = ('last_backup_date', 'last_backup_outcome', 'backup_files',
                      'backup_source')

# -----------------------------------------------------------------------------
# Sealing the backup configuration
#
# Everything above is plaintext, which is right for a theme and wrong for these.
# app_config is readable and writable by anyone holding the vault *file*, with
# no master password, and these particular rows are instructions: where to write
# a copy of the whole vault, and which files retention may delete. Left
# unauthenticated they let somebody who can write a vault — a shared one on a
# network share, a synced folder, a stick handed over — point the next backup at
# a destination of their choosing and have the victim's own unlock carry it out.
#
# So they are sealed: a token over the exact bytes of every backup row,
# encrypted under the session key. Only somebody who can already open the vault
# can produce one, which is the property the rows themselves lack.
#
# Sealed, not encrypted. The rows stay readable so that a locked window can
# still be built, and so a vault whose seal is rejected can say what it is
# discarding rather than showing an unreadable blob.
#
# Failure is closed and one-way: no valid seal means the stored configuration is
# not this vault's own, so it is replaced with the defaults — automatic backups
# off, the folder beside the vault — and resealed. Adopting what is already
# there instead would defeat the control entirely, since deleting the seal row
# is no harder than writing the rows it covers.
# -----------------------------------------------------------------------------

_BACKUP_AUTH_KEY = 'backup_auth'

# Every row the backup run reads. A key added to the backup feature and missed
# here would be the one an attacker gets to write, so this list is the whole
# surface and is worth checking against _SETTING_DEFAULTS when either changes.
_AUTHENTICATED_BACKUP_KEYS = (
    'auto_backup_enabled', 'backup_frequency', 'backup_retention_months',
    'backup_location', 'backup_confirmed_dir', *_BACKUP_STATE_KEYS,
)


def _backup_aad() -> bytes:
    return encryption.field_aad('app_config', _BACKUP_AUTH_KEY)


def _backup_fingerprint() -> str:
    """Canonical text over every backup row, for sealing and for checking.

    Sorted and separator-fixed so the same configuration always produces the
    same bytes; None and '' are folded together because a row that was never
    written and one written empty mean the same thing here."""
    return json.dumps({key: (_read_config(key) or '')
                       for key in _AUTHENTICATED_BACKUP_KEYS},
                      sort_keys=True, separators=(',', ':'))


def seal_backup_config() -> bool:
    """Records that the backup rows as they now stand were written by this app.

    Called after every write that touches one. False when the vault is locked,
    which leaves the seal stale and the configuration refused on next use —
    fail-closed, and nothing in the app writes these rows while locked."""
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
        # Any failure is the same answer. A token that will not decrypt, was
        # written under another key, or does not match the rows it covers are
        # all "not ours", and none of them is worth a different code path.
        return False


def backup_config_is_pristine() -> bool:
    """Whether every backup row is still at its default.

    A vault that has never configured a backup has nothing to lose to a reset,
    so it is sealed silently rather than reported as tampered with — otherwise
    every first unlock after this shipped would accuse the user of something."""
    for key in _AUTHENTICATED_BACKUP_KEYS:
        stored = _read_config(key)
        if stored in (None, ''):
            continue
        if key in _SETTING_DEFAULTS and stored == str(_SETTING_DEFAULTS[key]):
            continue
        return False
    return True


def reset_backup_config():
    """Puts the backup rows back to their defaults and seals them.

    The destination and the schedule go back to the defaults rather than being
    cleared, and the record of written files is emptied so retention has nothing
    it believes it may delete."""
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

    `files` is the list of backups this app created, each an entry of
    {'name', 'date'}. Pruning reads it and deletes nothing that is not on it —
    a manual copy that happens to be sitting in the same folder under a name of
    the same shape is not ours to remove.

    `source` is the vault path the rest of this was written for; see
    services/backups.py for what a mismatch means.
    """
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

    This row is read back and handed to unlink(), and app_config is plaintext:
    anyone who can write the vault *file* can put anything here without knowing
    the master password. A name carrying a separator, a drive or a '..' would
    leave the backups folder — `Path(folder) / 'C:/…'` drops the folder
    entirely — so the app would delete a file it never wrote.

    Everything this app records comes from services.backups.backup_name(), which
    is always a bare filename, so nothing legitimate is refused here."""
    if not isinstance(name, str) or name in ('', '.', '..'):
        return False
    # ntpath, not the running platform's rules: a vault written on Windows can
    # be opened on Linux, where a backslash is an ordinary filename character
    # and 'C:\\Windows\\x' would sail through posixpath as one long name.
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
    """Records the folder the user has agreed backups may be written to.

    Sealed like the rest, which is what makes it worth recording: an unsealed
    note saying 'the user approved this' would be written by the same hand that
    chose the destination."""
    _write_backup_config('backup_confirmed_dir', path)


def _write_backup_config(key: str, value: str):
    """Writes a sealed row. The seal covers every backup row at once, so it is
    renewed here rather than by each caller remembering to."""
    _write_config(key, value)
    seal_backup_config()


def clear_backup_state():
    """Forgets this vault's backup history without touching a single file.

    Used when a vault turns out to be a copy of another one: the record it
    inherited describes backups of a different file, kept somewhere the copy has
    no business writing to or deleting from."""
    for key in _BACKUP_STATE_KEYS:
        _write_config(key, '')
    seal_backup_config()


# -----------------------------------------------------------------------------
# View state
#
# Stored per item rather than in save_settings' bundle, which validates and
# rewrites all six preferences together: these change as the window is sorted,
# resized and dragged, and folding them in would rewrite the theme and all three
# timeouts on every flick of the sort combo.
#
# All cosmetic, so every path here fails silently and falls back to "no stored
# value" rather than blocking a launch.
# -----------------------------------------------------------------------------

def _read_config(key: str) -> str | None:
    """database.read_config, silenced. Cosmetic state must never block a launch,
    while the same failure reading a master hash has to be heard."""
    try:
        return read_config(key)
    except sqlite3.Error:
        return None


def _write_config(key: str, value: str):
    """database.write_config, silenced. See _read_config."""
    try:
        write_config(key, value)
    except sqlite3.Error:
        pass


VALID_SORT_KEYS = ('name', 'due_date', 'last_updated')

# Direction each sort starts in before the user overrides it. A–Z and the
# nearest due date read ascending; "Last Updated" means most recent first.
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
    """Stores the sort order."""
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


# The sidebar's drag range, and the bounds a stored width has to fall inside to
# be believed — a corrupt row must not collapse the list panel.
#
# Defined here rather than in ui/main_window.py, which used to hold its own copy
# under different names and a comment pointing at the wrong module. This is the
# side that reads the value off disk, and it cannot import from ui/; the window
# can import from here, so this is the end the one definition has to live at.
SIDEBAR_MIN = 240
SIDEBAR_MAX = 560


def get_window_geometry() -> bytes | None:
    """The stored result of QMainWindow.saveGeometry(), or None.

    Qt's opaque blob rather than our own x/y/w/h: it carries the screen and
    maximised state, and restoreGeometry checks it against the displays actually
    attached, which keeps a window saved on an unplugged monitor from reopening
    off-screen."""
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
    have — in which case the list fits itself to its widest row instead."""
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

# -----------------------------------------------------------------------------
# services/backups.py
# Copying the vault: by hand, and on a schedule.
#
# A backup is a copy of the vault file. It opens with the master password the
# vault held when it was taken — which is why change_master_password is followed
# by an offer of a fresh copy.
#
# Three rules the rest of this file exists to hold:
#
#   * a partial write never becomes a backup. Copies land on a temporary name
#     in the destination folder and are verified as openable vaults before
#     being renamed into place;
#   * deletion only ever follows a successful write, so a destination that has
#     stopped working stops the history growing instead of eating it;
#   * nothing is deleted that this app did not create. Pruning works from the
#     recorded list of files it wrote, not from the folder's contents.
# -----------------------------------------------------------------------------

import datetime
import os
import re
import sqlite3
import tempfile
from pathlib import Path

from core import database
from core import preferences
from core.categories import parse_iso_date
from core.database import VAULT_SUFFIX

# Days between backups at each setting. Elapsed days rather than calendar
# boundaries, which survives an app that is not opened every day.
FREQUENCY_DAYS = {'daily': 1, 'weekly': 7, 'monthly': 30}

# Folder made beside the vault when the user has not named one.
DEFAULT_DIR_NAME = 'backups'

_TEMP_PREFIX = '.avbackup-'

# The shape backup_name() produces: '<stem>_backup_<YYYYMMDD>.vault'. Only a
# name matching this is ever deleted. The stem is whatever the user called their
# vault, but it cannot contain a separator, a drive, or '..'.
_BACKUP_NAME = re.compile(
    r'[^\\/:*?"<>|]+_backup_\d{8}' + re.escape(VAULT_SUFFIX))


class BackupError(Exception):
    """A backup could not be written, with a sentence saying why."""


# What the user is told when check_seal discards a configuration.
RESET_MESSAGE = ("The stored backup settings did not match this vault and have "
                 "been reset. Automatic backups are off until you turn them on "
                 "again in Settings.")


# -----------------------------------------------------------------------------
# Naming and locations
# -----------------------------------------------------------------------------

def backup_name(vault_path=None, when=None) -> str:
    """The filename a backup of this vault takes today: `mymoney.vault` becomes
    `mymoney_backup_20260917.vault`, which sorts in the order taken."""
    vault_path = Path(vault_path or database.DB_PATH)
    when = when or datetime.date.today()
    return f"{vault_path.stem}_backup_{when:%Y%m%d}{VAULT_SUFFIX}"


def default_dir(vault_path=None) -> Path:
    """The backups folder beside the vault."""
    return Path(vault_path or database.DB_PATH).parent / DEFAULT_DIR_NAME


def resolve_dir(settings: dict, vault_path=None) -> Path:
    """Where backups go, given the stored setting. The default sentinel is
    resolved here and nowhere else, so a moved vault's backups follow it."""
    location = settings.get('backup_location') or preferences.BACKUP_LOCATION_DEFAULT
    if location == preferences.BACKUP_LOCATION_DEFAULT:
        return default_dir(vault_path)
    return Path(location).expanduser()


# -----------------------------------------------------------------------------
# Copying
# -----------------------------------------------------------------------------

def copy_vault(destination) -> Path:
    """Copies the open vault to destination, atomically. Returns the path.

    Uses sqlite3's backup API rather than a file copy, which could capture a
    file torn by a write in flight. The copy is built under a temporary name in
    the destination folder, checked as a vault that opens, and only then renamed
    into place."""
    if database.DB_PATH is None:
        raise BackupError('No vault is open.')

    destination = Path(destination).expanduser()
    folder = destination.parent
    try:
        created_folder = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"The backup folder could not be created: {exc}")

    # Only a folder this call has just made; one the user picked is theirs.
    if created_folder:
        database.restrict_to_owner(folder)

    handle, temp_name = None, None
    try:
        # mkstemp creates the file 0o600; the ACL follows before a byte is
        # copied in.
        handle, temp_name = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=str(folder))
        os.close(handle)
        handle = None
        database.restrict_to_owner(Path(temp_name))

        _copy_database(Path(temp_name))

        if database.inspect_vault(temp_name) != 'ok':
            raise BackupError('The copy did not come out as a readable vault.')

        os.replace(temp_name, destination)
        temp_name = None
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise BackupError(str(exc))
    finally:
        if handle is not None:
            os.close(handle)
        if temp_name is not None:
            # Half a vault in a backups folder looks like one that worked.
            try:
                os.unlink(temp_name)
            except OSError:
                pass

    database.restrict_to_owner(destination)
    return destination


def _copy_database(target: Path):
    source = sqlite3.connect(database.DB_PATH)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()


# -----------------------------------------------------------------------------
# Scheduling
# -----------------------------------------------------------------------------

def is_due(settings: dict, state: dict, today=None) -> bool:
    """Whether a scheduled backup is owed. A schedule missed for months produces
    one backup, not one per missed interval."""
    if not settings.get('auto_backup_enabled'):
        return False

    today = today or datetime.date.today()
    last = _parse_date(state.get('last_backup_date'))
    if last is None:
        return True
    # A recorded date in the future is a clock that moved; treat it as due.
    if last > today:
        return True

    interval = FREQUENCY_DAYS.get(settings.get('backup_frequency'), 7)
    return (today - last).days >= interval


def is_default_dir(settings: dict, folder=None) -> bool:
    """Whether backups are going to the folder beside the vault, which needs no
    confirming: it travels with the vault and the user never chose it."""
    folder = folder or resolve_dir(settings)
    return os.path.normcase(str(folder)) == os.path.normcase(str(default_dir()))


def destination_confirmed(folder) -> bool:
    """Whether the user has agreed to backups going to this folder. The record
    is sealed, so this is only meaningful after check_seal has run."""
    confirmed = preferences.get_backup_state().get('confirmed_dir') or ''
    if not confirmed:
        return False
    return os.path.normcase(confirmed) == os.path.normcase(str(folder))


def destination_warning(folder) -> str:
    """What is worth saying about where this folder is, or ''. A backup is the
    whole vault, encrypted; a copy somewhere the user does not control can be
    taken away and attacked offline at leisure."""
    text = str(folder)
    if text.startswith('\\\\') or text.startswith('//'):
        return ("That folder is on a network share. Each backup is a complete "
                "copy of your vault — encrypted, but anyone who can read the "
                "share can take one away and try passwords against it offline.")
    if _is_removable(folder):
        return ("That folder is on a removable drive. Each backup is a complete "
                "copy of your vault — encrypted, but a drive that is lost or "
                "lent out takes the copy with it.")
    return ''


def _is_removable(folder) -> bool:
    """Whether the folder sits on removable media. Windows only; False when it
    cannot be told."""
    if os.name != 'nt':
        return False
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(str(folder)))[0]
        if not drive:
            return False
        # 2 is DRIVE_REMOVABLE.
        return ctypes.windll.kernel32.GetDriveTypeW(drive + '\\') == 2
    except Exception:
        return False


def check_seal() -> str:
    """Confirms the stored backup configuration is this vault's own.

    Returns 'ok' when it is, 'sealed' when a never-configured vault was sealed
    just now, and 'reset' when a configuration that could not be vouched for
    was discarded — which the caller must tell the user about, because their
    automatic backups have just been turned off.

    Must run before anything reads the backup settings to act on them."""
    if preferences.backup_config_is_sealed():
        return 'ok'
    if preferences.backup_config_is_pristine():
        preferences.seal_backup_config()
        return 'sealed'
    preferences.reset_backup_config()
    return 'reset'


def run_if_due(settings: dict, today=None) -> dict | None:
    """Takes the scheduled backup if one is owed. None when nothing was due.

    Returns {'ok', 'path'|'message', 'pruned'} so the caller can report the
    outcome. Never raises: a backup that cannot be written must not interrupt an
    unlock."""
    today = today or datetime.date.today()

    # Checked again even when the caller already has: this function must never
    # act on rows that were not vouched for.
    if check_seal() == 'reset':
        return {'ok': False, 'reset': True, 'pruned': 0, 'message': RESET_MESSAGE}

    state = _usable_state(settings)
    if not is_due(settings, state, today):
        return None

    folder = resolve_dir(settings)
    name = backup_name(when=today)
    destination = folder / name

    # Today's backup already exists, and a second unlock must not overwrite the
    # morning's copy with the evening's vault. The file is not adopted into the
    # record unless it already is: Save a Copy offers this same name, so it may
    # be one the user wrote by hand, and retention may only delete our own.
    if destination.exists():
        preferences.save_backup_date(today.isoformat())
        if any(entry.get('name') == name for entry in state.get('files', [])):
            _record_file(state, name, today)
        return None

    try:
        copy_vault(destination)
    except BackupError as exc:
        outcome = {'date': today.isoformat(), 'ok': False, 'message': str(exc)}
        preferences.save_backup_outcome(outcome)
        # last_backup_date is not moved on, so the next unlock tries again.
        return {'ok': False, 'message': str(exc), 'pruned': 0}

    preferences.save_backup_date(today.isoformat())
    preferences.save_backup_source(str(database.DB_PATH))
    preferences.save_backup_outcome(
        {'date': today.isoformat(), 'ok': True, 'name': name})
    entries = _record_file(state, name, today)

    # Only now, with a new backup on disk and verified.
    pruned = prune(settings, entries, folder, today)
    # Read back rather than trusted: a share or a stick may not be able to
    # express owner-only, and says so by doing nothing.
    return {'ok': True, 'path': destination, 'pruned': pruned,
            'hardened': database.verify_restricted(destination)}


# -----------------------------------------------------------------------------
# Retention
# -----------------------------------------------------------------------------

def prune(settings: dict, entries: list, folder: Path, today=None) -> int:
    """Deletes recorded backups older than the retention window. Returns how many.

    Never deletes the newest backup whatever its age (retention means "stop
    accumulating", never "end up with nothing"), never a file it has no record
    of writing, and nothing when the window is 0 ("keep forever").

    Age comes from the date recorded with the file, not its mtime, which a copy
    to a new machine resets."""
    months = settings.get('backup_retention_months', 12)
    today = today or datetime.date.today()
    if not months:
        _store(entries)
        return 0

    dated = [(entry, _parse_date(entry.get('date'))) for entry in entries]
    # An entry whose date will not parse is never deleted: its age is unknown,
    # not old.
    newest = max((date for _, date in dated if date), default=None)

    cutoff = _months_before(today, months)
    survivors, deleted = [], 0
    for entry, date in dated:
        expired = (date is not None and date != newest
                   and (date.year, date.month) < cutoff)
        if not expired:
            survivors.append(entry)
            continue
        target = _deletable(folder, entry.get('name'))
        if target is None:
            # Not a name this app could have written: kept, and kept recorded.
            survivors.append(entry)
            continue
        try:
            target.unlink()
            deleted += 1
        except FileNotFoundError:
            # Already gone; drop the record.
            pass
        except OSError:
            # Still there and not removable; keep the record so it is retried.
            survivors.append(entry)

    _store(survivors)
    return deleted


def expired_count(entries: list, months: int, today=None) -> int:
    """How many recorded backups a retention window would eventually remove, by
    the same rules prune follows."""
    today = today or datetime.date.today()
    if not months:
        return 0
    dated = [d for d in (_parse_date(e.get('date')) for e in entries) if d]
    if not dated:
        return 0
    newest = max(dated)
    cutoff = _months_before(today, months)
    return sum(1 for date in dated
               if date != newest and (date.year, date.month) < cutoff)


def _months_before(today: datetime.date, months: int) -> tuple:
    """The (year, month) that many whole months back. A backup is kept while its
    own month is this one or later, which rounds the window up — towards
    keeping."""
    total = today.year * 12 + (today.month - 1) - months
    return total // 12, total % 12 + 1


# -----------------------------------------------------------------------------
# The record
# -----------------------------------------------------------------------------

def _usable_state(settings: dict) -> dict:
    """The backup history, unless it belongs to a different vault.

    A backup is a copy of the whole vault, app_config included, so a copy opened
    as a live vault brings the original's list of backup files with it. Left
    alone it would prune the original's history. Detected by the vault path
    recorded alongside: if it names a different file, the history is forgotten.
    The settings survive — wanting backups is a preference worth inheriting."""
    state = preferences.get_backup_state()
    source = state.get('source')
    if source and database.DB_PATH is not None:
        if os.path.normcase(source) != os.path.normcase(str(database.DB_PATH)):
            preferences.clear_backup_state()
            return preferences.get_backup_state()
    return state


def _deletable(folder: Path, name) -> Path | None:
    """The file inside `folder` that this name refers to, or None to leave it.

    The last check before unlink(), alongside preferences._is_bare_filename on
    the way in. The name must have backup_name()'s exact shape — so it cannot
    leave the folder or name the user's own vault — and its resolved parent must
    be the folder, in case the folder is reached through a link."""
    if not isinstance(name, str) or not _BACKUP_NAME.fullmatch(name):
        return None
    target = folder / name
    try:
        if target.resolve().parent != folder.resolve():
            return None
    except OSError:
        return None
    return target


def _record_file(state: dict, name: str, when: datetime.date) -> list:
    """Adds a backup to the record, replacing any entry of the same name."""
    entries = [e for e in state.get('files', []) if e.get('name') != name]
    entries.append({'name': name, 'date': when.isoformat()})
    _store(entries)
    return entries


def _store(entries: list):
    preferences.save_backup_files(
        sorted(entries, key=lambda entry: (entry.get('date', ''), entry.get('name', ''))))


def _parse_date(value):
    return parse_iso_date(value) if isinstance(value, str) else None

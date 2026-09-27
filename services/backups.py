# -----------------------------------------------------------------------------
# services/backups.py
# Copying the vault: by hand, and on a schedule.
#
# A backup is a copy of the vault file. It used to be a format of its own — a
# JSON envelope sealed under a second password — and replacing that with a copy
# trades one property for two. Lost: a backup that survives a master password
# change, because a copy is frozen under whatever key the vault held when it
# was taken. Gained: there is no second password to lose, and restoring is
# opening the file rather than an import that could reject or reshape what it
# reads.
#
# The frozen-key trade is real and foreseeable — a copy taken in January still
# wants January's password in June — so change_master_password offers a fresh
# copy on the spot rather than leaving it to be discovered.
#
# Three rules the rest of this file exists to hold:
#
#   * a partial write never becomes a backup. Copies land on a temporary name
#     in the destination folder and are verified as openable vaults before
#     being renamed into place, so a full disk or a pulled drive leaves no file
#     rather than a plausible-looking broken one;
#   * deletion only ever follows a successful write. Pruning runs after a new
#     backup exists and never otherwise, so a destination that has stopped
#     working stops the history growing instead of eating it;
#   * nothing is deleted that this app did not create. Pruning works from the
#     recorded list of files it wrote, not from whatever matches the naming
#     pattern in the folder.
# -----------------------------------------------------------------------------

import datetime
import os
import re
import sqlite3
import tempfile
from pathlib import Path

import database
import preferences
from database import VAULT_SUFFIX

# Days between backups at each setting. Elapsed days rather than calendar
# boundaries: "a week" is seven days from the last one, which is what somebody
# choosing weekly means and what survives an app that is not opened every day.
FREQUENCY_DAYS = {'daily': 1, 'weekly': 7, 'monthly': 30}

# Folder made beside the vault when the user has not named one.
DEFAULT_DIR_NAME = 'backups'

_TEMP_PREFIX = '.avbackup-'

# The shape backup_name() produces: '<stem>_backup_<YYYYMMDD>.vault'. Only a
# name matching this is ever deleted — see _deletable. The stem is whatever the
# user called their vault, so it is left open, but it cannot contain a path
# separator, a drive, or the '..' that would take the name out of the folder.
_BACKUP_NAME = re.compile(
    r'[^\\/:*?"<>|]+_backup_\d{8}' + re.escape(VAULT_SUFFIX))


class BackupError(Exception):
    """A backup could not be written, with a sentence saying why."""


# -----------------------------------------------------------------------------
# Naming and locations
# -----------------------------------------------------------------------------

def backup_name(vault_path=None, when=None) -> str:
    """The filename a backup of this vault takes today.

    `mymoney.vault` becomes `mymoney_backup_20260917.vault`. The date is local
    and unpunctuated so the files sort in the order they were taken, which is
    the order anybody scanning the folder is looking for.
    """
    vault_path = Path(vault_path or database.DB_PATH)
    when = when or datetime.date.today()
    return f"{vault_path.stem}_backup_{when:%Y%m%d}{VAULT_SUFFIX}"


def default_dir(vault_path=None) -> Path:
    """The backups folder beside the vault."""
    return Path(vault_path or database.DB_PATH).parent / DEFAULT_DIR_NAME


def resolve_dir(settings: dict, vault_path=None) -> Path:
    """Where backups go, given the stored setting.

    The sentinel is resolved here and nowhere else, which is what lets a vault
    be moved without its backups carrying on into the old folder."""
    location = settings.get('backup_location') or preferences.BACKUP_LOCATION_DEFAULT
    if location == preferences.BACKUP_LOCATION_DEFAULT:
        return default_dir(vault_path)
    return Path(location).expanduser()


# -----------------------------------------------------------------------------
# Copying
# -----------------------------------------------------------------------------

def copy_vault(destination) -> Path:
    """Copies the open vault to destination, atomically. Returns the path.

    sqlite3's own backup API rather than a file copy. The vault is a live
    database on a rollback journal, and shutil.copy taken while a write is in
    flight captures a file that is torn in exactly the way nobody notices until
    they need it. The API copies pages under a read lock and produces a
    consistent database whatever else is happening.

    The copy is built under a temporary name in the destination folder, checked
    for being a vault that opens, and only then renamed into place. A rename
    within one filesystem is atomic, so the named file is either the previous
    one or a complete new one, and never a half-written thing wearing a
    backup's name.
    """
    if database.DB_PATH is None:
        raise BackupError('No vault is open.')

    destination = Path(destination).expanduser()
    folder = destination.parent
    try:
        created_folder = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"The backup folder could not be created: {exc}")

    # Only a folder this call has just made. One the user picked is theirs; see
    # database._harden_parent for the same rule and why it matters.
    if created_folder:
        database.restrict_to_owner(folder)

    handle, temp_name = None, None
    try:
        # Created owner-only before a byte goes in, so the copy is never briefly
        # readable by everyone the way a file made under the ambient umask is.
        handle, temp_name = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=str(folder))
        os.close(handle)
        handle = None
        os.chmod(temp_name, 0o600)
        database.restrict_to_owner(Path(temp_name))

        _copy_database(Path(temp_name))

        # Checked before the rename, not after: the point of the temporary name
        # is that nothing which fails this is ever called a backup.
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
            # A failed copy leaves nothing behind. Half a vault in a backups
            # folder is worse than no vault: it is indistinguishable from one
            # that worked until somebody tries to open it.
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
    """Whether a scheduled backup is owed.

    A schedule that has been missed for months produces one backup, not one per
    interval that went by: the vault only has one state to copy, and filling the
    retention window with copies of the same moment is how the older ones get
    pushed out.
    """
    if not settings.get('auto_backup_enabled'):
        return False

    today = today or datetime.date.today()
    last = _parse_date(state.get('last_backup_date'))
    if last is None:
        return True
    # A recorded date in the future is a clock that has been moved, not a
    # backup from next week. Treat it as due rather than waiting it out.
    if last > today:
        return True

    interval = FREQUENCY_DAYS.get(settings.get('backup_frequency'), 7)
    return (today - last).days >= interval


def is_default_dir(settings: dict, folder=None) -> bool:
    """Whether backups are going to the folder beside the vault.

    The default needs no confirming: it travels with the vault, it is covered by
    whatever protects the vault's own folder, and the user never chose it."""
    folder = folder or resolve_dir(settings)
    return os.path.normcase(str(folder)) == os.path.normcase(str(default_dir()))


def destination_confirmed(folder) -> bool:
    """Whether the user has agreed to backups going to this folder.

    The record is sealed, so it says what the user approved rather than what the
    file claims they approved."""
    confirmed = preferences.get_backup_state().get('confirmed_dir') or ''
    if not confirmed:
        return False
    return os.path.normcase(confirmed) == os.path.normcase(str(folder))


def destination_warning(folder) -> str:
    """What is worth saying about where this folder is, or ''.

    A backup is the whole vault in one file. It is encrypted, so this is not a
    disclosure — but a copy somewhere the user does not control is a copy
    somebody else can take away and spend as long as they like guessing at, and
    that is worth a sentence before it starts happening on a schedule rather
    than after."""
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
    """Whether the folder sits on removable media. False when it cannot be told.

    Windows only, and best effort: this decides whether to add a sentence, so
    being unable to answer means saying nothing rather than guessing."""
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

    Returns 'ok' when it is, 'sealed' when a vault that had never configured a
    backup was sealed just now, and 'reset' when a configuration that could not
    be vouched for was discarded — which is the answer the caller has to tell
    the user about, because their automatic backups have just been turned off.

    Called before anything reads the backup settings. The rows say where to copy
    the vault and which files may be deleted, and until this has run they are
    only what was in the file."""
    if preferences.backup_config_is_sealed():
        return 'ok'
    if preferences.backup_config_is_pristine():
        # Nothing configured, so nothing to discard and nobody to tell. This is
        # every vault's first run, and the upgrade path for one that never
        # turned backups on.
        preferences.seal_backup_config()
        return 'sealed'
    preferences.reset_backup_config()
    return 'reset'


def run_if_due(settings: dict, today=None) -> dict | None:
    """Takes the scheduled backup if one is owed. None when nothing was due.

    Returns {'ok', 'path'|'message', 'pruned'} so the caller can report the
    outcome, which is also recorded for Settings to show. Never raises: a
    backup that cannot be written must not be able to interrupt an unlock.
    """
    today = today or datetime.date.today()

    # Before the settings are read for anything. A configuration that cannot be
    # vouched for is discarded rather than acted on, and the caller is told so
    # it can say why the backups it had set up have stopped.
    seal = check_seal()
    if seal == 'reset':
        return {'ok': False, 'reset': True, 'pruned': 0, 'message':
                "The stored backup settings did not match this vault and have "
                "been reset. Automatic backups are off until you turn them on "
                "again in Settings."}
    # Past here the seal vouches for the stored rows, so the settings the caller
    # read out of them are as good as re-reading. On 'reset' we never get here,
    # and the caller reloads — see MainWindow._run_scheduled_backup.

    state = _usable_state(settings)
    if not is_due(settings, state, today):
        return None

    folder = resolve_dir(settings)
    name = backup_name(when=today)
    destination = folder / name

    # Today's backup already exists, so there is nothing to add — and, more to
    # the point, nothing to overwrite. Without this a second unlock on the same
    # day would rewrite the morning's backup with the evening's vault, which is
    # how a good copy gets replaced by one taken after something went wrong.
    #
    # The date moves on but the file is *not* adopted into the record unless it
    # is already there. Save a Copy offers this same folder and this same name
    # by default, so the file sitting here can easily be one the user wrote by
    # hand — and a file this app did not create is not one it may later delete.
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
        # last_backup_date is deliberately not moved on. A failed attempt has
        # not backed anything up, and recording it as though it had would mean
        # waiting out another whole interval before trying again.
        return {'ok': False, 'message': str(exc), 'pruned': 0}

    preferences.save_backup_date(today.isoformat())
    preferences.save_backup_source(str(database.DB_PATH))
    preferences.save_backup_outcome(
        {'date': today.isoformat(), 'ok': True, 'name': name})
    entries = _record_file(state, name, today)

    # Only now, with a new backup on disk and verified. This ordering is the
    # whole safety property of the retention setting: a destination that has
    # stopped working never reaches here, so it stops the history growing
    # rather than consuming it.
    pruned = prune(settings, entries, folder, today)
    # Read back rather than trusted: copy_vault asks for owner-only and a
    # filesystem that cannot express it says so by doing nothing. The vault's
    # own hardening is reported at startup by main._warn_if_unhardened; a backup
    # lands somewhere the user picked, which is likelier to be a share or a
    # stick, so it is worth the same honesty.
    return {'ok': True, 'path': destination, 'pruned': pruned,
            'hardened': database.verify_restricted(destination)}


# -----------------------------------------------------------------------------
# Retention
# -----------------------------------------------------------------------------

def prune(settings: dict, entries: list, folder: Path, today=None) -> int:
    """Deletes recorded backups older than the retention window. Returns how many.

    Three things it will not do:

      * delete the newest backup, whatever its age. Come back to a vault after
        thirteen months with a twelve-month window and every backup in the
        folder is expired; taken literally that empties the folder at the exact
        moment somebody is least sure of themselves. Retention means "stop
        accumulating", never "end up with nothing";
      * delete a file it has no record of writing. A copy the user made by hand
        can sit in this folder under a name of the same shape, and it is not
        ours to tidy away;
      * delete anything when the window is 0, which is the "keep forever"
        setting.

    Age comes from the date recorded with the file rather than its mtime: a
    backup that was copied to a new machine, or restored from a sync service,
    has a modification time that says today and tells you nothing about when it
    was taken.
    """
    months = settings.get('backup_retention_months', 12)
    today = today or datetime.date.today()
    if not months:
        _store(entries)
        return 0

    dated = [(entry, _parse_date(entry.get('date'))) for entry in entries]
    # An entry whose date will not parse is kept and never deleted: the record
    # is the only evidence of when it was taken, so an unreadable one means the
    # age is unknown, not that it is old.
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
            # Not a name this app could have written. Kept rather than deleted,
            # and kept rather than dropped: forgetting it would only hide it.
            survivors.append(entry)
            continue
        try:
            target.unlink()
            deleted += 1
        except FileNotFoundError:
            # Already gone — somebody tidied the folder. Drop the record and
            # count nothing: no deletion happened here.
            pass
        except OSError:
            # Still there and not removable. Keep the record, or the next run
            # would forget the file exists and leave it behind forever.
            survivors.append(entry)

    _store(survivors)
    return deleted


def expired_count(entries: list, months: int, today=None) -> int:
    """How many recorded backups a retention window would eventually remove.

    Answers the question Settings has to put in front of somebody shortening the
    window, and answers it by the same rules prune does — the newest is never
    counted, an undated entry is never counted, and 0 months counts nothing.
    """
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
    """The (year, month) that many whole months back.

    Compared as a pair rather than by subtracting days, so the window does not
    depend on how long the months in it happened to be, and so there is never a
    question about what "one month before the 31st" means. A backup is kept
    while its own month is this one or later, which rounds the window up to the
    nearest whole month — erring towards keeping backups is the only direction
    to err in here.
    """
    total = today.year * 12 + (today.month - 1) - months
    return total // 12, total % 12 + 1


# -----------------------------------------------------------------------------
# The record
# -----------------------------------------------------------------------------

def _usable_state(settings: dict) -> dict:
    """The backup history, unless it belongs to a different vault.

    A backup is a copy of the whole vault file, app_config included, so opening
    one as a live vault brings the original's backup settings and the original's
    list of backup files with it. Left alone, the copy would write its backups
    into the original's folder under the original's name, and prune the
    original's history — from a file the user is probably only looking at
    because something went wrong with the original.

    Detected by the vault path recorded alongside: if it names a different file,
    the history is forgotten and this vault starts its own. The settings survive
    — wanting backups is a preference worth inheriting — but everything that
    points at another vault's files does not.
    """
    state = preferences.get_backup_state()
    source = state.get('source')
    if source and database.DB_PATH is not None:
        if os.path.normcase(source) != os.path.normcase(str(database.DB_PATH)):
            preferences.clear_backup_state()
            return preferences.get_backup_state()
    return state


def _deletable(folder: Path, name) -> Path | None:
    """The file inside `folder` that this name refers to, or None to leave it.

    The last word before unlink(), and deliberately not the only one —
    preferences._is_bare_filename refuses the same names as they are read. Both
    exist because app_config is plaintext: someone who can write a vault file,
    with no master password, can put any string in the recorded backup list, and
    a name like 'C:/Users/you/taxes.pdf' or '../../Desktop/keys.pem' would
    otherwise be handed to unlink() the next time retention ran.

    Checked against what this app actually writes — backup_name()'s
    '<stem>_backup_<YYYYMMDD>.vault' — rather than merely against escaping the
    folder, so a name that stays put but names the user's own vault is refused
    too. The resolved parent is compared as well, in case the folder itself is
    reached through a link."""
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
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None

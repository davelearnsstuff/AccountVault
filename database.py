# -----------------------------------------------------------------------------
# database.py
# SQLite storage for AccountVault.
# Synchronous — the Qt event loop is the only driver, so there is no reason
# for the database layer to be async.
# -----------------------------------------------------------------------------

import getpass
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

BASE_DIR = Path(__file__).resolve().parent

# The extension a vault file carries. Deliberately the same one the old
# encrypted backups used: a backup is now a copy of the vault rather than a
# document in a format of its own, so there is only one kind of file left for
# the extension to mean.
VAULT_SUFFIX = '.vault'

# Where vaults lived before the user got to choose. Both are still opened when
# they are found — see vaultfile.startup_path, which adopts one silently rather
# than asking somebody to go and find their own vault.
LEGACY_APP_DIR_PATH = BASE_DIR / 'data' / 'accountvault.db'
LEGACY_DB_NAME = 'accountvault.db'


def app_data_dir() -> Path:
    """The per-user directory this app owns.

    Home to the vault registry, and to any vault the user did not pick a place
    for. It is the one directory here whose permissions are ours to set: a
    location the user chose is theirs, and init_db must not restrict it on
    their behalf — see _harden below.
    """
    if os.name == 'nt':
        root = os.environ.get('LOCALAPPDATA')
        base = Path(root) if root else Path.home() / 'AppData' / 'Local'
    else:
        root = os.environ.get('XDG_DATA_HOME')
        base = Path(root) if root else Path.home() / '.local' / 'share'
    return base / 'AccountVault'


def default_vault_path() -> Path:
    """Where a vault goes when the user creates one without picking a folder.

    Under the application data directory rather than beside the code, for two
    reasons that outlived the fixed location. init_db can restrict this
    directory to the owner but nothing restricts the source tree, so a vault
    kept there is protected by an ACL on the database while the module that
    decrypts it sits unprotected next to it — an attacker who can write
    encryption.py does not need to read the database at all. And an install
    under Program Files cannot be written to, so the vault would be silently
    redirected by UAC virtualization into a per-user shadow copy that backup
    tools and the user both fail to find.
    """
    return app_data_dir() / ('accountvault' + VAULT_SUFFIX)


# The vault currently open, or None when there is none — at first launch, and
# between closing one vault and opening the next. Read inside every function
# here at call time rather than captured, so rebinding it is all it takes to
# point the app at a different file.
#
# None is not an error state the app has to route around: connect() raises
# NoVaultOpen, which is a sqlite3.Error, and every caller that already tolerates
# a database it cannot read (preferences, the window geometry, the sort order)
# falls back to its default without knowing anything new.
DB_PATH = None


class NoVaultOpen(sqlite3.Error):
    """Raised by connect() when no vault file is open.

    Deliberately a sqlite3.Error. The cosmetic readers in preferences.py
    already treat "the database would not answer" as "use the default", which
    is exactly right here, and the ones that must not swallow it — the master
    hash, the key check — do not catch sqlite3.Error either.
    """


class VaultFileError(Exception):
    """A vault file cannot be used, with a sentence saying why."""


# Authoritative source for re-keying, export, import, and migration. Covers
# metadata (names, usernames, URLs, notes) as well as the obviously sensitive
# fields, so the existence of an account is protected and not just its password.
ENCRYPTED_FIELDS = {
    'accounts':        ['name', 'username', 'password', 'website', 'notes', 'extra_data'],
    'balance_history': ['balance'],
}

# Well-known SIDs, used instead of names because the names are localised — the
# first is "NT-AUTORITÄT\SYSTEM" on a German Windows, and icacls would reject it.
_SYSTEM_SID = 'S-1-5-18'

# Groups that must never hold a grant on the vault. /inheritance:r removes only
# inherited entries and /grant:r replaces only the principals it names, so any
# of these held as an explicit ACE — how several system locations are built —
# would survive the restriction otherwise.
#
# BUILTIN\Administrators and OWNER RIGHTS are deliberately absent: they can take
# ownership regardless, so removing them buys nothing and breaks tooling.
_WIDE_SIDS = (
    'S-1-1-0',       # Everyone
    'S-1-5-32-545',  # BUILTIN\Users
    'S-1-5-11',      # NT AUTHORITY\Authenticated Users
    'S-1-5-4',       # NT AUTHORITY\INTERACTIVE
)


def restrict_to_owner(path: Path):
    """Limits a file or directory to the account that owns it. True only when
    the restriction was applied and read back as applied.

    chmod means almost nothing on Windows — it toggles the read-only bit and
    ignores the mode — so the POSIX call alone would leave the vault at whatever
    the parent directory grants, which off the profile root can be far wider.

    SYSTEM is kept: it is not a security boundary, since anything running as
    SYSTEM can take ownership anyway, and removing it breaks File History,
    search indexing and antivirus.

    Best effort. A vault on a filesystem that cannot express this must still
    open, because the encryption is the real protection and this is defence in
    depth. init_db records the result in permissions_hardened."""
    try:
        if os.name != 'nt':
            path.chmod(0o700 if path.is_dir() else 0o600)
        elif not _restrict_windows(path):
            return False
        return verify_restricted(path)
    except Exception:
        return False


def verify_restricted(path: Path) -> bool:
    """Reads the permissions back and says whether they are actually owner-only.

    chmod can be a no-op on the filesystem and icacls can report success against
    an unintended principal, either of which is otherwise indistinguishable from
    a working restriction. A check that cannot be performed returns False rather
    than assuming the best."""
    try:
        if os.name != 'nt':
            return path.stat().st_mode & 0o077 == 0
        return _verify_windows(path)
    except Exception:
        return False


def _verify_windows(path: Path) -> bool:
    """True when the ACL read back shows inheritance actually dropped.

    Inherited entries are marked '(I)' in every locale, so their absence is the
    one property establishable from icacls output without depending on the
    language Windows is running in. With the /remove:g pass in
    _restrict_windows, that rules out Everyone, Users, Authenticated Users and
    INTERACTIVE.

    It does not enumerate the remaining principals: their names are localised
    and resolving them to SIDs would need pywin32, which this app does not
    depend on. A second user granted access explicitly by name would pass."""
    result = _icacls(str(path))
    if result.returncode != 0:
        return False
    aces = [line for line in result.stdout.splitlines() if ':(' in line]
    return bool(aces) and not any('(I)' in line for line in aces)


def _windows_principals() -> list:
    """Names to try for the current user, best first.

    The bare account name is what icacls wants on a standalone machine, and is
    ambiguous only once a domain is involved."""
    user = getpass.getuser()
    names = [user]
    domain = os.environ.get('USERDOMAIN')
    if domain:
        names.append(f'{domain}\\{user}')
    return names


def _icacls(*args) -> subprocess.CompletedProcess:
    """Runs icacls without flashing a console window under pythonw."""
    return subprocess.run(
        ['icacls', *args], check=False, capture_output=True, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def _restrict_windows(path: Path) -> bool:
    """Drops inheritance, grants the owner and SYSTEM, and removes any wide
    group already held explicitly. True when all of that succeeded."""
    inherit = '(OI)(CI)' if path.is_dir() else ''
    granted = False
    for principal in _windows_principals():
        result = _icacls(str(path), '/inheritance:r',
                         '/grant:r', f'{principal}:{inherit}F',
                         '/grant:r', f'*{_SYSTEM_SID}:{inherit}F')
        if result.returncode == 0:
            granted = True
            break
    if not granted:
        return False

    # Removing a SID that holds no grant succeeds, so presence needs no check.
    for sid in _WIDE_SIDS:
        if _icacls(str(path), '/remove:g', f'*{sid}').returncode != 0:
            return False
    return True


# True when the vault file came back verified as owner-only, together with its
# directory in the one case where that directory is ours to restrict. main.py
# warns the user when it is False.
permissions_hardened = True

_SQLITE_MAGIC = b'SQLite format 3\x00'


def open_vault(path, *, create: bool):
    """Points the app at a vault file and brings it up.

    `create` says which of the two things the caller means, and a mismatch is
    refused rather than resolved either way. Opening a path that is not there
    must not quietly make a new empty vault — reached from a recent-files entry
    whose file has been moved, that is indistinguishable from the vault having
    been emptied. Creating over a path that is there must not truncate it: a
    save dialog offers "overwrite?" as an ordinary confirmation, and the file on
    the other side of that click is somebody's only copy of everything.

    Leaves DB_PATH as it found it if the vault will not come up, so a failed
    switch does not strand the app pointing at a file it never opened.
    """
    global DB_PATH
    path = Path(path).expanduser()
    if create and path.exists():
        raise VaultFileError(
            f"{path.name} already exists. Open it instead, or choose another name."
        )
    if not create and not path.exists():
        raise VaultFileError(f"There is no file at {path}.")

    previous = DB_PATH
    DB_PATH = path
    try:
        init_db()
    except Exception:
        DB_PATH = previous
        raise


def close_vault():
    """Forgets which vault is open. connect() raises NoVaultOpen afterwards.

    The teardown that matters — the key and the decrypted caches — belongs to
    auth.lock_app and the listeners it fires. This is only the file half, and it
    goes last so that nothing can still be mid-write when the path disappears.
    """
    global DB_PATH, permissions_hardened
    DB_PATH = None
    permissions_hardened = True


def inspect_vault(path) -> str:
    """What a path actually holds, creating and changing nothing.

    Returns one of:

      'ok'          a vault, carrying a master hash
      'missing'     nothing at that path, but the drive it names is there
      'unavailable' the drive or share itself is not reachable
      'empty'       a zero-byte file, which is what a failed create leaves
      'not-sqlite'  a file, but not a database
      'not-vault'   a database, but not one of ours
      'unreadable'  it is there and will not open

    'missing' and 'unavailable' are kept apart because they mean opposite things
    to whoever reads the message: one says the file has been moved or deleted,
    the other says to plug the stick back in.

    Opened read-only through a file: URI, so a path that disappears between the
    check and the connect cannot be brought into existence here as a side
    effect.
    """
    path = Path(path).expanduser()
    try:
        exists = path.exists()
    except OSError:
        return 'unavailable'
    if not exists:
        anchor = Path(path.anchor) if path.anchor else path.parent
        try:
            return 'missing' if anchor.exists() else 'unavailable'
        except OSError:
            return 'unavailable'

    try:
        with open(path, 'rb') as handle:
            magic = handle.read(len(_SQLITE_MAGIC))
    except OSError:
        return 'unreadable'
    if not magic:
        return 'empty'
    if magic != _SQLITE_MAGIC:
        return 'not-sqlite'

    return 'ok' if format_of(path) is not None else 'not-vault'


def format_of(path):
    """The vault format recorded in a file, or None if it is not a vault.

    A vault is identified by carrying a master hash rather than by having the
    right tables: a database left behind by an interrupted init has every table
    and is still not something anyone can log into.

    Callers compare the number against auth.VAULT_FORMAT themselves. Knowing
    what today's format is belongs to auth, and importing it here would close a
    cycle — auth is built on this module.
    """
    try:
        db = sqlite3.connect(_read_only_uri(Path(path)), uri=True)
    except sqlite3.Error:
        return None
    try:
        rows = dict(db.execute(
            "SELECT key, value FROM app_config "
            "WHERE key IN ('master_hash', 'vault_format')"
        ).fetchall())
    except sqlite3.Error:
        return None
    finally:
        db.close()

    if not rows.get('master_hash'):
        return None
    try:
        return int(rows.get('vault_format') or 0)
    except (TypeError, ValueError):
        return 0


def _read_only_uri(path: Path) -> str:
    """A file: URI SQLite will open without being able to create anything.

    Drive letters and separators both have to survive the trip. SQLite parses
    the URI itself and percent-decodes it, so ':' and '/' are left alone while
    everything it would otherwise read as syntax — spaces, '#', '?' — is
    escaped.

    Made absolute first, and that is not cosmetic. A file: URI has no notion of
    a working directory, so 'demo/vault.vault' became 'file:///demo/vault.vault'
    — a path at the root of the drive, which is not there. SQLite opened that
    read-only without complaint, found no tables, and format_of answered None,
    so inspect_vault reported a perfectly good vault as "a database, but not an
    AccountVault vault". Every caller passing a path straight from the command
    line hit it, and the message sent the user looking for corruption that was
    not there.

    absolute() rather than resolve(): this only has to name the file, and
    resolve() would additionally follow symlinks — a different decision, and
    not one this function is entitled to make on a caller's behalf."""
    text = str(Path(path).expanduser().absolute()).replace('\\', '/')
    return 'file:///' + quote(text.lstrip('/'), safe='/:') + '?mode=ro'


def init_db():
    """Creates or opens the vault at DB_PATH and applies the schema."""
    global permissions_hardened
    if DB_PATH is None:
        raise NoVaultOpen('No vault is open.')

    # Read before the mkdir below, which would otherwise make every answer
    # "yes, we just created it".
    parent_existed = DB_PATH.parent.exists()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    hardened = _harden_parent(parent_existed)
    # Before the schema goes in: sqlite3.connect would create the file under the
    # ambient umask, leaving it world-readable for the whole of its first write.
    _create_db_file()
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                category     TEXT NOT NULL,
                name         TEXT NOT NULL,
                username     TEXT,
                password     TEXT,
                website      TEXT,
                notes        TEXT,
                extra_data   TEXT,
                closed       INTEGER DEFAULT 0,
                -- Backstop only: services/accounts.py binds date_added
                -- explicitly on every insert.
                date_added   TEXT DEFAULT CURRENT_TIMESTAMP,
                last_updated TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS balance_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                balance     TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
    # The file is restricted once, here, rather than also before the schema
    # block above. Twice was not merely redundant: _create_db_file returns
    # quietly when it cannot create the file, so on that path the earlier call
    # ran against a path that did not exist yet, answered False, and poisoned
    # `hardened` for good — connect() would then create the file, this call
    # would restrict it successfully, and the app would still warn the user
    # about a vault that is in fact owner-only. Restricting after the file
    # certainly exists is the only ordering that reports what is true.
    permissions_hardened = restrict_to_owner(DB_PATH) and hardened


def _harden_parent(parent_existed: bool) -> bool:
    """Restricts the vault's directory when that directory is the app's to
    restrict, and reports whether the vault is worse off for the answer.

    A directory the user chose is not ours. This used to restrict DB_PATH.parent
    unconditionally, which was right for as long as every vault lived in one
    directory this app had created — and becomes destructive the moment somebody
    saves a vault into Documents, because the ACL does not land on the vault, it
    lands on Documents and locks every other application out of it.

    So it runs for exactly two directories: the application data directory, and
    one this very call has just created. Declining anywhere else returns True
    rather than False — the file itself is still restricted directly, and the
    file is what the warning in main.py is about.
    """
    parent = DB_PATH.parent
    if parent == app_data_dir() or not parent_existed:
        return restrict_to_owner(parent)
    return True


def _create_db_file():
    """Creates the database file with owner-only permissions if it is absent.

    Applying the mode at creation leaves no window in which the file exists and
    is readable. Harmless if the filesystem ignores it — restrict_to_owner
    covers that case straight after."""
    try:
        fd = os.open(DB_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except FileExistsError:
        return
    except OSError:
        return
    os.close(fd)


# The only two statements in the app that touch app_config.
_CONFIG_SELECT = "SELECT value FROM app_config WHERE key = ?"
_CONFIG_UPSERT = "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)"


def read_config(key: str, db=None) -> str | None:
    """The stored value for key, or None.

    Pass db to read inside a transaction the caller already holds. Errors
    propagate: a caller wanting a default catches them itself, and the ones
    reading the master hash must not."""
    if db is not None:
        row = db.execute(_CONFIG_SELECT, (key,)).fetchone()
    else:
        with connect() as own:
            row = own.execute(_CONFIG_SELECT, (key,)).fetchone()
    return row[0] if row else None


def write_config(key: str, value: str, db=None) -> int:
    """Stores value under key, inserting the row if absent. Returns rows
    written, which a caller that cannot tolerate a silent no-op should check.

    Pass db to write inside a transaction the caller already holds — the
    password rotation needs its config writes to commit or roll back together
    with the re-encrypted fields."""
    if db is not None:
        return db.execute(_CONFIG_UPSERT, (key, value)).rowcount
    with connect() as own:
        return own.execute(_CONFIG_UPSERT, (key, value)).rowcount


def vacuum() -> bool:
    """Rebuilds the database file, dropping the free pages entirely. True if it ran.

    secure_delete on connect() scrubs content as it is freed from here on, but
    it cannot reach what was already sitting in the freelist when it was turned
    on — which for an upgrade is exactly the interesting part, since that is
    where migrate_plaintext_fields left the pre-encryption copies of every
    field. VACUUM writes a fresh file from the live pages only.

    Its own connection: VACUUM cannot run inside a transaction, and connect()
    opens one implicitly. Best effort — a vault that will not compact is still
    a vault that works."""
    if DB_PATH is None:
        return False
    try:
        db = sqlite3.connect(DB_PATH, isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        db.execute("VACUUM")
        return True
    except sqlite3.Error:
        return False
    finally:
        db.close()


@contextmanager
def connect():
    """Opens a connection with row access by name and foreign keys enforced.
    Commits on clean exit, rolls back if the body raises.

    Raises NoVaultOpen rather than connecting to None, which sqlite3 would
    otherwise turn into a confusing TypeError deep inside a caller that has no
    idea a vault is even involved."""
    if DB_PATH is None:
        raise NoVaultOpen('No vault is open.')
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    # Overwrites deleted content instead of leaving it in the freelist to be
    # reused whenever. Without it a deleted account's ciphertext, and — far more
    # to the point — the plaintext that migrate_plaintext_fields replaces on an
    # upgrade, stay legible in freed pages long after the row is gone. The cost
    # is extra writes on delete, which at this table size is not measurable.
    db.execute("PRAGMA secure_delete = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

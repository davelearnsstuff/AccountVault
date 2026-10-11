# -----------------------------------------------------------------------------
# core/database.py
# SQLite storage for AccountVault.
# Synchronous — the Qt event loop is the only driver.
# -----------------------------------------------------------------------------

import getpass
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

# The project root, one up from core/. The legacy vault location below is
# relative to it, so moving this file must not move where that is looked for.
BASE_DIR = Path(__file__).resolve().parent.parent

# The extension a vault file carries. A backup is a copy of the vault, so there
# is only one kind of file for it to mean.
VAULT_SUFFIX = '.vault'

# Where vaults lived before the user got to choose. Still opened when found —
# see vaultfile.startup_path.
LEGACY_APP_DIR_PATH = BASE_DIR / 'data' / 'accountvault.db'
LEGACY_DB_NAME = 'accountvault.db'


def app_data_dir() -> Path:
    """The per-user directory this app owns: home to the vault registry, the
    error log, and any vault the user did not pick a place for. It is the one
    directory whose permissions are ours to set — see _harden_parent."""
    if os.name == 'nt':
        root = os.environ.get('LOCALAPPDATA')
        base = Path(root) if root else Path.home() / 'AppData' / 'Local'
    else:
        root = os.environ.get('XDG_DATA_HOME')
        base = Path(root) if root else Path.home() / '.local' / 'share'
    return base / 'AccountVault'


def default_vault_path() -> Path:
    """Where a vault goes when the user creates one without picking a folder.

    Not beside the code: the source tree is not restricted to the owner (and
    whoever can write encryption.py does not need to read the vault), and an
    install under Program Files would be redirected by UAC virtualization into a
    shadow copy nobody can find."""
    return app_data_dir() / ('accountvault' + VAULT_SUFFIX)


# The vault currently open, or None. Read at call time everywhere, so rebinding
# it is all it takes to point the app at a different file.
#
# With no vault open, connect() raises NoVaultOpen, a sqlite3.Error, so the
# cosmetic readers that already fall back to defaults on a database error keep
# working unchanged.
DB_PATH = None


class NoVaultOpen(sqlite3.Error):
    """Raised by connect() when no vault file is open. A sqlite3.Error so that
    "use the default" readers handle it; the master-hash and key-check readers
    do not catch sqlite3.Error and so still hear about it."""


class VaultFileError(Exception):
    """A vault file cannot be used, with a sentence saying why."""


# Authoritative source for re-keying, export, import, and migration. Covers
# metadata (names, usernames, URLs, notes) as well as the obviously sensitive
# fields, so the existence of an account is protected and not just its password.
ENCRYPTED_FIELDS = {
    'accounts':        ['name', 'username', 'password', 'website', 'notes', 'extra_data'],
    'balance_history': ['balance'],
}

# Well-known SIDs, used instead of names because the names are localised.
_SYSTEM_SID = 'S-1-5-18'

# Groups that must never hold a grant on the vault. /inheritance:r removes only
# inherited entries, so any of these held as an explicit ACE would survive it.
# BUILTIN\Administrators is deliberately absent: it can take ownership anyway.
_WIDE_SIDS = (
    'S-1-1-0',       # Everyone
    'S-1-5-32-545',  # BUILTIN\Users
    'S-1-5-11',      # NT AUTHORITY\Authenticated Users
    'S-1-5-4',       # NT AUTHORITY\INTERACTIVE
)


def restrict_to_owner(path: Path):
    """Limits a file or directory to the account that owns it. True only when
    the restriction was applied and read back as applied.

    chmod is nearly meaningless on Windows, so there the ACL is rewritten with
    icacls. SYSTEM keeps access: anything running as SYSTEM can take ownership
    anyway, and removing it breaks File History, indexing and antivirus.

    Best effort. The encryption is the real protection, so a filesystem that
    cannot express this must not stop a vault opening."""
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
    A check that cannot be performed returns False."""
    try:
        if os.name != 'nt':
            return path.stat().st_mode & 0o077 == 0
        return _verify_windows(path)
    except Exception:
        return False


def _verify_windows(path: Path) -> bool:
    """True when the ACL read back shows inheritance actually dropped.

    Inherited entries are marked '(I)' in every locale, so their absence is the
    one property readable from icacls output without depending on the display
    language. Together with the /remove:g in _restrict_windows, that rules out
    the wide groups. A second user granted access explicitly by name would
    pass; naming principals reliably would need pywin32."""
    result = _icacls(str(path))
    if result.returncode != 0:
        return False
    aces = [line for line in result.stdout.splitlines() if ':(' in line]
    return bool(aces) and not any('(I)' in line for line in aces)


def _windows_principals() -> list:
    """Names to try for the current user, best first. The bare name is what
    icacls wants on a standalone machine."""
    user = getpass.getuser()
    names = [user]
    domain = os.environ.get('USERDOMAIN')
    if domain:
        names.append(f'{domain}\\{user}')
    return names


def _icacls_path() -> str:
    """icacls by full path. Run by bare name, Windows looks in the current
    working directory before System32."""
    root = os.environ.get('SystemRoot') or r'C:\Windows'
    return os.path.join(root, 'System32', 'icacls.exe')


def _icacls(*args) -> subprocess.CompletedProcess:
    """Runs icacls without flashing a console window under pythonw."""
    return subprocess.run(
        [_icacls_path(), *args], check=False, capture_output=True, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def _restrict_windows(path: Path) -> bool:
    """Drops inheritance, grants the owner and SYSTEM, and removes any wide
    group held explicitly — in one icacls call. True when it succeeded.

    Removing a SID that holds no grant succeeds, so the removals need no check
    first."""
    inherit = '(OI)(CI)' if path.is_dir() else ''
    removals = [f'*{sid}' for sid in _WIDE_SIDS]
    for principal in _windows_principals():
        result = _icacls(str(path), '/inheritance:r',
                         '/grant:r', f'{principal}:{inherit}F',
                         '/grant:r', f'*{_SYSTEM_SID}:{inherit}F',
                         '/remove:g', *removals)
        if result.returncode == 0:
            return True
    return False


# True when the vault file came back verified as owner-only, together with its
# directory where that directory is ours to restrict. main.py warns when False.
permissions_hardened = True

_SQLITE_MAGIC = b'SQLite format 3\x00'


def open_vault(path, *, create: bool):
    """Points the app at a vault file and brings it up.

    `create` says which the caller means, and a mismatch is refused. Opening a
    missing path must not quietly make an empty vault — from a recent-files entry
    whose file has moved, that looks exactly like the vault being emptied. And
    creating over an existing file must not truncate somebody's only copy.

    Leaves DB_PATH as it found it if the vault will not come up."""
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
    """Forgets which vault is open; connect() raises NoVaultOpen afterwards.

    Only the file half of closing. The key and the decrypted caches belong to
    auth.lock_app and its listeners, which run first."""
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

    'missing' and 'unavailable' are kept apart because one means the file moved
    and the other means plug the drive back in. Can block for as long as the
    network takes to give up on an unreachable share — see vaultfile.probe."""
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
    right tables: an interrupted init leaves every table and nothing anyone can
    log into. Callers compare against auth.VAULT_FORMAT themselves; importing
    auth here would be a cycle."""
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

    Made absolute first: a file: URI has no working directory, so a relative
    path would name a location at the root of the drive. SQLite percent-decodes
    the URI itself, so ':' and '/' are kept and everything else that is URI
    syntax is escaped. absolute() rather than resolve(), which would also follow
    symlinks."""
    text = str(Path(path).expanduser().absolute()).replace('\\', '/')
    return 'file:///' + quote(text.lstrip('/'), safe='/:') + '?mode=ro'


def init_db():
    """Creates or opens the vault at DB_PATH and applies the schema."""
    global permissions_hardened
    if DB_PATH is None:
        raise NoVaultOpen('No vault is open.')

    # Read before the mkdir, which would otherwise make the answer always yes.
    parent_existed = DB_PATH.parent.exists()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    hardened = _harden_parent(parent_existed)
    # Before the schema goes in: sqlite3.connect would create the file under the
    # ambient umask, world-readable for the whole of its first write.
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
    # Restricted once, here, after the file certainly exists — restricting a
    # path _create_db_file could not create would report a false failure.
    permissions_hardened = restrict_to_owner(DB_PATH) and hardened


def _harden_parent(parent_existed: bool) -> bool:
    """Restricts the vault's directory when that directory is ours to restrict,
    and reports whether the vault is worse off for the answer.

    A directory the user chose is not ours: restricting Documents because a
    vault was saved there would lock every other application out of it. So this
    runs only for the application data directory and for a directory this call
    has just created. Declining elsewhere returns True — the file itself is
    still restricted directly."""
    parent = DB_PATH.parent
    if parent == app_data_dir() or not parent_existed:
        return restrict_to_owner(parent)
    return True


def _create_db_file():
    """Creates the database file owner-only if it is absent, so it is never
    readable by others even briefly. Harmless if the filesystem ignores the
    mode — restrict_to_owner follows."""
    try:
        fd = os.open(DB_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
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
    """Stores value under key. Returns rows written, which a caller that cannot
    tolerate a silent no-op should check.

    Pass db to write inside a transaction the caller already holds."""
    if db is not None:
        return db.execute(_CONFIG_UPSERT, (key, value)).rowcount
    with connect() as own:
        return own.execute(_CONFIG_UPSERT, (key, value)).rowcount


def vacuum() -> bool:
    """Rebuilds the database file from its live pages, dropping the freelist.
    True if it ran.

    Its own connection, because VACUUM cannot run inside the transaction
    connect() opens. Best effort — a vault that will not compact still works."""
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

    Raises NoVaultOpen rather than connecting to None, which sqlite3 would turn
    into a confusing TypeError deep inside the caller."""
    if DB_PATH is None:
        raise NoVaultOpen('No vault is open.')
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    # Overwrites deleted content instead of leaving it in the freelist, where a
    # deleted account's ciphertext — or the plaintext a legacy migration
    # replaced — would stay legible long after the row is gone.
    db.execute("PRAGMA secure_delete = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# -----------------------------------------------------------------------------
# core/database.py
# SQLite storage for AccountVault.
# Synchronous — the Qt event loop is the only driver.
# -----------------------------------------------------------------------------

import ctypes
import getpass
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from core.encryption import valid_password_hash

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


class ForeignSchemaError(sqlite3.DatabaseError):
    """Raised by connect() when the vault's schema holds something this app
    never creates. A sqlite3.Error, so every caller treats the vault the way it
    treats one that will not open."""


# -----------------------------------------------------------------------------
# The schema a vault may hold
#
# SQLite keeps triggers and views inside the database file, and runs them on
# every connection that writes. A trigger planted by anyone who could write the
# file once would run on every save from then on: putting a forged value back,
# rewriting app_config, surviving a password change, and travelling into every
# backup. The app creates none, so a vault holding one, or any table or index it
# did not make, is refused rather than used.
# -----------------------------------------------------------------------------

_VAULT_TABLES = frozenset({'accounts', 'balance_history', 'app_config'})

# One table per category, from the first day's design. The move to one table
# never dropped them, so a vault from then may still carry them. Tables hold
# data but run nothing, so they are no reason to refuse it.
_LEGACY_TABLES = frozenset({'banking', 'creditcards', 'investments', 'loans',
                            'secure_notes', 'utilities', 'websites'})


def schema_problem(db) -> str | None:
    """What in this database's schema the app did not create, as 'trigger
    "name"', or None when there is nothing.

    Tables named sqlite_* are SQLite's own (sqlite_sequence, for AUTOINCREMENT)
    and cannot be created by a statement. An index is allowed only when it is
    the one SQLite builds for app_config's primary key, which has no SQL."""
    for kind, name, sql in db.execute("SELECT type, name, sql FROM sqlite_master"):
        if kind == 'table' and (name in _VAULT_TABLES or name in _LEGACY_TABLES
                                or name.startswith('sqlite_')):
            continue
        if kind == 'index' and name.startswith('sqlite_autoindex_') and sql is None:
            continue
        return f'{kind} "{name}"'
    return None


def foreign_schema_message(problem: str) -> str:
    return (f"This vault has been changed by another program: it holds a {problem}, "
            f"which AccountVault never adds. It won't be used, because that could "
            f"change your data every time you save. Restore it from a backup.")


def _harden_connection(db):
    """SQLite's protections against a hostile database file.

    trusted_schema off stops triggers and views from calling functions with
    side effects. Defensive mode refuses the statements that corrupt a file
    directly, such as writable_schema. Neither replaces schema_problem; they
    narrow what a schema could do if one ever got past it."""
    db.execute("PRAGMA trusted_schema = OFF")
    # setconfig is Python 3.12+; 3.11 still gets the pragma and the check.
    if hasattr(db, 'setconfig'):
        db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)


# Authoritative source for re-keying, export, import, and migration. Covers
# metadata (categories, names, usernames, URLs, notes) as well as the obviously sensitive
# fields, so the existence of an account is protected and not just its password.
ENCRYPTED_FIELDS = {
    'accounts':        ['category', 'name', 'username', 'password', 'website', 'notes',
                        'extra_data'],
    'balance_history': ['balance'],
}

# The columns that name a row in its fields' associated data from format 3 on
# (see encryption.row_aad). A balance entry is bound to its account as well as
# itself, so it can neither move to another account nor be copied as a new one.
ROW_KEY = {
    'accounts':        ('id',),
    'balance_history': ('account_id', 'id'),
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


# -----------------------------------------------------------------------------
# Whether other accounts can change the vault's folder
#
# Restricting the vault file does not stop anyone who can write its folder:
# they can rename or replace the file, or plant a rollback journal beside it,
# which SQLite replays as crash recovery the next time the vault opens. A folder
# the user chose is not ours to restrict (see _harden_parent), so this only
# finds out, and the UI says so.
# -----------------------------------------------------------------------------

def folder_open_to_others(folder) -> bool | None:
    """True when an account other than the owner (and SYSTEM and
    Administrators, which can take over any file anyway) can create, delete or
    rename files in folder. None when that cannot be told."""
    try:
        if os.name == 'nt':
            return _windows_folder_open(Path(folder))
        return _posix_folder_open(Path(folder))
    except Exception:
        return None


def _posix_folder_open(folder: Path) -> bool:
    """Write permission for others, for a group the owner shares, or a folder
    owned by somebody else. A group write bit is not counted when the group is
    the owner's own private group, which is how many Linux systems set
    umask 002 up."""
    st = folder.stat()
    uid = os.getuid()
    if st.st_uid not in (uid, 0):
        return True
    if st.st_mode & 0o002:
        return True
    return bool(st.st_mode & 0o020) and not _group_is_private(st.st_gid, uid)


def _group_is_private(gid: int, uid: int) -> bool:
    import grp
    import pwd
    try:
        group = grp.getgrgid(gid)
        owner = pwd.getpwuid(uid).pw_name
    except KeyError:
        return False
    if set(group.gr_mem) - {owner}:
        return False
    return all(user.pw_uid == uid for user in pwd.getpwall() if user.pw_gid == gid)


# Folder rights that let someone put a file in place or take one away: create
# a file, delete or rename one, delete or rename the folder itself, take over
# its permissions, and the generic rights that include those. Creating a
# subfolder is left out: C:\ grants it to every user, and a subfolder cannot
# stand in for the vault.
_FOLDER_WRITE_RIGHTS = (
    0x00000002    # FILE_ADD_FILE
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)

# Holders of a write grant that are not "other accounts". SYSTEM and
# Administrators can take ownership of anything, as restrict_to_owner notes.
_TRUSTED_FOLDER_SIDS = frozenset({
    _SYSTEM_SID,
    'S-1-5-32-544',  # BUILTIN\Administrators
    'S-1-3-0',       # CREATOR OWNER, which stands for the owner
    'S-1-3-4',       # OWNER RIGHTS
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464',  # TrustedInstaller
})

_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERIT_ONLY_ACE = 0x08


def _windows_folder_open(folder: Path) -> bool | None:
    """Reads the folder's ACL through the Windows API. icacls cannot answer
    this: it prints names in the display language rather than SIDs.

    Only allow entries are read. A deny entry that cancels a grant is not
    worked out, so this can warn about a folder that is in fact closed, but it
    never calls an open one closed."""
    api = _win_api()
    user_sid = _windows_user_sid(api)
    if user_sid is None:
        return None
    trusted = _TRUSTED_FOLDER_SIDS | {user_sid}

    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    status = api.GetNamedSecurityInfoW(
        str(folder), 1, 4,  # SE_FILE_OBJECT, DACL_SECURITY_INFORMATION
        None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status != 0:
        return None
    try:
        if not dacl.value:
            # A null DACL grants everyone everything.
            return True
        count = _AclHeader.from_address(dacl.value).AceCount
        for index in range(count):
            ace = ctypes.c_void_p()
            if not api.GetAce(dacl, index, ctypes.byref(ace)):
                return None
            header = _AceHeader.from_address(ace.value)
            if (header.AceType != _ACCESS_ALLOWED_ACE_TYPE
                    or header.AceFlags & _INHERIT_ONLY_ACE):
                continue
            # ACCESS_ALLOWED_ACE: the header, a 4-byte mask, then the SID.
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            if not mask & _FOLDER_WRITE_RIGHTS:
                continue
            sid = _sid_string(api, ace.value + 8)
            if sid is None:
                return None
            if sid not in trusted:
                return True
        return False
    finally:
        api.LocalFree(descriptor)


def _sid_string(api, sid_pointer) -> str | None:
    text = ctypes.c_void_p()
    if not api.ConvertSidToStringSidW(ctypes.c_void_p(sid_pointer), ctypes.byref(text)):
        return None
    try:
        return ctypes.wstring_at(text.value)
    finally:
        api.LocalFree(text)


def _windows_user_sid(api) -> str | None:
    """The SID of the account this process runs as."""
    from ctypes import wintypes
    token = wintypes.HANDLE()
    if not api.OpenProcessToken(api.GetCurrentProcess(), 0x0008,  # TOKEN_QUERY
                                ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        api.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
            return None
        # TOKEN_USER starts with the PSID of the user.
        return _sid_string(api, ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        api.CloseHandle(token)


def _win_api():
    """The handful of Windows calls the folder check needs, typed once."""
    global _WIN_API
    if _WIN_API is not None:
        return _WIN_API
    from ctypes import wintypes
    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)

    class Api:
        GetNamedSecurityInfoW = advapi32.GetNamedSecurityInfoW
        GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                          pointer, pointer, pointer, pointer, pointer]
        GetNamedSecurityInfoW.restype = wintypes.DWORD
        GetAce = advapi32.GetAce
        GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, pointer]
        GetAce.restype = wintypes.BOOL
        ConvertSidToStringSidW = advapi32.ConvertSidToStringSidW
        ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, pointer]
        ConvertSidToStringSidW.restype = wintypes.BOOL
        OpenProcessToken = advapi32.OpenProcessToken
        OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
        OpenProcessToken.restype = wintypes.BOOL
        GetTokenInformation = advapi32.GetTokenInformation
        GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        GetTokenInformation.restype = wintypes.BOOL
        GetCurrentProcess = kernel32.GetCurrentProcess
        GetCurrentProcess.argtypes = []
        GetCurrentProcess.restype = wintypes.HANDLE
        CloseHandle = kernel32.CloseHandle
        CloseHandle.argtypes = [wintypes.HANDLE]
        CloseHandle.restype = wintypes.BOOL
        LocalFree = kernel32.LocalFree
        LocalFree.argtypes = [ctypes.c_void_p]
        LocalFree.restype = ctypes.c_void_p

    _WIN_API = Api
    return _WIN_API


_WIN_API = None


class _AclHeader(ctypes.Structure):
    _fields_ = [('AclRevision', ctypes.c_ubyte), ('Sbz1', ctypes.c_ubyte),
                ('AclSize', ctypes.c_ushort), ('AceCount', ctypes.c_ushort),
                ('Sbz2', ctypes.c_ushort)]


class _AceHeader(ctypes.Structure):
    _fields_ = [('AceType', ctypes.c_ubyte), ('AceFlags', ctypes.c_ubyte),
                ('AceSize', ctypes.c_ushort)]


# True when the vault file and its journal came back verified as owner-only,
# together with its directory where that directory is ours to restrict.
permissions_hardened = True

# Whether other accounts can change files in the vault's folder: True, False,
# or None when it could not be told. See folder_open_to_others.
folder_open = False

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
    global DB_PATH, permissions_hardened, folder_open
    DB_PATH = None
    permissions_hardened = True
    folder_open = False


def inspect_vault(path) -> str:
    """What a path actually holds, creating and changing nothing.

    Returns one of:

      'ok'          a vault, carrying a master hash
      'missing'     nothing at that path, but the drive it names is there
      'unavailable' the drive or share itself is not reachable
      'empty'       a zero-byte file, which is what a failed create leaves
      'not-sqlite'  a file, but not a database
      'not-vault'   a database, but not one of ours
      'altered'     a vault whose schema holds something the app never creates
                    (see schema_problem)
      'damaged'     a vault whose master hash is not one it is safe to verify
                    (see encryption.valid_password_hash)
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

    rows = _identity_rows(path)
    if not rows.get('master_hash'):
        return 'not-vault'
    if _schema_problem_of(path):
        return 'altered'
    if not valid_password_hash(rows['master_hash']):
        return 'damaged'
    return 'ok'


def _read_only(path) -> sqlite3.Connection:
    """A hardened connection that can neither create nor change anything."""
    db = sqlite3.connect(_read_only_uri(Path(path)), uri=True)
    try:
        _harden_connection(db)
    except Exception:
        db.close()
        raise
    return db


def _schema_problem_of(path) -> str | None:
    """schema_problem for a file that is not open. A file whose schema cannot
    be read reports that as the problem."""
    try:
        db = _read_only(path)
    except sqlite3.Error:
        return 'schema that cannot be read'
    try:
        return schema_problem(db)
    except sqlite3.Error:
        return 'schema that cannot be read'
    finally:
        db.close()


def _identity_rows(path) -> dict:
    """The master_hash and vault_format rows of a file that is not open, or {}
    when they cannot be read."""
    try:
        db = _read_only(path)
    except sqlite3.Error:
        return {}
    try:
        return dict(db.execute(
            "SELECT key, value FROM app_config "
            "WHERE key IN ('master_hash', 'vault_format')"
        ).fetchall())
    except sqlite3.Error:
        return {}
    finally:
        db.close()


def format_of(path):
    """The vault format recorded in a file, or None if it is not a vault.

    A vault is identified by carrying a master hash rather than by having the
    right tables: an interrupted init leaves every table and nothing anyone can
    log into. Callers compare against auth.VAULT_FORMAT themselves; importing
    auth here would be a cycle."""
    rows = _identity_rows(path)
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
    global permissions_hardened, folder_open
    if DB_PATH is None:
        raise NoVaultOpen('No vault is open.')

    # Read before the mkdir, which would otherwise make the answer always yes.
    parent_existed = DB_PATH.parent.exists()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    hardened = _harden_parent(parent_existed)
    # Before the schema goes in: sqlite3.connect would create the file under the
    # ambient umask, world-readable for the whole of its first write.
    _create_db_file()
    _create_journal_file()
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
    # path _create_db_file could not create would report a false failure. The
    # journal likewise, and only if it is there: one that could not be created
    # is SQLite's to create, and says nothing about the vault.
    journal = journal_path()
    journal_hardened = restrict_to_owner(journal) if journal.exists() else True
    permissions_hardened = restrict_to_owner(DB_PATH) and journal_hardened and hardened
    folder_open = folder_open_to_others(DB_PATH.parent)


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


# -----------------------------------------------------------------------------
# The rollback journal
#
# SQLite writes the pages a transaction is about to change into a journal file
# beside the vault: encrypted fields, but app_config in the clear. In its
# default mode it creates that file for every write and deletes it after, so
# each one is born with the folder's permissions, not the vault's. On Windows
# that is whatever the folder grants.
#
# TRUNCATE mode keeps the file and empties it after each transaction instead.
# So the app creates it once, owner-only, and SQLite reuses it from then on. An
# empty journal is not a crash journal, so having one there changes nothing.
# -----------------------------------------------------------------------------

JOURNAL_SUFFIX = '-journal'


def journal_path(path=None) -> Path:
    return Path(f"{path or DB_PATH}{JOURNAL_SUFFIX}")


def _create_journal_file():
    """Creates the journal owner-only if it is absent. One already there is
    left exactly as it is: it may be what a crash left, which SQLite must
    replay to keep the vault whole."""
    try:
        fd = os.open(journal_path(), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    except OSError:
        return
    os.close(fd)


def _keep_journal(db):
    """Puts a connection in TRUNCATE mode, so it reuses the journal file rather
    than deleting it. Per connection: SQLite does not store the mode."""
    db.execute("PRAGMA journal_mode = TRUNCATE")


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
        _harden_connection(db)
        if schema_problem(db):
            return False
        _keep_journal(db)
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
    into a confusing TypeError deep inside the caller.

    Checks the schema every time, before the caller runs anything, and raises
    ForeignSchemaError if it holds something the app never creates. Every time
    rather than once at open: a vault on a shared drive can have a trigger
    added while it is open, and it would run on the next save. The check is one
    read of a table of a handful of rows."""
    if DB_PATH is None:
        raise NoVaultOpen('No vault is open.')
    db = sqlite3.connect(DB_PATH)
    try:
        _harden_connection(db)
        problem = schema_problem(db)
    except Exception:
        db.close()
        raise
    if problem:
        db.close()
        raise ForeignSchemaError(foreign_schema_message(problem))
    db.row_factory = sqlite3.Row
    _keep_journal(db)
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

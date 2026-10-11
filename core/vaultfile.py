# -----------------------------------------------------------------------------
# core/vaultfile.py
# Which vault the app opens, and the ones it has opened before.
#
# The only AccountVault state that does not live inside a vault — it answers
# "which vault?" before any vault is open — so it sits in the per-user
# application data directory.
#
# Everything here is advisory. A registry that is absent, truncated, or full of
# nonsense leaves the app at the chooser screen; nothing in this file raises on
# bad input.
#
# It stores paths in the clear and nothing else. A vault called
# `divorce-accounts.vault` says something to anyone who reads the file, which is
# why the list can be turned off and cleared.
# -----------------------------------------------------------------------------

import json
import os
import threading
import time
from pathlib import Path

from core import database
from core.database import VAULT_SUFFIX

# How many vaults the list remembers.
MAX_RECENT = 8

_REGISTRY_NAME = 'vaults.json'
_VERSION = 1

# Set to point the app at one vault and keep it there — a separate profile, or
# the tests and tooling staying off the real vault.
ENV_OVERRIDE = 'ACCOUNTVAULT_DB'

# How long a status check may take before the vault is reported unavailable. A
# path on an unreachable share otherwise blocks for as long as the network takes
# to give up, with the window frozen.
PROBE_SECONDS = 3.0


def registry_path() -> Path:
    # Through the module, so redirecting the data directory redirects this too.
    return database.app_data_dir() / _REGISTRY_NAME


# -----------------------------------------------------------------------------
# Reading and writing the registry
# -----------------------------------------------------------------------------

def _read() -> dict:
    """The stored registry, or an empty one. Every failure lands on the same
    answer: the chooser screen a first launch sees."""
    try:
        with open(registry_path(), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> bool:
    """Stores the registry, owner-only. False if it could not be written — a
    read-only profile costs the recent list, never a launch."""
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created restricted rather than restricted afterwards.
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        return False
    database.restrict_to_owner(path)
    return True


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

def normalize(path) -> Path:
    """The absolute path, with symlinks and '..' taken out. strict=False, so a
    vault on an unplugged drive normalizes the same as when it was there."""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except OSError:
        return Path(path).expanduser()


def _key(path) -> str:
    """What makes two paths the same vault. Case-folded on Windows, which treats
    `Vault.vault` and `vault.VAULT` as one file."""
    return os.path.normcase(str(normalize(path)))


def same_file(first, second) -> bool:
    return _key(first) == _key(second)


def probe(path, timeout: float = PROBE_SECONDS) -> str:
    """database.inspect_vault, giving up after `timeout` seconds with
    'unavailable'. See probe_all."""
    return probe_all([path], timeout)[0]


def probe_all(paths, timeout: float = PROBE_SECONDS) -> list:
    """inspect_vault for each path, concurrently, within one shared deadline.

    Each check runs on a daemon thread: one stuck on a dead share is abandoned
    rather than joined, and cannot hold up the app exiting."""
    paths = list(paths)
    results = [None] * len(paths)

    def check(index, path):
        results[index] = database.inspect_vault(path)

    workers = [threading.Thread(target=check, args=(index, path), daemon=True)
               for index, path in enumerate(paths)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    return [status or 'unavailable' for status in results]


# -----------------------------------------------------------------------------
# The recent list
# -----------------------------------------------------------------------------

def recent() -> list:
    """The vaults most recently opened, newest first. Paths only; whether each
    is still there is asked when the list is drawn."""
    if not remembering():
        return []
    stored = _read().get('recent')
    if not isinstance(stored, list):
        return []

    paths, seen = [], set()
    for entry in stored[:MAX_RECENT * 2]:
        if not isinstance(entry, str) or not entry.strip():
            continue
        key = _key(entry)
        if key in seen:
            continue
        seen.add(key)
        paths.append(Path(entry))
    return paths[:MAX_RECENT]


def remember(path) -> bool:
    """Moves a vault to the front of the list, adding it if it is new. Called
    once a vault has actually opened, never when one is merely picked."""
    if not remembering():
        return False
    path = normalize(path)
    kept = [p for p in recent() if not same_file(p, path)]
    return _write({**_read(), 'version': _VERSION,
                   'recent': [str(path), *(str(p) for p in kept)][:MAX_RECENT]})


def forget(path) -> bool:
    """Drops one vault from the list. The file itself is never touched."""
    path = normalize(path)
    kept = [str(p) for p in recent() if not same_file(p, path)]
    return _write({**_read(), 'version': _VERSION, 'recent': kept})


def clear() -> bool:
    """Empties the list, leaving every vault where it is."""
    return _write({**_read(), 'version': _VERSION, 'recent': []})


def remembering() -> bool:
    """Whether the recent list is being kept at all."""
    return _read().get('remember_recent', True) is not False


def set_remembering(enabled: bool) -> bool:
    """Turns the recent list on or off. Turning it off clears the stored paths,
    which is the point: someone switching it off wants the file that names their
    vault gone, not hidden."""
    data = {**_read(), 'version': _VERSION, 'remember_recent': bool(enabled)}
    if not enabled:
        data['recent'] = []
    return _write(data)


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------

def startup_path(argv_path=None):
    """The vault to open on launch, or None to ask.

    In order: a path on the command line, the environment override, the vault
    opened last, then the two places vaults lived before anybody got to choose
    (adopted silently).

    The first three are returned whether or not the file is still there: a
    recorded vault that has moved is reported as missing rather than silently
    replaced by a different vault. The legacy locations are only returned if
    they hold a vault."""
    if argv_path:
        return Path(argv_path).expanduser()

    override = os.environ.get(ENV_OVERRIDE)
    if override:
        return Path(override).expanduser()

    last = recent()
    if last:
        return last[0]

    for candidate in (database.default_vault_path(),
                      database.app_data_dir() / database.LEGACY_DB_NAME,
                      database.LEGACY_APP_DIR_PATH):
        if database.inspect_vault(candidate) == 'ok':
            return candidate
    return None


def suggested_name() -> str:
    """The filename a brand-new vault is offered."""
    return 'accounts' + VAULT_SUFFIX


def describe(status: str, path) -> str:
    """A sentence for a status inspect_vault returned, worded to say what to do
    next."""
    name = Path(path).name
    return {
        'missing': f"{name} is no longer at that location. It may have been "
                   f"moved, renamed or deleted.",
        'unavailable': f"The drive holding {name} is not available. If it is on "
                       f"a USB stick or a network share, connect it and try again.",
        'empty': f"{name} is empty — there is no vault in it.",
        'not-sqlite': f"{name} is not an AccountVault vault.",
        'not-vault': f"{name} is a database, but not an AccountVault vault.",
        'unreadable': f"{name} could not be read. Another program may have it "
                      f"open, or the file may be damaged.",
        'newer': f"{name} was written by a newer version of AccountVault. "
                 f"Update the app to open it.",
    }.get(status, f"{name} could not be opened.")

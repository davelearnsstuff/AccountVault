# -----------------------------------------------------------------------------
# vaultfile.py
# Which vault the app opens, and the ones it has opened before.
#
# This is the only piece of AccountVault state that does not live inside a
# vault, and it cannot: reading it is how the app answers "which vault?", which
# is a question asked before any vault is open. So it sits beside them instead,
# in the per-user application data directory.
#
# Everything here is advisory. A registry that is absent, truncated, or full of
# nonsense has to leave the app at the chooser screen — never stop it starting,
# and never send it somewhere it should not write. Nothing in this file raises
# on bad input; it returns the empty answer and lets the user pick.
#
# What it stores is metadata in the clear: the paths, and nothing else. A vault
# called `divorce-accounts.vault` says something to anyone who reads this file,
# which is why there is a way to turn the list off and a way to clear it, and
# why no count, size or summary of a vault's contents is ever recorded here.
# -----------------------------------------------------------------------------

import json
import os
from pathlib import Path

import database
from database import VAULT_SUFFIX

# How many vaults the list remembers. Small on purpose: this is a way back to
# the two or three vaults somebody actually uses, not a history of everything
# they have ever opened.
MAX_RECENT = 8

_REGISTRY_NAME = 'vaults.json'
_VERSION = 1

# Set to point the app at one vault and keep it there. Ahead of everything
# below, because it is how a second profile is kept deliberately apart and how
# the test suite and the tooling stay off the real vault.
ENV_OVERRIDE = 'ACCOUNTVAULT_DB'


def registry_path() -> Path:
    # Through the module rather than a name bound at import, so that
    # redirecting the application data directory redirects this too.
    return database.app_data_dir() / _REGISTRY_NAME


# -----------------------------------------------------------------------------
# Reading and writing the registry
# -----------------------------------------------------------------------------

def _read() -> dict:
    """The stored registry, or an empty one.

    Every failure lands on the same answer. A registry that will not parse is
    not worth a message: the chooser screen it falls back to is the same screen
    a first-ever launch sees, and it is already the right thing to show."""
    try:
        with open(registry_path(), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> bool:
    """Stores the registry, owner-only. False if it could not be written.

    Best effort by design — a read-only profile or a full disk costs the user
    their recent list, which is a convenience, and must not cost them a launch.
    """
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created restricted rather than restricted afterwards, so the list of
        # where somebody keeps their vaults is never briefly world-readable.
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
    """The absolute path, with the symlinks and the '..' taken out.

    strict=False throughout: a vault on an unplugged drive still has to
    normalize to the same thing it did when it was there, or unplugging the
    drive would silently add a second entry for the same vault."""
    try:
        return Path(path).expanduser().resolve(strict=False)
    except OSError:
        return Path(path).expanduser()


def _key(path) -> str:
    """What makes two paths the same vault.

    Case-folded, because Windows treats `Vault.vault` and `vault.VAULT` as one
    file and a list that disagreed would show the same vault twice — once for
    each way the user happened to have typed it."""
    return os.path.normcase(str(normalize(path)))


def same_file(first, second) -> bool:
    return _key(first) == _key(second)


# -----------------------------------------------------------------------------
# The recent list
# -----------------------------------------------------------------------------

def recent() -> list:
    """The vaults most recently opened, newest first.

    Paths only, exactly as recorded. Whether each one is still there is
    database.inspect_vault's question, asked at the moment the list is drawn
    rather than cached here — a drive gets unplugged between launches."""
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
    """Moves a vault to the front of the list, adding it if it is new.

    Called once a vault has actually opened, never when one is merely picked: a
    file the user chose and then failed to unlock has not earned the top of the
    list, and one that turned out not to be a vault at all does not belong on it
    at any position."""
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
    """Whether the recent list is being kept at all.

    Off is a real setting rather than a synonym for "empty": somebody sharing a
    machine can want the app to stop writing down where their vault is without
    also wanting to answer the question again every launch — which is why
    turning it off clears the list rather than just hiding it."""
    return _read().get('remember_recent', True) is not False


def set_remembering(enabled: bool) -> bool:
    """Turns the recent list on or off, clearing it on the way out.

    Clearing on disable is the point of the switch. Leaving the stored paths in
    place and merely declining to show them would keep the file that names them
    on disk, which is the thing somebody turning this off is trying to avoid."""
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
    opened last, and then the two places vaults lived before anybody got to
    choose. The legacy pair are adopted without asking and without a message —
    nobody should have to go and find their own vault because the app changed
    how it stores the location.

    Returns a path that exists. A recorded vault whose file has since moved
    deliberately does *not* fall through to the next candidate: the user is told
    it is missing and offered the chooser, because silently opening a different
    vault instead is how somebody concludes their data is gone.
    """
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
    """A sentence for a status database.inspect_vault returned.

    Written to say what to do next rather than what went wrong — every one of
    these is read by somebody looking at a file picker, deciding whether to try
    again or pick something else."""
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

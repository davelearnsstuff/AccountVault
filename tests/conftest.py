# -----------------------------------------------------------------------------
# tests/conftest.py
# Shared fixtures and helpers.
#
# The suite is weighted towards services/ and auth.py — the paths where a bug
# destroys data the user cannot get back. Most of the Qt layer stays untested,
# because its failures are cosmetic and driving widgets costs more than it
# catches; the exceptions are the widgets that handle secrets or stored fields
# directly, which get the same treatment as the services. See test_ui.py.
# -----------------------------------------------------------------------------

import base64
import os
import sys
from pathlib import Path

import pytest
from argon2 import PasswordHasher

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set before any PySide6 import anywhere in the suite: Qt reads it when the
# platform plugin loads, and a test machine with no display otherwise aborts the
# whole process rather than failing a test. Lives here rather than in the one
# Qt test module that used to set it, because "before any PySide6 import" is not
# a property a single test file can hold on its own.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import auth              # noqa: E402
import database          # noqa: E402
import encryption        # noqa: E402
from services import accounts as accounts_service  # noqa: E402

MASTER = 'correct horse battery staple'
NEW_MASTER = 'a different long passphrase'


@pytest.fixture(autouse=True)
def reset_login_state():
    """Clears the in-memory half of the login rate limiter around every test.

    The lockout deadline and the escalation counter are module globals that
    outlive a database: without this, one test's failed logins lock the next
    one out, and the escalation counter makes the wait longer every time. The
    persisted half goes with the throwaway database and needs no help."""
    auth._lockout_deadline_monotonic = 0.0
    auth._lockout_count = 0
    yield
    auth._lockout_deadline_monotonic = 0.0
    auth._lockout_count = 0


@pytest.fixture
def cheap_argon2(monkeypatch):
    """Drops the KDF to its floor for the duration of one test.

    The real parameters (128 MiB, t=4) are deliberately expensive and cost about
    a second per derivation; a vault fixture pays that several times over and a
    rotation test three times more. Nothing here is testing the work factors —
    those are asserted directly, without this fixture, in test_rotation.py.
    """
    monkeypatch.setattr(encryption, 'ARGON2_TIME_COST', 1)
    monkeypatch.setattr(encryption, 'ARGON2_MEMORY_COST', 8)
    monkeypatch.setattr(encryption, 'ARGON2_PARALLELISM', 1)
    monkeypatch.setattr(
        auth, 'ph', PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    )
    # The historical set as well, and by name in each module that imported it:
    # `from encryption import LEGACY_KDF_PARAMS` binds a second reference that
    # patching encryption alone would not reach. A test standing a format-0
    # vault up derives under this one and should not pay 128 MiB to do it.
    cheap = {'t': 1, 'm': 8, 'p': 1}
    for module in (encryption, auth):
        monkeypatch.setattr(module, 'LEGACY_KDF_PARAMS', cheap)


@pytest.fixture
def vault(tmp_path, monkeypatch, cheap_argon2):
    """A fresh, unlocked vault backed by a throwaway database.

    database.DB_PATH is read inside connect() on every call, so pointing it at
    tmp_path is enough to keep the real vault untouched. restrict_to_owner is
    stubbed out: it shells to icacls on Windows and has nothing to do with what
    these tests check.

    The suffix is the real one. A vault is a .vault file now, and the backup
    tests build names from the stem, so a fixture that kept .db here would be
    asserting against filenames the app never produces.
    """
    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'vault.vault')
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: None)
    database.init_db()
    auth.setup_master_password(MASTER)
    yield
    # The key is a module global; leaving it set would leak into the next test.
    encryption.release_key()

    # A test that puts DB_PATH back — monkeypatch.undo() reverts this fixture's
    # patches too, since the monkeypatch instance is shared for the whole test —
    # would spend the rest of its run against the user's actual vault. Nothing
    # in the suite should ever be able to reach it, so the exit is checked
    # rather than trusted.
    #
    # Anywhere under tmp_path passes, not just the fixture's own file: opening
    # a second vault and switching between them is a thing the app does now, and
    # the tests for it have to be able to do it too. None passes as well —
    # closing the window closes the vault, and a closed vault is the one state
    # that cannot possibly be pointing at the real one.
    #
    # What is being checked has not changed: that nothing in the suite can end a
    # test pointing at somewhere real.
    assert database.DB_PATH is None or tmp_path in database.DB_PATH.parents, (
        'a test left database.DB_PATH outside tmp_path and may have touched the '
        'real vault')


# -- helpers ------------------------------------------------------------------

def make_account(**fields) -> dict:
    """Creates an account, defaulting the two fields every category needs."""
    flat = {'name': 'Test Account', 'category': 'Banking'}
    flat.update(fields)
    return accounts_service.create_account(flat)


def raw_column(account_id: int, column: str):
    """The value as it actually sits on disk, still encrypted."""
    with database.connect() as db:
        row = db.execute(
            f"SELECT {column} FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
    return row[column]


def decrypted_column(account_id: int, column: str) -> str:
    """What a stored column actually holds, opened the way the app opens it.

    Every field is bound to the column it lives in, so a bare decrypt() of what
    raw_column returns raises InvalidTag — that is the binding working, not the
    value being wrong. Tests that want the plaintext want this.
    """
    return encryption.decrypt(raw_column(account_id, column),
                              encryption.field_aad('accounts', column))


# Login rate-limiting bookkeeping. Excluded from snapshot_rows because it is
# *supposed* to change when an attempt fails: a wrong password bumps the
# persisted failure count, which is the control working, not a stray write.
# Everything else in app_config is still compared.
_VOLATILE_CONFIG_KEYS = ('failed_login_attempts', 'lockout_started', 'lockout_duration')


def snapshot_rows() -> list:
    """Every stored byte of both tables, for asserting a write did not happen.

    app_config is included apart from the rate-limit counters — see above."""
    with database.connect() as db:
        accounts = [tuple(r) for r in db.execute("SELECT * FROM accounts ORDER BY id")]
        history = [tuple(r) for r in
                   db.execute("SELECT * FROM balance_history ORDER BY id")]
        config = [tuple(r) for r in db.execute("SELECT * FROM app_config ORDER BY key")
                  if r['key'] not in _VOLATILE_CONFIG_KEYS]
    return [accounts, history, config]


def corrupt(token: str) -> str:
    """Flips a byte of a ciphertext so it still looks encrypted but will not open.

    Length is preserved, so looks_encrypted() still says yes — which is the whole
    point: this is the "encrypted and broken" case, not the "never encrypted" one.
    """
    raw = bytearray(base64.urlsafe_b64decode(token))
    raw[-1] ^= 0xFF
    return base64.urlsafe_b64encode(bytes(raw)).decode()


def corrupt_column(account_id: int, column: str) -> str:
    """Corrupts a stored column in place. Returns the value now on disk."""
    broken = corrupt(raw_column(account_id, column))
    with database.connect() as db:
        db.execute(
            f"UPDATE accounts SET {column} = ? WHERE id = ?", (broken, account_id)
        )
    return broken


# -- Qt ------------------------------------------------------------------------

def _destroy(window, qt_app):
    """Tears a MainWindow down so the next one does not pay for it.

    Three steps, and all three are needed. close() is not destruction;
    deleteLater only fires when the loop spins, which these tests never do; and
    the window is held in reference cycles that only a gc pass breaks. Leave any
    of them out and the windows accumulate on the session-scoped QApplication —
    each one still installed as an application-wide event filter and still
    re-polished by every stylesheet change — and window construction goes
    quadratic across the run.
    """
    import gc
    from PySide6.QtCore import QEvent

    window.close()
    window.deleteLater()
    del window
    qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
    gc.collect()


@pytest.fixture(scope='session')
def qt_app():
    """One QApplication for the session — Qt allows only one per process.

    Imported lazily so that a run touching none of the Qt tests never loads
    PySide6 at all.
    """
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])

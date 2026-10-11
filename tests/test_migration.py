# -----------------------------------------------------------------------------
# tests/test_migration.py
# The legacy plaintext migration that runs on every login.
#
# It walks every encrypted column in the vault and rewrites what it finds, which
# makes it the single most destructive thing that happens automatically. The
# cases that matter are the ones where it must do *nothing*: a field already
# encrypted, a field encrypted but broken, and a run that fails partway.
# -----------------------------------------------------------------------------

import sqlite3

import pytest

from core import auth
from core import database
from core import encryption
from conftest import (
    MASTER, corrupt_column, decrypted_column, make_account, raw_column,
    snapshot_rows,
)


def store_plaintext(account_id: int, column: str, value: str):
    """Writes a column in the clear, as a version that did not encrypt it would."""
    with database.connect() as db:
        db.execute(f"UPDATE accounts SET {column} = ? WHERE id = ?", (value, account_id))


# -- what it is for -------------------------------------------------------------

def test_a_plaintext_field_is_encrypted_in_place(vault):
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')
    assert raw_column(account['id'], 'notes') == 'readable on disk'

    auth.migrate_plaintext_fields()

    on_disk = raw_column(account['id'], 'notes')
    assert on_disk != 'readable on disk'
    assert encryption.looks_encrypted(on_disk)
    assert decrypted_column(account['id'], 'notes') == 'readable on disk'


def test_every_encrypted_column_is_walked(vault):
    account = make_account(name='Legacy')
    for column in ('name', 'username', 'password', 'website', 'notes'):
        store_plaintext(account['id'], column, f'plain {column}')

    auth.migrate_plaintext_fields()

    for column in ('name', 'username', 'password', 'website', 'notes'):
        assert decrypted_column(account['id'], column) == f'plain {column}'


def test_balance_history_is_migrated_too(vault):
    from services import accounts as accounts_service  # noqa: F401 — used below
    account = make_account(name='Legacy', current_balance='100')
    accounts_service.add_balance_entry(account['id'], '250', '2024-01-15')

    with database.connect() as db:
        entry_id = db.execute("SELECT id FROM balance_history").fetchone()['id']
        db.execute("UPDATE balance_history SET balance = ? WHERE id = ?", ('250', entry_id))

    auth.migrate_plaintext_fields()

    with database.connect() as db:
        stored = db.execute("SELECT balance FROM balance_history WHERE id = ?",
                            (entry_id,)).fetchone()['balance']
    assert encryption.decrypt(stored, accounts_service.BALANCE_AAD) == '250'


# -- what it must never touch ---------------------------------------------------

def test_running_it_twice_changes_nothing_the_second_time(vault):
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')

    auth.migrate_plaintext_fields()
    once = snapshot_rows()
    auth.migrate_plaintext_fields()

    assert snapshot_rows() == once


def test_an_already_encrypted_vault_is_left_exactly_as_it_was(vault):
    make_account(name='Already Encrypted', notes='a note')
    before = snapshot_rows()

    auth.migrate_plaintext_fields()

    assert snapshot_rows() == before


def test_a_broken_ciphertext_is_not_wrapped_a_second_time(vault):
    """The one that would destroy data silently, on every login. The check asks
    about the value's *shape*, not whether it decrypts — encrypting a field that
    is already ciphertext but damaged buries the original beyond recovery."""
    account = make_account(name='Broken', notes='a note')
    broken = corrupt_column(account['id'], 'notes')

    auth.migrate_plaintext_fields()

    assert raw_column(account['id'], 'notes') == broken


def test_an_empty_field_is_left_empty_rather_than_encrypted(vault):
    account = make_account(name='Sparse')
    store_plaintext(account['id'], 'notes', '')

    auth.migrate_plaintext_fields()

    assert raw_column(account['id'], 'notes') == ''


# -- failure must not cost a login ---------------------------------------------

def test_a_locked_vault_raises_rather_than_writing_nonsense(vault):
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')

    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        auth.migrate_plaintext_fields()


def test_a_locked_vault_is_reported_even_through_the_quiet_wrapper(vault):
    """Everything else is swallowed, because by then the user is already in and
    no caller of verify_master_password expects an exception on the way out. A
    missing key is different: it is a fault in the caller, not a stubborn row."""
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')

    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        auth._migrate_plaintext_fields_quietly()


def test_a_database_error_does_not_escape_the_quiet_wrapper(vault, monkeypatch):
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')

    def broken_connect(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(auth, 'connect', broken_connect)
    auth._migrate_plaintext_fields_quietly()   # must not raise

    assert raw_column(account['id'], 'notes') == 'readable on disk'


def test_a_failed_pass_is_retried_on_the_next_login(vault):
    """Idempotent by design, so a table that could not be written this time is
    simply picked up again — no state records that it was skipped.

    The failure is injected by hand and put back in a finally, not with
    monkeypatch.undo(): undo() is not selective, and would also revert the
    vault fixture's redirect of database.DB_PATH — pointing the second half of
    this test at the user's real vault."""
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')

    def broken_connect(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    real_connect = auth.connect
    try:
        auth.connect = broken_connect
        auth.migrate_plaintext_fields()
    finally:
        auth.connect = real_connect

    auth.migrate_plaintext_fields()
    assert decrypted_column(account['id'], 'notes') == 'readable on disk'


def test_logging_in_migrates_without_being_asked(vault):
    """The whole point: an upgrade from a version that encrypted fewer fields
    repairs itself the next time the user opens the vault."""
    account = make_account(name='Legacy', notes='readable on disk')
    store_plaintext(account['id'], 'notes', 'readable on disk')

    auth.lock_app()
    assert auth.verify_master_password(MASTER) is True

    assert decrypted_column(account['id'], 'notes') == 'readable on disk'


# -- the hardened-permissions flag has to report what is true ------------------

def test_permissions_hardened_reports_the_file_as_it_ends_up(tmp_path, monkeypatch):
    """init_db restricted the database file twice, before and after the schema.
    _create_db_file returns quietly when it cannot create the file, so on that
    path the earlier call ran against a path that did not exist yet, answered
    False, and poisoned the flag for good — main.py then warned the user about
    a vault that had in fact ended up owner-only."""
    from core import database

    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'vault.db')
    monkeypatch.setattr(database, '_create_db_file', lambda: None)  # the failure path
    monkeypatch.setattr(database, 'restrict_to_owner',
                        lambda path: path.exists())

    database.init_db()

    assert database.permissions_hardened is True


def test_permissions_hardened_is_false_when_the_file_cannot_be_restricted(
        tmp_path, monkeypatch):
    """The other direction: the warning still fires when it should."""
    from core import database

    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'vault.db')
    monkeypatch.setattr(database, 'restrict_to_owner',
                        lambda path: path.is_dir())   # directory yes, file no

    database.init_db()

    assert database.permissions_hardened is False

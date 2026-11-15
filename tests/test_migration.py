# -----------------------------------------------------------------------------
# tests/test_migration.py
# Plaintext in a vault that is already at the current format.
#
# Plaintext is legitimate only in a format-0 vault, and the login upgrade
# encrypts it (see test_vault_format). Anywhere else it was written by someone
# who could change the file but not open it. These tests hold the line that it
# is never shown as the value and never encrypted, because once encrypted under
# the user's key a forged website or routing number is indistinguishable from
# the user's own.
# -----------------------------------------------------------------------------

import json
import sqlite3

import pytest

from core import auth
from core import database
from core import encryption
from conftest import (
    MASTER, NEW_MASTER, make_account, raw_column, snapshot_rows,
)
from services import accounts as accounts_service


def store_plaintext(account_id: int, column: str, value: str):
    """Writes a column in the clear, as someone editing the file would."""
    with database.connect() as db:
        db.execute(f"UPDATE accounts SET {column} = ? WHERE id = ?", (value, account_id))
    accounts_service.invalidate_cache()


# -- it is not believed ---------------------------------------------------------

def test_a_plaintext_field_reads_as_unreadable(vault):
    account = make_account(name='Bank', website='bank.example')
    store_plaintext(account['id'], 'website', 'https://evil.example')

    assert encryption.is_unreadable(accounts_service.get_account(account['id'])['website'])
    listed = {a['id']: a for a in accounts_service.list_accounts()}
    assert encryption.is_unreadable(listed[account['id']]['website'])


def test_plaintext_extra_data_contributes_nothing(vault):
    """The routing-number case: a plaintext blob is not read as fields."""
    account = make_account(name='Bank', category='Banking')
    store_plaintext(account['id'], 'extra_data',
                    json.dumps({'routing_number': '999999999'}))

    assert 'routing_number' not in accounts_service.get_account(account['id'])


def test_a_forged_row_shows_no_forged_values(vault):
    with database.connect() as db:
        db.execute("INSERT INTO accounts (category, name, website) VALUES (?, ?, ?)",
                   ('Banking', 'Chase', 'https://evil.example'))
    accounts_service.invalidate_cache()

    (forged,) = accounts_service.list_accounts()
    assert encryption.is_unreadable(forged['name'])
    assert encryption.is_unreadable(forged['website'])


def test_a_locked_vault_raises_rather_than_reading_plaintext(vault):
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        encryption.safe_decrypt('https://evil.example')


# -- it is never encrypted ------------------------------------------------------

def test_logging_in_leaves_it_as_it_was(vault):
    """The laundering this exists to stop: the login used to encrypt plaintext
    under the real key, after which it decrypted as genuine."""
    account = make_account(name='Bank')
    store_plaintext(account['id'], 'website', 'https://evil.example')
    before = snapshot_rows()

    auth.lock_app()
    assert auth.verify_master_password(MASTER) is True

    assert snapshot_rows() == before
    assert raw_column(account['id'], 'website') == 'https://evil.example'


def test_a_password_change_refuses_rather_than_encrypting_it(vault):
    """Rotation re-encrypts every field, so it would launder the value too. It
    refuses, as it does for a field that will not decrypt, and changes nothing."""
    account = make_account(name='Bank')
    store_plaintext(account['id'], 'notes', 'call 555-0100 for support')
    before = snapshot_rows()

    with pytest.raises(auth.ReencryptionError):
        auth.change_master_password(MASTER, NEW_MASTER)

    assert snapshot_rows() == before


def test_a_work_factor_rekey_steps_over_it(vault, monkeypatch):
    """The login-time re-key skips what it cannot vouch for rather than locking
    the owner out, and the value stays plaintext, still unreadable."""
    account = make_account(name='Bank', password='s3cret')
    store_plaintext(account['id'], 'notes', 'forged')
    auth.lock_app()

    monkeypatch.setattr(encryption, 'ARGON2_TIME_COST', 2)
    assert auth.verify_master_password(MASTER) is True

    assert raw_column(account['id'], 'notes') == 'forged'
    fresh = accounts_service.get_account(account['id'])
    assert fresh['password'] == 's3cret'
    assert encryption.is_unreadable(fresh['notes'])


# -- it is counted, so the user can be told -------------------------------------

def test_a_clean_vault_counts_nothing(vault):
    make_account(name='Bank', notes='a note', current_balance='100')
    assert auth.foreign_field_count() == 0


def test_every_encrypted_column_is_counted(vault):
    account = make_account(name='Bank', current_balance='100')
    for column in ('name', 'username', 'password', 'website', 'notes', 'extra_data'):
        store_plaintext(account['id'], column, f'plain {column}')
    with database.connect() as db:
        db.execute("UPDATE balance_history SET balance = ?", ('250',))

    assert auth.foreign_field_count() == 7


def test_an_empty_field_is_not_counted(vault):
    account = make_account(name='Sparse')
    store_plaintext(account['id'], 'notes', '')
    assert auth.foreign_field_count() == 0


def test_an_unreadable_database_counts_nothing(vault, monkeypatch):
    def broken_connect(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(auth, 'connect', broken_connect)
    assert auth.foreign_field_count() == 0


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

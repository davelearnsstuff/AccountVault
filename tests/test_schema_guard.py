# -----------------------------------------------------------------------------
# tests/test_schema_guard.py
# A vault whose schema holds something the app never creates is refused.
#
# SQLite runs a file's triggers on every write, so one planted by anyone who
# could write the file once would act on every save after, survive a password
# change, and be copied into every backup. These tests plant one the way an
# outsider would, with a plain sqlite3 connection, and check that nothing in the
# app will open, use or copy the vault afterwards.
# -----------------------------------------------------------------------------

import sqlite3

import pytest

from core import auth
from core import database
from conftest import MASTER, make_account, raw_column, snapshot_rows
from services import accounts as accounts_service
from services import backups as backups_service

_TRIGGER = ("CREATE TRIGGER put_it_back AFTER UPDATE ON accounts BEGIN "
            "UPDATE accounts SET notes = 'forged' WHERE id = NEW.id; END")


def plant(sql: str, path=None):
    """Runs sql against the vault file directly, as another program would."""
    db = sqlite3.connect(path or database.DB_PATH)
    try:
        db.execute(sql)
        db.commit()
    finally:
        db.close()


ALTERATIONS = [
    pytest.param(_TRIGGER, id='trigger'),
    pytest.param("CREATE VIEW everything AS SELECT * FROM accounts", id='view'),
    pytest.param("CREATE TABLE stash (value TEXT)", id='table'),
    pytest.param("CREATE INDEX by_name ON accounts (name)", id='index'),
]


# -- what a vault is allowed to hold --------------------------------------------

def test_a_vault_the_app_built_passes(vault):
    """The baseline every other test depends on: the app's own schema,
    AUTOINCREMENT's sqlite_sequence and app_config's key index included."""
    make_account(name='Bank', current_balance='100')
    with database.connect() as db:
        assert database.schema_problem(db) is None
    assert database.inspect_vault(database.DB_PATH) == 'ok'


def test_the_first_designs_tables_are_tolerated(vault):
    """A vault from before the move to one table may still carry them."""
    plant("CREATE TABLE banking (id INTEGER PRIMARY KEY, name TEXT)")
    assert database.inspect_vault(database.DB_PATH) == 'ok'


def test_every_connection_is_hardened(vault):
    with database.connect() as db:
        assert db.execute("PRAGMA trusted_schema").fetchone()[0] == 0


# -- what is refused --------------------------------------------------------------

@pytest.mark.parametrize('sql', ALTERATIONS)
def test_an_altered_vault_is_reported_as_altered(vault, sql):
    plant(sql)
    assert database.inspect_vault(database.DB_PATH) == 'altered'


@pytest.mark.parametrize('sql', ALTERATIONS)
def test_an_altered_vault_will_not_connect(vault, sql):
    plant(sql)
    with pytest.raises(database.ForeignSchemaError):
        with database.connect():
            pass


def test_an_altered_vault_will_not_open(vault, tmp_path):
    path = tmp_path / 'other.vault'
    make_account(name='Bank')
    backups_service.copy_vault(path)
    plant(_TRIGGER, path)
    before = database.DB_PATH

    with pytest.raises(database.ForeignSchemaError):
        database.open_vault(path, create=False)

    assert database.DB_PATH == before


def test_a_trigger_added_while_open_never_runs(vault):
    """The shared-drive case: the vault passed when it opened, then gained a
    trigger. The next save is refused before the trigger can fire."""
    account = make_account(name='Bank', notes='real note')
    before = raw_column(account['id'], 'notes')
    plant(_TRIGGER)

    with pytest.raises(database.ForeignSchemaError):
        accounts_service.update_account(account['id'], {
            'name': 'Bank', 'category': 'Banking', 'notes': 'edited'})

    db = sqlite3.connect(database.DB_PATH)
    try:
        notes = db.execute("SELECT notes FROM accounts WHERE id = ?",
                           (account['id'],)).fetchone()[0]
    finally:
        db.close()
    assert notes == before


def test_logging_in_to_an_altered_vault_is_refused_before_the_key(vault):
    make_account(name='Bank')
    auth.lock_app()
    plant(_TRIGGER)

    with pytest.raises(database.ForeignSchemaError):
        auth.verify_master_password(MASTER)

    assert auth.check_session() is False


def test_an_altered_vault_is_not_copied(vault, tmp_path):
    """A trigger in a backup would come back with any restore."""
    plant(_TRIGGER)
    destination = tmp_path / 'copy.vault'

    with pytest.raises(backups_service.BackupError, match='another program'):
        backups_service.copy_vault(destination)

    assert not destination.exists()
    assert not list(tmp_path.glob('.avbackup-*'))


def test_the_refusal_names_what_was_found(vault):
    plant(_TRIGGER)
    with pytest.raises(database.ForeignSchemaError, match='trigger "put_it_back"'):
        with database.connect():
            pass


def test_vacuum_does_not_run_on_an_altered_vault(vault):
    plant(_TRIGGER)
    assert database.vacuum() is False


def test_an_unaltered_vault_is_untouched_by_the_check(vault):
    """Reading the schema must not write anything."""
    make_account(name='Bank', notes='a note')
    before = snapshot_rows()
    database.inspect_vault(database.DB_PATH)
    with database.connect():
        pass
    assert snapshot_rows() == before

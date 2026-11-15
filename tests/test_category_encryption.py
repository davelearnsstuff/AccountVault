# -----------------------------------------------------------------------------
# tests/test_category_encryption.py
# Format 2: the category column is encrypted.
#
# Custom category names are vault content, and the count of accounts per
# category says what kinds of accounts exist. Format 1 stored the column in the
# clear, so the upgrade has to encrypt exactly that column's plaintext, and
# nothing else's, at the next login.
# -----------------------------------------------------------------------------

import pytest

from core import auth
from core import database
from core import encryption
from conftest import (
    MASTER, decrypted_column, make_account, raw_column, rewrite_as_format,
)
from services import accounts as accounts_service
from services import dashboard as dashboard_service


# -- written encrypted --------------------------------------------------------------

def test_a_new_account_stores_its_category_encrypted(vault):
    account = make_account(name='Bank', category='Hobby Club')
    assert raw_column(account['id'], 'category') != 'Hobby Club'
    assert decrypted_column(account['id'], 'category') == 'Hobby Club'
    assert accounts_service.get_account(account['id'])['category'] == 'Hobby Club'


def test_an_edit_stores_its_category_encrypted(vault):
    account = make_account(name='Bank', category='Banking')
    edit = dict(accounts_service.get_account(account['id']), category='Hobby Club')
    accounts_service.update_account(account['id'], edit)
    assert decrypted_column(account['id'], 'category') == 'Hobby Club'


def test_a_bulk_move_encrypts_each_row_separately(vault):
    """One token on every row would show which accounts share a category."""
    first = make_account(name='One', category='Banking')
    second = make_account(name='Two', category='Banking')

    assert accounts_service.set_category([first['id'], second['id']], 'Hobby Club') == 2

    tokens = {raw_column(first['id'], 'category'), raw_column(second['id'], 'category')}
    assert len(tokens) == 2
    assert {decrypted_column(a['id'], 'category') for a in (first, second)} == {'Hobby Club'}


def test_an_unreadable_category_survives_an_edit(vault):
    """The form sends the placeholder back; encrypting it would lose the real
    category for good."""
    account = make_account(name='Bank', category='Banking')
    broken = encryption.encrypt('Banking', b'some other binding')
    with database.connect() as db:
        db.execute("UPDATE accounts SET category = ? WHERE id = ?", (broken, account['id']))
    accounts_service.invalidate_cache()

    edit = dict(accounts_service.get_account(account['id']))
    assert encryption.is_unreadable(edit['category'])
    accounts_service.update_account(account['id'], dict(edit, notes='edited'))

    assert raw_column(account['id'], 'category') == broken


def test_the_list_is_still_ordered_by_category_then_id(vault):
    late = make_account(name='Z', category='Websites')
    early = make_account(name='A', category='Banking')
    other = make_account(name='B', category='Banking')

    assert [a['id'] for a in accounts_service.list_accounts()] == \
        [early['id'], other['id'], late['id']]


def test_the_dashboard_reads_the_encrypted_category(vault):
    make_account(name='Card', category='Credit Cards', current_balance=500)
    assert dashboard_service.monthly_summary()['net_worth'][-1] == -500.0


# -- the upgrade from format 1 ------------------------------------------------------

def test_a_format_1_vault_has_its_categories_encrypted_at_login(vault):
    account = make_account(name='Bank', category='Hobby Club', password='s3cret',
                           current_balance='100')
    rewrite_as_format(1)
    assert raw_column(account['id'], 'category') == 'Hobby Club'

    assert auth.verify_master_password(MASTER) is True

    assert database.read_config(auth._FORMAT_KEY) == str(auth.VAULT_FORMAT)
    assert decrypted_column(account['id'], 'category') == 'Hobby Club'
    fresh = accounts_service.get_account(account['id'])
    assert fresh['password'] == 's3cret'
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '100'
    assert auth.foreign_field_count() == 0


def test_the_upgrade_encrypts_only_the_category(vault):
    """Format 1 encrypted every other column, so plaintext there was written
    without the key, and the upgrade must not launder it."""
    account = make_account(name='Bank', category='Banking')
    rewrite_as_format(1)
    with database.connect() as db:
        db.execute("UPDATE accounts SET website = ? WHERE id = ?",
                   ('https://evil.example', account['id']))

    assert auth.verify_master_password(MASTER) is True

    assert raw_column(account['id'], 'website') == 'https://evil.example'
    assert encryption.is_unreadable(accounts_service.get_account(account['id'])['website'])
    assert auth.foreign_field_count() == 1


def test_a_plaintext_category_in_a_format_2_vault_is_not_trusted(vault):
    account = make_account(name='Bank', category='Banking')
    with database.connect() as db:
        db.execute("UPDATE accounts SET category = ? WHERE id = ?",
                   ('Planted', account['id']))
    accounts_service.invalidate_cache()

    assert encryption.is_unreadable(accounts_service.get_account(account['id'])['category'])
    assert auth.foreign_field_count() == 1

    auth.lock_app()
    assert auth.verify_master_password(MASTER) is True
    assert raw_column(account['id'], 'category') == 'Planted'


def test_claiming_format_1_does_not_excuse_a_missing_key_check(vault):
    """The key check arrived with format 1. A vault claiming format 1 without
    one has had it removed, and waving it through would skip the check."""
    make_account(name='Bank')
    database.write_config(auth._FORMAT_KEY, '1')
    with database.connect() as db:
        db.execute("DELETE FROM app_config WHERE key = ?", (auth._CANARY_KEY,))
    auth.lock_app()

    with pytest.raises(auth.VaultConfigError):
        auth.verify_master_password(MASTER)
    assert encryption.is_unlocked() is False

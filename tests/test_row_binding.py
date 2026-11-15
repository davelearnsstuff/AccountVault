# -----------------------------------------------------------------------------
# tests/test_row_binding.py
# Format 3: every field bound to its row, and padded.
#
# Binding to the column stopped a note being read as a password. Binding to the
# row stops one account's password, website or routing number being read as
# another's, and a balance entry moving to a different account or being copied
# as a new one. Padding stops the file showing how long each value is.
# -----------------------------------------------------------------------------

import base64

import pytest

from core import auth
from core import database
from core import encryption
from conftest import (
    MASTER, NEW_MASTER, decrypted_column, make_account, raw_column,
    rewrite_as_format,
)
from services import accounts as accounts_service


def token_size(token: str) -> int:
    return len(base64.urlsafe_b64decode(token))


def swap(column: str, first: int, second: int):
    """Exchanges two rows' stored values, as someone editing the file would."""
    a, b = raw_column(first, column), raw_column(second, column)
    with database.connect() as db:
        db.execute(f"UPDATE accounts SET {column} = ? WHERE id = ?", (b, first))
        db.execute(f"UPDATE accounts SET {column} = ? WHERE id = ?", (a, second))
    accounts_service.invalidate_cache()


def history_rows():
    with database.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM balance_history ORDER BY id")]


# -- padding ----------------------------------------------------------------------

@pytest.mark.parametrize('size, padded', [(0, 64), (1, 64), (60, 64), (61, 128), (124, 128),
                                          (125, 192)])
def test_padding_rounds_up_to_whole_blocks(size, padded):
    data = b'x' * size
    assert len(encryption.pad(data)) == padded
    assert encryption.unpad(encryption.pad(data)) == data


@pytest.mark.parametrize('bad', [b'', b'\x00' * 63, b'\x00\x00\x00\x41' + b'\x00' * 60,
                                 b'\x00\x00\x00\x01a' + b'\x01' + b'\x00' * 58])
def test_unpadding_refuses_what_padding_never_writes(bad):
    with pytest.raises(ValueError):
        encryption.unpad(bad)


def test_short_and_long_passwords_look_the_same_on_disk(vault):
    short = make_account(name='Short', password='a')
    long = make_account(name='Long', password='x' * 50)
    assert token_size(raw_column(short['id'], 'password')) == \
        token_size(raw_column(long['id'], 'password'))


def test_small_and_large_balances_look_the_same_on_disk(vault):
    make_account(name='Small', current_balance='12')
    make_account(name='Large', current_balance='1234567.89')
    sizes = {token_size(row['balance']) for row in history_rows()}
    assert len(sizes) == 1


# -- binding to the row -----------------------------------------------------------

@pytest.mark.parametrize('column', ['password', 'website', 'name', 'category'])
def test_swapped_fields_do_not_open(vault, column):
    first = make_account(name='Bank', category='Banking', password='bank-pw',
                         website='bank.example')
    second = make_account(name='Forum', category='Websites', password='forum-pw',
                          website='forum.example')
    swap(column, first['id'], second['id'])

    for account in (first, second):
        assert encryption.is_unreadable(accounts_service.get_account(account['id'])[column])


def test_swapped_category_fields_do_not_open(vault):
    """extra_data holds routing and account numbers."""
    first = make_account(name='Mine', category='Banking', routing_number='111000025')
    second = make_account(name='Other', category='Banking', routing_number='999999999')
    assert accounts_service.get_account(first['id'])['routing_number'] == '111000025'
    swap('extra_data', first['id'], second['id'])

    assert 'routing_number' not in accounts_service.get_account(first['id'])
    assert 'routing_number' not in accounts_service.get_account(second['id'])


def test_a_balance_moved_to_another_account_does_not_open(vault):
    source = make_account(name='Source', current_balance='500')
    target = make_account(name='Target')
    with database.connect() as db:
        db.execute("UPDATE balance_history SET account_id = ? WHERE account_id = ?",
                   (target['id'], source['id']))

    (entry,) = accounts_service.list_balance_history(target['id'])
    assert encryption.is_unreadable(entry['balance'])


def test_a_balance_copied_as_a_new_entry_does_not_open(vault):
    account = make_account(name='Bank', current_balance='500')
    (original,) = history_rows()
    with database.connect() as db:
        db.execute("INSERT INTO balance_history (account_id, balance, recorded_at) "
                   "VALUES (?, ?, ?)",
                   (account['id'], original['balance'], '2020-01-01'))

    balances = [entry['balance'] for entry in accounts_service.list_balance_history(account['id'])]
    assert '500' in balances
    assert sum(encryption.is_unreadable(b) for b in balances) == 1


def test_undo_still_restores_a_deleted_account(vault):
    """Restore puts the row back under its own id, so its fields still open."""
    account = make_account(name='Bank', password='s3cret', current_balance='100')
    accounts_service.set_closed([account['id']], True)
    token = accounts_service.delete_account(account['id'])
    accounts_service.restore_account(token)

    fresh = accounts_service.get_account(account['id'])
    assert fresh['password'] == 's3cret'
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '100'


# -- the upgrade --------------------------------------------------------------------

@pytest.mark.parametrize('version', [1, 2])
def test_an_older_vault_is_bound_and_padded_at_login(vault, version):
    account = make_account(name='Bank', category='Hobby Club', password='s3cret',
                           routing_number='111000025', current_balance='100')
    rewrite_as_format(version)

    assert auth.verify_master_password(MASTER) is True

    assert database.read_config(auth._FORMAT_KEY) == str(auth.VAULT_FORMAT)
    assert decrypted_column(account['id'], 'password') == 's3cret'
    assert len(base64.urlsafe_b64decode(raw_column(account['id'], 'password'))) == 12 + 64 + 16
    fresh = accounts_service.get_account(account['id'])
    assert (fresh['category'], fresh['routing_number']) == ('Hobby Club', '111000025')
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '100'
    assert auth.foreign_field_count() == 0


def test_a_password_change_does_not_pad_twice(vault):
    account = make_account(name='Bank', password='s3cret', current_balance='100')
    before = token_size(raw_column(account['id'], 'password'))

    assert auth.change_master_password(MASTER, NEW_MASTER) is True

    assert token_size(raw_column(account['id'], 'password')) == before
    assert decrypted_column(account['id'], 'password') == 's3cret'
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '100'


def test_a_new_row_is_written_whole_or_not_at_all(vault):
    """A row goes in empty and is filled in, in one transaction: a value too
    long to store must leave no empty row behind."""
    with pytest.raises(ValueError):
        make_account(name='Bank', notes='x' * 10_001)
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0

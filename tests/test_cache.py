# -----------------------------------------------------------------------------
# tests/test_cache.py
# The decrypted account cache in services/accounts.py.
#
# Two things matter here and they pull in opposite directions: the cache has to
# be dropped by every write, or the UI shows stale data; and it has to be dropped
# on lock, or plaintext outlives the key that opened it. The tests below are
# mostly about the second kind of failure, because a stale read is visible and a
# surviving cache is not.
# -----------------------------------------------------------------------------

import pytest

from core import auth
from core import encryption
from services import accounts as accounts_service

from conftest import MASTER, make_account


def names():
    return sorted(a['name'] for a in accounts_service.list_accounts())


def test_cache_warms_and_returns_the_same_rows(vault):
    make_account(name='Alpha')
    make_account(name='Beta')

    assert not accounts_service.cache_is_warm()   # the write above dropped it
    first = accounts_service.list_accounts()
    assert accounts_service.cache_is_warm()
    second = accounts_service.list_accounts()

    assert [a['name'] for a in first] == [a['name'] for a in second]


def test_callers_cannot_mutate_the_cache(vault):
    make_account(name='Alpha')

    handed_out = accounts_service.list_accounts()
    # Exactly what ui/main_window.py does to every row it renders.
    handed_out[0]['_category'] = 'tampered'
    handed_out[0]['name'] = 'Renamed'

    fresh = accounts_service.list_accounts()
    assert fresh[0]['name'] == 'Alpha'
    assert '_category' not in fresh[0]


@pytest.mark.parametrize('write', [
    lambda a: make_account(name='Added'),
    lambda a: accounts_service.update_account(a['id'], {**a, 'name': 'Renamed'}),
    lambda a: accounts_service.touch_account(a['id']),
    lambda a: accounts_service.delete_account(a['id']),
    lambda a: accounts_service.set_closed([a['id']], True),
    lambda a: accounts_service.set_category([a['id']], 'Loans'),
    lambda a: accounts_service.add_balance_entry(a['id'], '10', '2020-01-01'),
])
def test_every_write_drops_the_cache(vault, write):
    account = make_account(name='Alpha', category='Banking')
    accounts_service.list_accounts()
    assert accounts_service.cache_is_warm()

    write(account)
    assert not accounts_service.cache_is_warm()


def test_balance_entry_edits_drop_the_cache(vault):
    account = make_account(name='Alpha', current_balance=5)
    accounts_service.add_balance_entry(account['id'], '10', '2020-01-01')
    entry = accounts_service.list_balance_history(account['id'])[0]

    accounts_service.list_accounts()
    accounts_service.update_balance_entry(account['id'], entry['id'], '20', '2020-01-02')
    assert not accounts_service.cache_is_warm()

    accounts_service.list_accounts()
    accounts_service.delete_balance_entry(account['id'], entry['id'])
    assert not accounts_service.cache_is_warm()


def test_writes_are_visible_to_the_next_read(vault):
    make_account(name='Alpha')
    assert names() == ['Alpha']

    make_account(name='Beta')
    assert names() == ['Alpha', 'Beta']

    accounts_service.set_category([accounts_service.list_accounts()[0]['id']], 'Loans')
    assert {a['category'] for a in accounts_service.list_accounts()} == {'Loans', 'Banking'}


def test_lock_drops_the_plaintext(vault):
    """The security-relevant one: decrypted rows must not survive the key."""
    make_account(name='Alpha')
    accounts_service.list_accounts()
    assert accounts_service.cache_is_warm()

    auth.lock_app()
    assert not accounts_service.cache_is_warm()


def test_locked_vault_is_refused_even_with_a_warm_cache(vault):
    """require_session comes before the cache, so a lock cannot be read around."""
    make_account(name='Alpha')
    accounts_service.list_accounts()

    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        accounts_service.list_accounts()


def test_password_change_drops_the_cache(vault):
    """A rotation re-keys every row; anything cached under the old key is stale."""
    make_account(name='Alpha')
    accounts_service.list_accounts()
    assert accounts_service.cache_is_warm()

    auth.change_master_password(MASTER, 'a different long passphrase')
    assert not accounts_service.cache_is_warm()
    assert names() == ['Alpha']


def test_category_filter_matches_the_full_listing(vault):
    make_account(name='Bank One', category='Banking')
    make_account(name='Bank Two', category='Banking')
    make_account(name='A Loan', category='Loans')

    filtered = accounts_service.list_accounts('Banking')
    assert [a['name'] for a in filtered] == ['Bank One', 'Bank Two']
    assert all(a['category'] == 'Banking' for a in filtered)
    assert accounts_service.list_accounts('Nothing Here') == []

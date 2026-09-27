# -----------------------------------------------------------------------------
# tests/test_balance_history.py
# _sync_current_balance and the backdating path in update_account.
#
# History is a way of updating the balance, not only a log of it, so an entry
# can move what the account itself holds. These cover which entries do that and
# which must not.
# -----------------------------------------------------------------------------

from datetime import date, timedelta

import pytest

from conftest import make_account
from services import accounts as accounts_service


def today() -> str:
    return date.today().isoformat()


def test_newer_entry_becomes_the_current_balance(vault):
    account = make_account(current_balance=100)

    accounts_service.add_balance_entry(account['id'], '250', today())

    assert accounts_service.get_account(account['id'])['current_balance'] == 250


def test_older_entry_only_fills_in_the_past(vault):
    """An entry behind an existing one says what the balance *was*."""
    account = make_account(current_balance=100)

    accounts_service.add_balance_entry(account['id'], '999', '2020-01-01')

    assert accounts_service.get_account(account['id'])['current_balance'] == 100


def test_future_dated_entry_is_stored_but_not_adopted(vault):
    """A balance from the future is a typo; adopting it would pin the account to
    it until someone deleted the entry."""
    account = make_account(current_balance=100)
    future = (date.today() + timedelta(days=30)).isoformat()

    accounts_service.add_balance_entry(account['id'], '999', future)

    assert accounts_service.get_account(account['id'])['current_balance'] == 100
    stored = accounts_service.list_balance_history(account['id'])
    assert any(e['recorded_at'] == future and e['balance'] == '999' for e in stored)


def test_deleting_the_newest_entry_falls_back_to_the_one_before(vault):
    """This is the way back out of a mistyped figure."""
    account = make_account(current_balance=100)
    accounts_service.add_balance_entry(account['id'], '250', today())
    assert accounts_service.get_account(account['id'])['current_balance'] == 250

    newest = accounts_service.list_balance_history(account['id'])[0]
    accounts_service.delete_balance_entry(account['id'], newest['id'])

    assert accounts_service.get_account(account['id'])['current_balance'] == 100


def test_editing_the_newest_entry_moves_the_account(vault):
    account = make_account(current_balance=100)
    accounts_service.add_balance_entry(account['id'], '250', today())
    newest = accounts_service.list_balance_history(account['id'])[0]

    accounts_service.update_balance_entry(account['id'], newest['id'], '375', today())

    assert accounts_service.get_account(account['id'])['current_balance'] == 375


def test_last_updated_only_ever_moves_forward(vault):
    """Filing an old statement does not make the record staler than it was."""
    account = make_account(current_balance=100)
    before = accounts_service.get_account(account['id'])['last_updated']

    accounts_service.add_balance_entry(account['id'], '50', '2019-06-01')

    assert accounts_service.get_account(account['id'])['last_updated'] == before


def test_backdated_save_records_history_without_moving_the_headline(vault):
    """update_account(balance_date=<earlier>) files the figure and leaves both
    the stored balance and last_updated where they were."""
    account = make_account(current_balance=100)
    before = accounts_service.get_account(account['id'])['last_updated']

    edit = dict(account)
    edit['current_balance'] = 500
    updated = accounts_service.update_account(
        account['id'], edit, balance_date='2024-01-15'
    )

    assert updated['current_balance'] == 100
    assert updated['last_updated'] == before
    entries = accounts_service.list_balance_history(account['id'])
    assert any(e['recorded_at'] == '2024-01-15' and e['balance'] == '500'
               for e in entries)


def test_same_day_save_does_move_the_headline(vault):
    """The companion to the test above: no balance_date means the figure is current."""
    account = make_account(current_balance=100)

    edit = dict(account)
    edit['current_balance'] = 500
    updated = accounts_service.update_account(account['id'], edit)

    assert updated['current_balance'] == 500


def test_touch_account_snapshots_without_changing_the_balance(vault):
    account = make_account(current_balance=100)
    entries_before = len(accounts_service.list_balance_history(account['id']))

    accounts_service.touch_account(account['id'])

    fresh = accounts_service.get_account(account['id'])
    assert fresh['current_balance'] == 100
    assert fresh['last_updated'] == today()
    assert len(accounts_service.list_balance_history(account['id'])) == entries_before + 1


def test_touch_account_recovers_a_balance_left_empty(vault):
    """An account can hold history while its own figure sits empty — cleared in
    the edit form, or moved in from a category that had no balance field. No
    Change is what repairs it: the entries say what the balance is, so it comes
    back off the newest one instead of the account reading blank beside them."""
    account = make_account(current_balance=100)
    cleared = dict(account)
    cleared['current_balance'] = ''
    accounts_service.update_account(account['id'], cleared)
    assert accounts_service.get_account(account['id'])['current_balance'] == ''

    accounts_service.touch_account(account['id'])

    fresh = accounts_service.get_account(account['id'])
    assert fresh['current_balance'] == 100
    # And the snapshot it files carries the recovered figure, not nothing.
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '100'


def test_touch_account_leaves_a_balance_free_account_alone(vault):
    """Nothing to recover and nothing to file — only the date moves."""
    account = make_account(category='Websites')

    accounts_service.touch_account(account['id'])

    fresh = accounts_service.get_account(account['id'])
    assert fresh['last_updated'] == today()
    assert fresh.get('current_balance') is None
    assert accounts_service.list_balance_history(account['id']) == []


def test_non_numeric_history_is_not_copied_onto_the_account(vault):
    """Legacy plaintext that was never a number has nothing worth adopting."""
    account = make_account(current_balance=100)
    with pytest.raises(ValueError):
        accounts_service.add_balance_entry(account['id'], 'not a number', today())

    assert accounts_service.get_account(account['id'])['current_balance'] == 100


def test_history_entry_rejects_an_impossible_date(vault):
    account = make_account(current_balance=100)
    with pytest.raises(ValueError):
        accounts_service.add_balance_entry(account['id'], '10', '2024-02-31')

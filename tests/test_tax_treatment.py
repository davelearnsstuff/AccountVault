# -----------------------------------------------------------------------------
# tests/test_tax_treatment.py
# Traditional, Roth, or both: the Investments fields the retirement planner
# reads, and the rules that keep a Both account's balance the sum of its parts.
#
# That balance is what history, the dashboard and net worth read, so a rule
# that slips here files a wrong figure everywhere at once.
# -----------------------------------------------------------------------------

from datetime import date, timedelta

import pytest

from conftest import make_account
from core import database
from core.categories import (
    TAX_TREATMENT_TYPES, active_splits, add_amounts, field_shown, schema_field,
)
from services import accounts as accounts_service
from services import dashboard as dashboard_service


def both_account(**fields) -> dict:
    """A 401(k) holding 1,000 Traditional and 500 Roth, paid into monthly."""
    values = {
        'category': 'Investments', 'name': 'Work 401(k)', 'account_type': '401k',
        'tax_treatment': 'Both', 'traditional_balance': 1000, 'roth_balance': 500,
        'traditional_contribution': 200, 'roth_contribution': 100,
        'contribution_period': 'Monthly',
    }
    values.update(fields)
    return make_account(**values)


def history(account_id: int) -> list:
    """The account's history figures, oldest first."""
    return [entry['balance'] for entry in
            reversed(accounts_service.list_balance_history(account_id))]


def days_ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def file_history_under(account_id: int, day: str):
    """Moves every history entry the account has to `day`, as if it had been
    recorded then."""
    with database.connect() as db:
        db.execute("UPDATE balance_history SET recorded_at = ? WHERE account_id = ?",
                   (day, account_id))
    accounts_service.invalidate_cache()


# -----------------------------------------------------------------------------
# The schema
# -----------------------------------------------------------------------------

def test_every_account_type_ever_offered_is_still_offered():
    """The form blanks a stored value it has no option for, and saving then
    writes the blank over it, so an option can never be renamed or removed."""
    options = schema_field('Investments', 'account_type')['options']
    assert {'401k', 'IRA', 'Roth IRA', 'HSA', 'Brokerage', 'Other'} <= set(options)


@pytest.mark.parametrize('account_type', TAX_TREATMENT_TYPES)
def test_tax_treatment_is_asked_for_where_the_type_leaves_it_open(account_type):
    field = schema_field('Investments', 'tax_treatment')
    assert field_shown('Investments', field, {'account_type': account_type})


@pytest.mark.parametrize('account_type', ['Roth IRA', 'HSA', 'Brokerage', 'Other', None])
def test_tax_treatment_is_not_asked_for_where_the_type_decides_it(account_type):
    field = schema_field('Investments', 'tax_treatment')
    assert not field_shown('Investments', field, {'account_type': account_type})


def test_the_two_amounts_apply_only_to_a_both_account_of_such_a_type():
    field = schema_field('Investments', 'roth_balance')
    assert field_shown('Investments', field, {'account_type': 'IRA', 'tax_treatment': 'Both'})
    assert not field_shown('Investments', field, {'account_type': 'IRA', 'tax_treatment': 'Roth'})
    # A treatment left over from before the type changed does not count.
    assert not field_shown('Investments', field,
                           {'account_type': 'Brokerage', 'tax_treatment': 'Both'})


def test_both_totals_are_split_on_a_both_account_and_nowhere_else():
    both = {'account_type': '403b', 'tax_treatment': 'Both'}
    assert set(active_splits('Investments', both)) == {'current_balance', 'contribution_amount'}
    assert active_splits('Investments', {**both, 'tax_treatment': 'Traditional'}) == {}
    assert active_splits('Banking', both) == {}


def test_amounts_add_up_without_binary_rounding():
    assert add_amounts(1000.10, 2000.20) == 3000.3
    assert add_amounts(1000, 500) == 1500
    assert isinstance(add_amounts(1000, 500), int)


# -----------------------------------------------------------------------------
# Saving
# -----------------------------------------------------------------------------

def test_a_both_account_files_the_sum_of_its_parts(vault):
    account = both_account()
    assert account['current_balance'] == 1500
    assert history(account['id']) == ['1500']


def test_the_contribution_parts_make_the_amount_the_dashboard_counts(vault):
    both_account()
    contributions = dashboard_service.investing_vs_income()['contributions']
    assert [(row['amount'], row['yearly']) for row in contributions] == [(300, 3600)]


def test_new_parts_make_a_new_balance(vault):
    account = both_account()
    saved = accounts_service.update_account(
        account['id'], {**account, 'traditional_balance': 1100, 'roth_balance': 600})
    assert saved['current_balance'] == 1700
    assert history(account['id']) == ['1500', '1700']


def test_parts_left_alone_keep_a_balance_history_has_moved(vault):
    """A newer history entry moves the balance and not the parts. Editing a
    note afterwards must not put the balance back to the parts' old sum, or
    file that sum as if it had just been read off a statement."""
    account = both_account()
    accounts_service.add_balance_entry(account['id'], '1600', date.today().isoformat())
    moved = accounts_service.get_account(account['id'])
    assert moved['current_balance'] == 1600

    saved = accounts_service.update_account(account['id'], {**moved, 'notes': 'checked'})

    assert saved['current_balance'] == 1600
    assert (saved['traditional_balance'], saved['roth_balance']) == (1000, 500)
    assert history(account['id']) == ['1500', '1600']


def test_leaving_both_clears_the_parts_and_keeps_the_totals(vault):
    account = both_account()
    saved = accounts_service.update_account(account['id'],
                                            {**account, 'tax_treatment': 'Traditional'})
    assert saved['tax_treatment'] == 'Traditional'
    assert (saved['current_balance'], saved['contribution_amount']) == (1500, 300)
    for key in ('traditional_balance', 'roth_balance',
                'traditional_contribution', 'roth_contribution'):
        assert key not in saved


def test_a_type_that_decides_its_own_treatment_clears_it(vault):
    account = both_account()
    saved = accounts_service.update_account(account['id'],
                                            {**account, 'account_type': 'Brokerage'})
    assert 'tax_treatment' not in saved
    assert 'roth_balance' not in saved
    assert saved['current_balance'] == 1500


def test_another_category_keeps_these_keys_as_it_keeps_any_other(vault):
    """Keys outside a category's schema are kept, so a record moved back into
    Investments still has them."""
    account = make_account(category='Banking', tax_treatment='Both',
                           traditional_balance=10, roth_balance=5, current_balance=99)
    assert (account['tax_treatment'], account['traditional_balance']) == ('Both', 10)
    assert account['current_balance'] == 99


def test_amounts_with_currency_symbols_are_read_and_stored_as_numbers(vault):
    account = both_account(traditional_balance='$1,000.50', roth_balance='250')
    assert account['current_balance'] == 1250.5
    assert (account['traditional_balance'], account['roth_balance']) == (1000.5, 250)


@pytest.mark.parametrize('value, message', [(-1, 'negative'), ('lots', 'amount')])
def test_a_part_that_is_no_amount_is_refused(vault, value, message):
    with pytest.raises(ValueError, match=message):
        both_account(roth_balance=value)


def test_a_missing_part_leaves_the_balance_as_given(vault):
    """The form insists on both. An import may bring one, and guessing the
    other would be inventing money."""
    account = both_account(roth_balance=None, current_balance=1200)
    assert account['current_balance'] == 1200


def test_an_account_stored_under_the_old_category_slug_follows_the_same_rules(vault):
    """Vaults from before the category rename store 'investments'. Read as an
    unknown category, a backdated reading's parts replaced the account's own
    even behind a newer reading, leaving parts that matched no balance."""
    account = both_account(category='investments')
    assert account['current_balance'] == 1500

    accounts_service.update_account(
        account['id'],
        {**account, 'traditional_balance': 1200, 'roth_balance': 600},
        balance_date=days_ago(30), confirm_balance=True)

    after = accounts_service.get_account(account['id'])
    assert after['current_balance'] == 1500
    assert (after['traditional_balance'], after['roth_balance']) == (1000, 500)


def test_an_import_works_out_the_balance(vault):
    accounts_service.create_accounts([{
        'category': 'Investments', 'name': 'Old 403(b)', 'account_type': '403b',
        'tax_treatment': 'Both', 'traditional_balance': '700', 'roth_balance': '300',
    }])
    imported = accounts_service.list_accounts('Investments')[0]
    assert imported['current_balance'] == 1000


# -----------------------------------------------------------------------------
# Backdated readings
# -----------------------------------------------------------------------------

def test_a_backdated_reading_that_becomes_current_brings_its_parts(vault):
    account = both_account()
    file_history_under(account['id'], days_ago(300))

    accounts_service.update_account(
        account['id'],
        {**account, 'traditional_balance': 1200, 'roth_balance': 600},
        balance_date=days_ago(30), confirm_balance=True)

    after = accounts_service.get_account(account['id'])
    assert after['current_balance'] == 1800
    assert (after['traditional_balance'], after['roth_balance']) == (1200, 600)


def test_a_backdated_reading_behind_a_newer_one_leaves_the_parts(vault):
    """It fills in the past. The account keeps the figure and the parts of the
    newer reading."""
    account = both_account()

    accounts_service.update_account(
        account['id'],
        {**account, 'traditional_balance': 1200, 'roth_balance': 600},
        balance_date=days_ago(30), confirm_balance=True)

    after = accounts_service.get_account(account['id'])
    assert after['current_balance'] == 1500
    assert (after['traditional_balance'], after['roth_balance']) == (1000, 500)
    assert sorted(history(account['id'])) == ['1500', '1800']

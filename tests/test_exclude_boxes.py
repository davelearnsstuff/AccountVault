# -----------------------------------------------------------------------------
# tests/test_exclude_boxes.py
# The two checkboxes that leave an account out: Exclude from Dashboard takes it
# out of every dashboard figure, and Exclude from Retirement out of the
# planner. Each does only its own job, and a tick survives the form, an
# export and its import.
# -----------------------------------------------------------------------------

import pytest

from conftest import make_account
from core.categories import (
    CATEGORY_CONFIG, CHECKBOX_KEYS, CHECKED, EXCLUDE_DASHBOARD_KEY, EXCLUDE_RETIREMENT_KEY,
    RECEIVING_KEY, format_value, has_field, is_checked, searchable_fields,
)
from services import account_import, csv_export
from services import accounts as accounts_service
from services import dashboard
from services import retirement as retirement_service


# -- the schema ----------------------------------------------------------------

def test_the_dashboard_box_is_on_every_category_the_dashboard_counts():
    counted = {'Banking', 'Credit Cards', 'Loans', 'Investments', 'Property', 'Income'}
    assert {name for name in CATEGORY_CONFIG if has_field(name, EXCLUDE_DASHBOARD_KEY)} \
        == counted


def test_the_retirement_box_is_on_the_categories_the_planner_reads():
    assert {name for name in CATEGORY_CONFIG if has_field(name, EXCLUDE_RETIREMENT_KEY)} \
        == {'Banking', 'Investments'}
    assert CHECKBOX_KEYS == {EXCLUDE_DASHBOARD_KEY, EXCLUDE_RETIREMENT_KEY, RECEIVING_KEY}


@pytest.mark.parametrize('value, ticked', [
    ('Yes', True), ('yes', True), (True, True),
    (None, False), ('', False), ('No', False), (False, False), ('true', False),
])
def test_only_yes_or_true_is_ticked(value, ticked):
    assert is_checked(value) is ticked


def test_a_box_reads_as_yes_or_no_and_is_never_searched():
    field = {'type': 'checkbox'}
    assert format_value(CHECKED, field) == 'Yes'
    assert format_value('junk', field) == 'No'
    assert EXCLUDE_DASHBOARD_KEY not in {f['key'] for f in searchable_fields('Banking')}


# -- the dashboard ---------------------------------------------------------------

def test_a_ticked_account_is_out_of_every_dashboard_figure(vault):
    make_account(name='Ours', account_type='Checking', current_balance='1000')
    kept = make_account(name="Grampa's", account_type='Checking', current_balance='5000',
                        **{EXCLUDE_DASHBOARD_KEY: CHECKED})
    make_account(name='Job', category='Income', current_balance='50000')
    make_account(name='Not ours', category='Income', current_balance='90000',
                 **{EXCLUDE_DASHBOARD_KEY: CHECKED})
    make_account(name='529', category='Investments', contribution_amount='100',
                 contribution_period='Monthly', **{EXCLUDE_DASHBOARD_KEY: CHECKED})

    summary = dashboard.monthly_summary()
    assert summary['categories']['Banking'][-1] == 1000
    assert summary['net_worth'][-1] == 1000
    assert [a['name'] for a in summary['accounts']['Banking']] == ['Ours']

    flows = dashboard.investing_vs_income()
    assert flows['income_total'] == 50000
    assert flows['contributions'] == []

    # Out of the dashboard, not out of the vault or the planner.
    assert accounts_service.get_account(kept['id'])['current_balance'] == '5000'
    counted = [row['name'] for row in retirement_service.savings_from_accounts()['counted']]
    assert "Grampa's" in counted


# -- the planner -----------------------------------------------------------------

def test_a_ticked_account_is_left_out_of_the_plan_and_says_why(vault):
    make_account(name='Ours', account_type='Savings', current_balance='1000')
    make_account(name="Lisa's 529", category='Investments', account_type='Brokerage',
                 current_balance='8000', contribution_amount='100',
                 contribution_period='Monthly', **{EXCLUDE_RETIREMENT_KEY: CHECKED})

    savings = retirement_service.savings_from_accounts()
    assert savings['figures']['taxable'] == 1000
    assert savings['figures']['save_taxable'] == 0
    assert [(row['name'], row['reason']) for row in savings['skipped']] == [
        ("Lisa's 529", retirement_service.EXCLUDED)]

    # The dashboard still counts it.
    assert dashboard.investing_vs_income()['contribution_total'] == 1200


# -- the form ----------------------------------------------------------------------

def test_the_form_ticks_and_unticks_the_box(vault, qt_app):
    from ui.account_form import AccountForm

    account = make_account(name='Kid Saver', account_type='Savings',
                           **{EXCLUDE_RETIREMENT_KEY: CHECKED})
    form = AccountForm(account, 'Banking')
    box = form._inputs[EXCLUDE_RETIREMENT_KEY]
    assert box.isChecked()
    assert not form._inputs[EXCLUDE_DASHBOARD_KEY].isChecked()
    assert form.values()[EXCLUDE_RETIREMENT_KEY] == CHECKED

    box.setChecked(False)
    assert form.is_dirty()
    saved = accounts_service.update_account(account['id'], form.values())
    assert not is_checked(saved.get(EXCLUDE_RETIREMENT_KEY))


# -- export and import ----------------------------------------------------------------

def test_a_tick_survives_a_csv_export_and_import(vault):
    make_account(name='Kid Saver', account_type='Savings',
                 **{EXCLUDE_RETIREMENT_KEY: CHECKED, EXCLUDE_DASHBOARD_KEY: CHECKED})

    rows = account_import.prepare(csv_export.export_csv(accounts_service.list_accounts()))['rows']

    assert rows[0][EXCLUDE_RETIREMENT_KEY] == CHECKED
    assert rows[0][EXCLUDE_DASHBOARD_KEY] == CHECKED


@pytest.mark.parametrize('cell, stored', [('true', CHECKED), ('Y', CHECKED), ('1', CHECKED),
                                          ('no', None), ('maybe', None), ('', None)])
def test_an_imported_box_is_stored_the_way_the_form_writes_it(cell, stored):
    rows = account_import.prepare(f'name,category,Exclude from Retirement\n'
                                  f'Kid Saver,Banking,{cell}\n')['rows']
    assert rows[0].get(EXCLUDE_RETIREMENT_KEY) == stored

# -----------------------------------------------------------------------------
# tests/test_demo_household.py
# The demo vault built to a household's figures: the money accounts land near
# the totals asked for but never on them, the dashboard and the planner read
# them as the figures say, and the saved plan is that household's.
# -----------------------------------------------------------------------------

import pytest

from core import retirement
from core.categories import (
    BOTH, CONTRIBUTIONS_PER_YEAR, EXCLUDE_DASHBOARD_KEY, INCOME_TYPE_KEY, PARTNERS,
    RECEIVING_KEY, SOCIAL_SECURITY, YOURS, is_checked, parse_money,
)
from services import accounts as accounts_service
from services import dashboard
from services import retirement as retirement_service
from tools import make_demo_vault
from tools.make_demo_vault import Household


@pytest.fixture
def build(vault, tmp_path):
    """Builds a demo vault for a household and leaves it open, as the builder
    does. Each call makes a new file."""
    made = []

    def run(household=None):
        path = tmp_path / f'demo{len(made)}.vault'
        make_demo_vault.build_vault(path, household or Household())
        made.append(path)
        return path
    return run


def open_accounts(category):
    return [a for a in accounts_service.list_accounts(category) if not a['closed']]


def jobs():
    """The open Income records that are pay, not Social Security."""
    return [a for a in open_accounts('Income') if a.get(INCOME_TYPE_KEY) != SOCIAL_SECURITY]


def benefits():
    """Homer's and Marge's Social Security at 67, as the plan reads them."""
    figures = retirement_service.savings_from_accounts()['figures']
    return figures['ss'], figures['partner_ss']


def total(category):
    return sum(parse_money(a.get('current_balance')) or 0 for a in open_accounts(category))


def named(name):
    return next(a for a in accounts_service.list_accounts() if a['name'] == name)


HOUSEHOLDS = {
    'single': Household(),
    'two': Household(two_incomes=True, income=140000, age=45, partner_age=43,
                     invested=620000, saving_pct=18),
}


# -- near the figures, never on them ------------------------------------------

@pytest.mark.parametrize('kind', HOUSEHOLDS)
def test_totals_land_near_the_figures_but_not_on_them(build, kind):
    household = HOUSEHOLDS[kind]
    build(household)

    invested, income = total('Investments'), total('Income')
    assert invested != household.invested
    assert invested == pytest.approx(household.invested, rel=0.035)
    assert income != household.income
    assert income == pytest.approx(household.income, rel=0.035)


@pytest.mark.parametrize('kind', HOUSEHOLDS)
def test_the_investing_rate_lands_near_the_saving_figure(build, kind):
    """Every contribution counts, so the dashboard's rate is the figure."""
    household = HOUSEHOLDS[kind]
    build(household)

    rate = dashboard.investing_vs_income()['rate'] * 100
    assert rate == pytest.approx(household.saving_pct, abs=1)


def test_a_limit_sends_the_rest_to_the_brokerage_account(build):
    """At 50 and over the catch-up is allowed, and nothing goes past it."""
    household = Household(income=400000, age=55, partner_age=52, invested=2500000,
                          saving_pct=25)
    build(household)

    def yearly(name):
        account = named(name)
        return (float(account['contribution_amount'])
                * CONTRIBUTIONS_PER_YEAR[account['contribution_period']])

    assert yearly('Nuclear Plant 401(k)') <= 24500 + 8000
    assert yearly('Springfield Index Fund') <= 7500 + 1100
    assert yearly('Springfield Mutual Brokerage') > 20000
    rate = dashboard.investing_vs_income()['rate'] * 100
    assert rate == pytest.approx(household.saving_pct, abs=1)


def test_bank_balances_follow_income(build):
    build(Household(income=50000))
    modest = total('Banking')
    build(Household(income=150000))
    comfortable = total('Banking')

    # The children's accounts stay as they are, so it is not quite threefold.
    assert 2 < comfortable / modest < 4


def test_the_same_answers_build_the_same_vault(build):
    def money():
        return sorted((a['name'], a.get('current_balance'), a.get('contribution_amount'))
                      for a in open_accounts(None))

    build(HOUSEHOLDS['two'])
    first, first_plan = money(), retirement_service.load_plan()
    build(HOUSEHOLDS['two'])

    assert money() == first
    assert retirement_service.load_plan() == first_plan


# -- who works ----------------------------------------------------------------

def test_single_income_is_homer_alone(build):
    build(Household())

    assert [a['name'] for a in jobs()] == ['Springfield Nuclear Power Plant — Safety Inspector']
    names = {a['name'] for a in open_accounts('Investments')}
    assert 'Springfield Elementary 403(b)' not in names

    homer, marge = benefits()
    assert marge == homer // 2


def test_two_incomes_give_marge_a_job_and_a_403b(build):
    build(HOUSEHOLDS['two'])

    assert len(jobs()) == 2
    plan = named('Springfield Elementary 403(b)')
    assert plan['account_type'] == '403b'
    assert plan['tax_treatment'] == 'Traditional'

    homer, marge = benefits()
    assert marge > homer // 2


def test_social_security_is_in_income_for_the_plan_but_not_the_dashboard(build):
    """Still to come, so it is not income yet; the plan reads it from the
    records rather than from its own details."""
    build()

    names = {row['name'] for row in dashboard.investing_vs_income()['income']}
    assert not any(name.startswith('Social Security') for name in names)
    homer, marge = named('Social Security — Homer'), named('Social Security — Marge')
    assert (homer['whose'], marge['whose']) == (YOURS, PARTNERS)
    assert is_checked(homer[EXCLUDE_DASHBOARD_KEY]) and is_checked(marge[EXCLUDE_DASHBOARD_KEY])
    assert not is_checked(homer.get(RECEIVING_KEY))
    # Every Income record says what it is.
    assert all(a.get(INCOME_TYPE_KEY) for a in accounts_service.list_accounts('Income'))
    details = retirement_service.load_plan()['details']
    assert 'ss' not in details and 'partner_ss' not in details

    homer, _ = benefits()
    inputs = retirement.plan_inputs(retirement_service.load_plan(),
                                    retirement_service.savings_from_accounts()['figures'])
    assert inputs['ss'] == homer > 0


# -- the planner's view --------------------------------------------------------

@pytest.mark.parametrize('kind', HOUSEHOLDS)
def test_the_plan_is_this_household(build, kind):
    household = HOUSEHOLDS[kind]
    build(household)

    plan = retirement_service.load_plan()
    assert retirement.missing_details(plan['details']) == []
    inputs = retirement.plan_inputs(plan, retirement_service.savings_from_accounts()['figures'])
    assert (inputs['age'], inputs['partner_age']) == (household.age, household.partner_age)
    assert inputs['partner'] is True


def test_spending_is_what_is_left_after_tax_and_saving(build):
    household = HOUSEHOLDS['two']
    build(household)

    flows = dashboard.investing_vs_income()
    spending = retirement_service.load_plan()['details']['spending']
    left = flows['income_total'] - flows['contribution_total']
    # Tax takes a fair share of what is left, but nowhere near all of it.
    assert 0.6 * left < spending < 0.9 * left


def test_the_401k_holds_both_parts_of_its_balance(build):
    """Ended on with the parts, so the planner reads them as entered and does
    not have to scale them to fit."""
    build()

    account = named('Nuclear Plant 401(k)')
    assert account['tax_treatment'] == BOTH
    parts = parse_money(account['traditional_balance']) + parse_money(account['roth_balance'])
    assert parts == pytest.approx(parse_money(account['current_balance']), abs=0.001)
    history = accounts_service.list_balance_history(account['id'])
    assert history[0]['balance'] == str(account['current_balance'])

    counted = {row['name']: row['notes'] for row in
               retirement_service.savings_from_accounts()['counted']}
    assert counted['Nuclear Plant 401(k)'] == []
    assert counted['Rollover IRA — Bowlarama Job'] == [retirement_service.ASSUMED]


def test_the_childrens_money_and_grampas_are_out_of_the_plan(build):
    build()

    excluded = {row['name'] for row in retirement_service.savings_from_accounts()['skipped']
                if row['reason'] == retirement_service.EXCLUDED}
    assert excluded == {"Lisa's 529 College Savings", "Bart's UGMA Custodial Account",
                        "Bart's Kid Saver", "Lisa's Saxophone Fund", "Maggie's Passbook",
                        "Grampa's Pension Account"}

    # Grampa's alone is off the dashboard as well.
    shown = {account['name'] for account in dashboard.monthly_summary()['accounts']['Banking']}
    assert "Grampa's Pension Account" not in shown
    assert "Bart's Kid Saver" in shown


# -- the figures themselves ---------------------------------------------------

@pytest.mark.parametrize('field, value', [('age', 17), ('partner_age', 81),
                                          ('income', 0), ('saving_pct', 51),
                                          ('invested', -1), ('two_incomes', 'yes')])
def test_a_figure_out_of_range_is_refused_before_any_file(tmp_path, field, value):
    path = tmp_path / 'demo.vault'

    with pytest.raises(ValueError):
        make_demo_vault.build_vault(path, Household(**{field: value}))

    assert not path.exists()


def test_social_security_follows_pay_up_to_the_wage_base():
    benefit = make_demo_vault.social_security
    assert benefit(0) == 0
    assert 2000 < benefit(58000) < 2500
    assert benefit(10 ** 6) == benefit(make_demo_vault.SS_WAGE_BASE)


# -- the dialog ---------------------------------------------------------------

def test_the_dialog_starts_on_the_defaults_and_hands_back_its_answers(qt_app):
    from dataclasses import asdict

    from ui.dialogs import DemoHouseholdDialog

    defaults = asdict(Household())
    dialog = DemoHouseholdDialog(None, defaults, make_demo_vault.LIMITS)
    assert dialog.values() == defaults

    dialog.incomes.setCurrentIndex(dialog.incomes.findData(True))
    dialog.saving.setValue(99)
    answers = dialog.values()
    assert answers['two_incomes'] is True
    assert answers['saving_pct'] == make_demo_vault.LIMITS['saving_pct'][1]
    Household(**answers).check()
    dialog.deleteLater()

# -----------------------------------------------------------------------------
# tests/test_income_benefits.py
# Social Security and pensions kept as Income records: a monthly benefit in
# place of annual pay, counted on the dashboard only once it is being paid,
# and read by the retirement planner for whoever it belongs to.
# -----------------------------------------------------------------------------

from datetime import date

import pytest

from conftest import make_account
from core import retirement
from core.categories import (
    CHECKED, INCOME_TYPE_KEY, PARTNERS, PENSION, SOCIAL_SECURITY, field_shown,
    has_balance, schema_field,
)
from services import accounts as accounts_service
from services import dashboard
from services import retirement as retirement_service

TODAY = date(2026, 10, 18)


def benefit_record(name='Social Security', kind=SOCIAL_SECURITY, monthly=2900, **fields):
    return make_account(category='Income', name=name, income_type=kind,
                        monthly_benefit=monthly, **fields)


def figures():
    return retirement_service.savings_from_accounts()['figures']


def plan(**details) -> dict:
    return {'version': 1, 'overrides': {}, 'scenarios': [],
            'details': {'birth_year': 1970, 'birth_month': 3, 'spending': 84000, **details}}


# -- the record ---------------------------------------------------------------

@pytest.mark.parametrize('kind, pay_applies', [
    (None, True), ('', True), ('Pay', True), ('Other', True), ('Salary', True),
    (SOCIAL_SECURITY, False), (PENSION, False),
])
def test_annual_pay_gives_way_to_a_monthly_benefit(kind, pay_applies):
    """Only the two benefit types hide pay, so a blank type, or one an import
    spelled its own way, keeps the pay it had."""
    values = {INCOME_TYPE_KEY: kind}
    pay = schema_field('Income', 'current_balance')
    monthly = schema_field('Income', 'monthly_benefit')
    assert field_shown('Income', pay, values) is pay_applies
    assert field_shown('Income', monthly, values) is not pay_applies
    assert has_balance('Income', values) is pay_applies


def test_making_a_job_social_security_clears_its_pay(vault):
    job = make_account(category='Income', name='Plant', current_balance=58000)

    saved = accounts_service.update_account(
        job['id'], {**job, INCOME_TYPE_KEY: SOCIAL_SECURITY, 'monthly_benefit': 2900})

    assert 'current_balance' not in saved
    assert saved['monthly_benefit'] == 2900
    # The pay it had stays on file as history.
    assert accounts_service.list_balance_history(job['id'])


def test_pay_clears_the_benefit_fields(vault):
    record = benefit_record(whose=PARTNERS, receiving_now=CHECKED)

    saved = accounts_service.update_account(record['id'], {**record, INCOME_TYPE_KEY: 'Pay'})

    for key in ('monthly_benefit', 'whose', 'receiving_now'):
        assert key not in saved


# -- the dashboard ------------------------------------------------------------

def test_a_benefit_counts_as_income_only_while_received(vault):
    make_account(category='Income', name='Job', current_balance=60000)
    benefit_record(name='Mine, to come')
    benefit_record(name='Pension, paid', kind=PENSION, monthly=1000, receiving_now=CHECKED)

    flows = dashboard.investing_vs_income()

    assert {row['name']: row['yearly'] for row in flows['income']} == {
        'Job': 60000, 'Pension, paid': 12000}
    assert flows['income_total'] == 72000


# -- the planner --------------------------------------------------------------

def test_each_benefit_goes_to_whoever_it_belongs_to(vault):
    benefit_record(monthly=2900)
    benefit_record(monthly=1600, whose=PARTNERS)
    benefit_record(monthly=2000, whose=PARTNERS, receiving_now=CHECKED)
    benefit_record(kind=PENSION, monthly=800)
    benefit_record(kind=PENSION, monthly=300, whose=PARTNERS)
    benefit_record(kind=PENSION, monthly=999, closed=True)
    make_account(category='Income', name='Job', current_balance=60000)

    totals = figures()

    assert (totals['ss'], totals['partner_ss'], totals['partner_ss_received']) == (2900, 1600, 2000)
    assert (totals['pension'], totals['partner_pension']) == (800, 300)


def test_the_plan_reads_the_records_unless_a_figure_is_typed(vault):
    benefit_record(monthly=2900)
    benefit_record(kind=PENSION, monthly=800)

    inputs = retirement.plan_inputs(plan(), figures(), TODAY)
    assert (inputs['ss'], inputs['pension']) == (2900, 800)

    inputs = retirement.plan_inputs(plan(ss=2500, pension=0), figures(), TODAY)
    assert (inputs['ss'], inputs['pension']) == (2500, 0)


def test_social_security_already_received_is_paid_as_it_is():
    """Received at 62, $2,030 a month is $2,900 at 67, which the plan scales
    back to $2,030 at the claim age."""
    supplied = {'ss_received': 2030}

    at_67 = retirement.benefit({'ss_age': 62}, supplied, 'ss')

    assert at_67 == pytest.approx(2900)
    assert at_67 * retirement.SS_FACTORS[62] == pytest.approx(2030)


def test_claim_crossover_is_where_the_two_claims_have_paid_the_same():
    """70% from 62 and 100% from 67 have both paid 16.6 years' worth of the
    age-67 benefit at 78 and 8 months; 124% from 70, at 82 and a half."""
    assert retirement.claim_crossover(62) == pytest.approx(78 + 2 / 3)
    assert retirement.claim_crossover(70) == pytest.approx(82.5)
    assert retirement.claim_crossover(67) is None
    for age in retirement.SS_FACTORS:
        crossover = retirement.claim_crossover(age)
        if crossover is not None:
            factor = retirement.SS_FACTORS[age]
            assert factor * (crossover - age) == pytest.approx(crossover - 67)


# -- Your details -------------------------------------------------------------

def test_your_details_shows_the_records_figure_and_its_claim_ages(vault, qt_app):
    from ui.retirement_details import DetailsPage

    benefit_record(monthly=2900)
    page = DetailsPage()
    page.load(plan(), retirement_service.savings_from_accounts())

    field = page._fields['ss']
    assert field.widget.text() == ''
    assert field.widget.placeholderText() == '$2,900'
    assert 'From your Income records' in field.hint.text()
    table = page._claim_tables['ss_table']
    assert table.item(0, 2).text() == '$2,030'   # at 62
    assert table.item(0, 3).text() == '78y 8m'
    assert table.item(5, 3).text() == '—'        # 67 itself

    page.load(plan(ss=3000), retirement_service.savings_from_accounts())
    assert 'Your figure' in field.hint.text()
    assert table.item(0, 2).text() == '$2,100'
    page.deleteLater()

# -----------------------------------------------------------------------------
# tests/test_retirement_plan.py
# The retirement plan as the vault keeps it, and the savings worked out from
# the accounts.
#
# The plan holds birth dates, spending and Social Security, so it is stored
# encrypted and has to survive everything the vault's other encrypted rows
# survive. The savings decide what the planner is asked, so each account type
# and tax treatment is checked for where it lands.
# -----------------------------------------------------------------------------

from datetime import date

import pytest

from conftest import MASTER, NEW_MASTER, make_account
from core import auth, database, encryption, preferences, retirement
from core.retirement import (
    DETAIL_DEFAULTS, empty_plan, missing_details, plan_inputs, validate_plan,
)
from services import accounts as accounts_service
from services import retirement as retirement_service

TODAY = date(2026, 10, 18)


def details(**fields) -> dict:
    """A plan holding enough to run: a birth date and spending."""
    return {'version': 1, 'overrides': {},
            'details': {'birth_year': 1978, 'birth_month': 3, 'spending': 84000, **fields}}


def figures(**amounts) -> dict:
    return {**dict.fromkeys(retirement.SAVINGS_KEYS, 0), **amounts}


# -----------------------------------------------------------------------------
# The plan's shape
# -----------------------------------------------------------------------------

def test_a_value_that_does_not_validate_is_dropped_on_its_own():
    """A newer version of the app might store a value this one refuses. That
    should cost the one field, not the whole plan."""
    clean = validate_plan({'details': {'spending': -5, 'retire_age': 60, 'stray': 1},
                           'overrides': {'pretax': 'lots', 'roth': 1000, 'bogus': 5}})
    assert clean == {'version': 1, 'details': {'retire_age': 60}, 'overrides': {'roth': 1000},
                     'scenarios': []}


@pytest.mark.parametrize('raw', [[], {'details': []}, {'overrides': 'x'}])
def test_something_that_is_not_a_plan_is_refused(raw):
    with pytest.raises(ValueError):
        validate_plan(raw)


def test_a_plan_waits_for_birth_dates_and_spending():
    assert missing_details({}) == ['birth_year', 'birth_month', 'spending']
    assert missing_details({'partner': True, 'birth_year': 1978, 'birth_month': 3}) == [
        'partner_birth_year', 'partner_birth_month', 'spending']
    with pytest.raises(ValueError):
        plan_inputs(empty_plan(), figures(), TODAY)


def test_ages_come_from_the_birth_dates():
    plan = details(partner=True, partner_birth_year=1980, partner_birth_month=11)
    inputs = plan_inputs(plan, figures(), TODAY)
    assert (inputs['age'], inputs['partner_age']) == (48, 45)


def test_a_partner_retires_the_same_year_as_you_until_their_age_is_entered():
    plan = details(partner=True, partner_birth_year=1980, partner_birth_month=11,
                   retire_age=60)
    assert plan_inputs(plan, figures(), TODAY)['partner_retire_age'] == 57
    plan['details']['partner_retire_age'] = 63
    assert plan_inputs(plan, figures(), TODAY)['partner_retire_age'] == 63


def test_details_not_entered_take_the_defaults():
    inputs = plan_inputs(details(), figures(), TODAY)
    for key in ('retire_age', 'plan_to', 'ss_age', 'return_mean', 'convert'):
        assert inputs[key] == DETAIL_DEFAULTS[key]
    assert inputs['spending'] == 84000


def test_pensions_are_kept_with_the_details_and_default_to_none():
    inputs = plan_inputs(details(), figures(), TODAY)
    assert (inputs['pension'], inputs['pension_age'], inputs['pension_cola']) == (0, 65, False)
    plan = validate_plan(details(pension=1850.5, pension_age=62, pension_cola=True,
                                 partner_pension=900, partner_pension_age=200))
    # An age past the limits costs only that field.
    assert {key: value for key, value in plan['details'].items() if 'pension' in key} == {
        'pension': 1850.5, 'pension_age': 62, 'pension_cola': True, 'partner_pension': 900}
    inputs = plan_inputs(plan, figures(), TODAY)
    assert (inputs['pension'], inputs['pension_age'], inputs['pension_cola']) == \
        (1850.5, 62, True)


def test_a_typed_figure_replaces_the_accounts_figure():
    plan = details()
    plan['overrides'] = {'pretax': 5000}
    inputs = plan_inputs(plan, figures(pretax=100, roth=200), TODAY)
    assert (inputs['pretax'], inputs['roth']) == (5000, 200)


def test_an_age_the_planner_cannot_run_for_is_explained():
    with pytest.raises(ValueError, match='between 18 and 90'):
        plan_inputs(details(birth_year=1920), figures(), TODAY)


# -----------------------------------------------------------------------------
# Scenarios
# -----------------------------------------------------------------------------

def scenario(scenario_id='a', name='Retire at 52', **levers):
    return {'id': scenario_id, 'name': name, 'levers': levers or {'retire_age': 52}}


def test_scenarios_that_do_not_validate_are_dropped_one_at_a_time():
    clean = validate_plan({'scenarios': [
        scenario('a'),
        {'id': 'b', 'name': '   ', 'levers': {}},               # no name
        scenario('a', 'Same id again'),
        scenario('c', 'Odd lever', retire_age=200, spending=70000, stray=1),
        'not a scenario',
    ]})
    assert clean['scenarios'] == [
        scenario('a'),
        {'id': 'c', 'name': 'Odd lever', 'levers': {'spending': 70000}},
    ]


def test_a_fifth_scenario_is_refused_instead_of_pushing_one_out():
    """Runway made room by deleting the oldest without a word."""
    plan = empty_plan()
    for index in range(retirement.MAX_SCENARIOS):
        plan = retirement.add_scenario(plan, f"Scenario {index}", {}, f"id{index}")
    with pytest.raises(ValueError, match='room'):
        retirement.add_scenario(plan, 'One too many', {}, 'extra')
    assert len(plan['scenarios']) == retirement.MAX_SCENARIOS


def test_a_new_scenario_goes_first_with_its_name_trimmed():
    plan = retirement.add_scenario(empty_plan(), 'Older', {}, 'old')
    plan = retirement.add_scenario(plan, '  ' + 'x' * 60, {'spending': 50000}, 'new')
    assert [s['id'] for s in plan['scenarios']] == ['new', 'old']
    assert plan['scenarios'][0]['name'] == 'x' * retirement.SCENARIO_NAME_MAX
    with pytest.raises(ValueError, match='name'):
        retirement.add_scenario(plan, '   ', {}, 'blank')


def test_a_scenario_keeps_only_what_it_changed_and_follows_the_rest():
    """So when the plan's spending changes later, a scenario that only moved
    the retirement age is compared at the new spending."""
    base = plan_inputs(details(), figures(), TODAY)
    levers = retirement.changed_levers(base, retirement.apply_levers(base, {'retire_age': 52}))
    assert levers == {'retire_age': 52}

    later = plan_inputs(details(spending=90000), figures(), TODAY)
    assert retirement.apply_levers(later, levers)['spending'] == 90000


def test_adopting_settings_moves_a_partners_claim_age_with_yours():
    plan = details(partner=True, partner_birth_year=1980, partner_birth_month=7)
    adopted = retirement.adopt_levers(plan, {'ss_age': 70, 'retire_age': 58})
    assert adopted['details']['ss_age'] == adopted['details']['partner_ss_age'] == 70
    assert adopted['details']['retire_age'] == 58


TRIP = {'id': 'trip', 'name': 'Travel', 'amount': 10000, 'start': 0, 'years': 10}
CAR = {'id': 'car', 'name': 'New car', 'amount': 40000, 'start': 5, 'years': 1}


def test_extra_items_that_do_not_validate_are_dropped_one_at_a_time():
    clean = validate_plan(details(extras=[
        TRIP, {**CAR, 'amount': 'lots'}, {**TRIP, 'name': 'Same id'}, 'not an item',
        {**CAR, 'id': 'car2', 'name': '  Trimmed  '}]))
    assert clean['details']['extras'] == [TRIP, {**CAR, 'id': 'car2', 'name': 'Trimmed'}]


def test_a_plan_without_extra_items_has_none():
    assert plan_inputs(details(), figures(), TODAY)['extras'] == ()
    assert 'extras' not in validate_plan(details(extras=[]))['details']


def test_extra_items_reach_the_inputs_in_order():
    inputs = plan_inputs(details(extras=[TRIP, CAR]), figures(), TODAY)
    assert inputs['extras'] == (('trip', 'Travel', 10000, 0, 10),
                                ('car', 'New car', 40000, 5, 1))


def test_a_scenario_keeps_only_the_extra_amounts_it_changed():
    base = plan_inputs(details(extras=[TRIP, CAR]), figures(), TODAY)
    moved = retirement.apply_levers(base, {retirement.EXTRA_LEVER: {'trip': 5000, 'car': 40000}})
    assert retirement.changed_levers(base, moved) == {retirement.EXTRA_LEVER: {'trip': 5000}}


def test_deleting_an_extra_item_drops_it_from_every_scenario():
    plan = retirement.add_scenario(details(extras=[TRIP, CAR]), 'Cheaper',
                                   {'spending': 70000,
                                    retirement.EXTRA_LEVER: {'trip': 5000, 'car': 1}}, 's')
    assert plan['scenarios'][0]['levers'][retirement.EXTRA_LEVER] == {'trip': 5000, 'car': 1}
    plan['details']['extras'] = [CAR]
    assert validate_plan(plan)['scenarios'][0]['levers'] == {
        'spending': 70000, retirement.EXTRA_LEVER: {'car': 1}}


def test_adopting_extra_amounts_changes_only_those_amounts():
    adopted = retirement.adopt_levers(details(extras=[TRIP, CAR]),
                                      {'retire_age': 60, retirement.EXTRA_LEVER: {'car': 30000}})
    assert adopted['details']['extras'] == [TRIP, {**CAR, 'amount': 30000}]
    assert adopted['details']['retire_age'] == 60
    assert retirement.EXTRA_LEVER not in adopted['details']


def test_removing_a_scenario_leaves_the_others():
    plan = retirement.add_scenario(empty_plan(), 'Keep', {}, 'keep')
    plan = retirement.add_scenario(plan, 'Drop', {}, 'drop')
    assert [s['id'] for s in retirement.remove_scenario(plan, 'drop')['scenarios']] == ['keep']


# -----------------------------------------------------------------------------
# Storage
# -----------------------------------------------------------------------------

def test_a_fresh_vault_has_no_plan(vault):
    assert preferences.get_retirement_plan() is None
    assert retirement_service.load_plan() == empty_plan()


def test_a_saved_plan_round_trips(vault):
    retirement_service.save_plan(details(retire_age=60))
    assert retirement_service.load_plan() == validate_plan(details(retire_age=60))


def test_the_plan_never_reaches_disk_in_the_clear(vault):
    retirement_service.save_plan(details(spending=87654))
    raw = database.read_config('retirement_plan')
    assert raw and '87654' not in raw
    assert database.DB_PATH.read_bytes().find(b'87654') == -1


def test_a_tampered_row_reads_as_no_plan(vault):
    database.write_config('retirement_plan', '{"details": {"spending": 1}}')
    assert preferences.get_retirement_plan() is None


def test_reading_while_locked_raises_instead_of_reading_as_no_plan(vault):
    """Taken for "nothing saved", the next edit would store a plan of one
    field over the real one."""
    retirement_service.save_plan(details())
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        preferences.get_retirement_plan()


def test_saving_while_locked_is_refused(vault):
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        preferences.save_retirement_plan(details())


def test_the_plan_survives_a_master_password_change(vault):
    retirement_service.save_plan(details(spending=91000))
    assert auth.change_master_password(MASTER, NEW_MASTER)
    assert retirement_service.load_plan()['details']['spending'] == 91000
    encryption.release_key()
    assert auth.verify_master_password(NEW_MASTER)
    assert retirement_service.load_plan()['details']['spending'] == 91000


def test_the_plan_survives_a_work_factor_upgrade_at_login(vault, monkeypatch):
    retirement_service.save_plan(details(spending=91000))
    encryption.release_key()
    # Raising the work factors makes the next login re-key the whole vault.
    monkeypatch.setattr(encryption, 'ARGON2_TIME_COST', 2)
    assert auth.verify_master_password(MASTER)
    assert auth.read_vault_crypto()[1]['t'] == 2
    assert retirement_service.load_plan()['details']['spending'] == 91000


# -----------------------------------------------------------------------------
# Savings from the accounts
# -----------------------------------------------------------------------------

def investment(account_type, balance, **fields):
    return make_account(category='Investments', name=f"{account_type} {balance}",
                        account_type=account_type, current_balance=balance, **fields)


def bank(account_type, balance):
    return make_account(category='Banking', name=f"{account_type} {balance}",
                        account_type=account_type, current_balance=balance)


def test_each_account_type_lands_in_its_bucket(vault):
    investment('401k', 100, tax_treatment='Traditional')
    investment('403b', 200, tax_treatment='Roth')
    investment('Roth IRA', 300)
    investment('HSA', 400)
    investment('Brokerage', 500)
    bank('Checking', 60)
    bank('Savings', 70)
    bank('Money Market', 80)

    totals = retirement_service.savings_from_accounts()['figures']
    assert (totals['pretax'], totals['roth'], totals['hsa'], totals['taxable']) == \
        (100, 500, 400, 710)


def test_a_both_account_splits_by_its_two_amounts(vault):
    investment('401k', None, tax_treatment='Both', traditional_balance=1000,
               roth_balance=500, traditional_contribution=200, roth_contribution=100,
               contribution_period='Monthly')
    result = retirement_service.savings_from_accounts()
    totals = result['figures']
    assert (totals['pretax'], totals['roth']) == (1000, 500)
    assert (totals['save_pretax'], totals['save_roth']) == (2400, 1200)
    assert result['counted'][0]['notes'] == []


def test_parts_that_no_longer_add_up_are_scaled_and_marked_estimated(vault):
    account = investment('401k', None, tax_treatment='Both',
                         traditional_balance=1000, roth_balance=500)
    accounts_service.add_balance_entry(account['id'], '3000', date.today().isoformat())

    result = retirement_service.savings_from_accounts()
    assert (result['figures']['pretax'], result['figures']['roth']) == (2000, 1000)
    assert result['counted'][0]['notes'] == [retirement_service.ESTIMATED]


@pytest.mark.parametrize('fields', [{}, {'tax_treatment': 'Both'}])
def test_no_treatment_or_no_amounts_counts_as_traditional_and_says_so(vault, fields):
    investment('IRA', 64000, **fields)
    result = retirement_service.savings_from_accounts()
    assert result['figures']['pretax'] == 64000
    assert result['counted'][0]['notes'] == [retirement_service.ASSUMED]


def test_accounts_the_plan_cannot_place_are_listed_not_counted(vault):
    investment('Other', 21000)
    make_account(category='Investments', name='No type', current_balance=5)
    bank('Other', 30)
    make_account(category='Banking', name='Untyped bank', current_balance=40)
    bank('Checking', -50)

    result = retirement_service.savings_from_accounts()
    assert all(value == 0 for value in result['figures'].values())
    assert result['counted'] == []
    reasons = {row['name']: row['reason'] for row in result['skipped']}
    assert reasons == {
        'Other 21000': retirement_service.OTHER_TYPE,
        'No type': retirement_service.NO_TYPE,
        'Other 30': retirement_service.OTHER_TYPE,
        'Untyped bank': retirement_service.NO_TYPE,
        'Checking -50': retirement_service.OVERDRAWN,
    }


def test_closed_accounts_and_other_categories_are_left_out(vault):
    investment('401k', 100, closed=True)
    make_account(category='Credit Cards', name='Card', current_balance=900)
    result = retirement_service.savings_from_accounts()
    assert (result['counted'], result['skipped']) == ([], [])


def test_contributions_follow_the_balance_into_its_bucket(vault):
    investment('Roth IRA', 52000, contribution_amount=7000, contribution_period='Yearly')
    investment('Brokerage', 9400, contribution_amount=25, contribution_period='Weekly')
    totals = retirement_service.savings_from_accounts()['figures']
    assert (totals['save_roth'], totals['save_taxable']) == (7000, 1300)


def test_a_partners_account_pays_into_the_partners_contributions(vault):
    investment('Roth IRA', 52000, contribution_amount=7000, contribution_period='Yearly',
               whose='Partner')
    investment('Brokerage', 9400, contribution_amount=1000, contribution_period='Yearly')
    savings = retirement_service.savings_from_accounts()
    totals = savings['figures']
    assert (totals['save_roth'], totals['partner_save_roth'], totals['save_taxable']) == \
        (0, 7000, 1000)
    assert {row['name']: row['partner'] for row in savings['counted']} == \
        {'Roth IRA 52000': True, 'Brokerage 9400': False}


def test_a_contribution_without_a_period_is_noted_not_counted(vault):
    """The dashboard's rule: a period nobody picked is not guessed at."""
    investment('401k', 1000, tax_treatment='Traditional', contribution_amount=500)
    result = retirement_service.savings_from_accounts()
    assert result['figures']['save_pretax'] == 0
    assert result['counted'][0]['notes'] == [retirement_service.NO_PERIOD]

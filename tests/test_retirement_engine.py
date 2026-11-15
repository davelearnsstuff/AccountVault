# -----------------------------------------------------------------------------
# tests/test_retirement_engine.py
# core/retirement.py: the planner's tax, withdrawals, lifetime paths, markets
# and next best moves.
#
# The tax figures are worked out by hand from the 2026 tables, so a slip in the
# port shows up as a wrong number rather than as a number nobody checked. The
# sample household is pinned to what Runway itself shows for it: the engine is
# meant to give the same answer as the page it came from.
# -----------------------------------------------------------------------------

import math
from datetime import date

import pytest

from core import retirement
from core.retirement import (
    SAMPLE_HOUSEHOLD, TAX_TABLES, Balances, age_on, apply_levers, best_moves,
    best_moves_steps, bracket_top, federal_tax, market_draws, plan_year, run_path,
    sanitize, simulate, steady, validate_inputs, verdict,
)

JOINT = TAX_TABLES['joint']
SINGLE = TAX_TABLES['single']


def household(**changes) -> dict:
    """The sample household with nothing coming in or going out, then the
    changes a test is about. The partner retires the same year as you unless
    a test says otherwise, as a plan has it until the age is entered."""
    quiet = {
        'pretax': 0, 'roth': 0, 'taxable': 0, 'hsa': 0,
        'save_pretax': 0, 'save_roth': 0, 'save_taxable': 0, 'save_hsa': 0,
        'spending': 0, 'health': 0, 'part_time': 0, 'part_time_years': 0,
        'ss': 0, 'partner_ss': 0, 'convert': False,
    }
    inputs = {**SAMPLE_HOUSEHOLD, **quiet, **changes}
    if 'partner_retire_age' not in changes:
        inputs['partner_retire_age'] = retirement.same_year_retirement(inputs)
    return inputs


def flat(inputs: dict) -> list:
    """A real return of zero every year, so balances move only by what the
    household puts in and takes out."""
    return [0.0] * (retirement.plan_years(inputs) + 1)


def row_at(path, age: int) -> dict:
    return next(row for row in path.rows if row['age'] == age)


# -----------------------------------------------------------------------------
# Federal tax
# -----------------------------------------------------------------------------

def test_ordinary_income_fills_the_brackets_in_order():
    # Joint: 100,000 - 32,200 = 67,800 taxable. 24,800 at 10%, 43,000 at 12%.
    assert federal_tax(100_000, 0, 0, JOINT).tax == pytest.approx(7_640)
    # Single: 60,000 - 16,100 = 43,900. 12,400 at 10%, 31,500 at 12%.
    assert federal_tax(60_000, 0, 0, SINGLE).tax == pytest.approx(5_020)


def test_income_under_the_standard_deduction_is_untaxed_and_reports_the_room():
    """The leftover deduction is what a Roth conversion can fill tax-free."""
    result = federal_tax(20_000, 0, 0, JOINT)
    assert result.tax == 0
    assert result.ordinary_taxable == 0
    assert result.unused_deduction == 12_200


@pytest.mark.parametrize('ordinary, benefits, taxable_ss, tax', [
    # Provisional income 10,000 + 15,000 = 25,000, under 32,000: none taxed.
    (10_000, 30_000, 0, 0),
    # 25,000 + 12,000 = 37,000, between the thresholds: half the excess, 2,500.
    (25_000, 24_000, 2_500, 0),
    # 30,000 + 20,000 = 50,000: 85% of the 6,000 over 44,000, plus 6,000.
    (30_000, 40_000, 11_100, 890),
    # 60,000 + 20,000 = 80,000: the formula gives 36,600, capped at 85% of 40,000.
    (60_000, 40_000, 34_000, 6_920),
])
def test_social_security_is_taxed_on_provisional_income(ordinary, benefits, taxable_ss, tax):
    result = federal_tax(ordinary, 0, benefits, JOINT)
    assert result.tax == pytest.approx(tax)
    # The taxed share shows in how much of the deduction it used up.
    assert result.unused_deduction == pytest.approx(max(0, 32_200 - ordinary - taxable_ss))


def test_gains_inside_the_zero_band_are_untaxed():
    # 57,800 taxable, 17,800 of it ordinary. The 40,000 of gains all sit under
    # the 98,900 where the 0% band ends.
    assert federal_tax(50_000, 40_000, 0, JOINT).tax == pytest.approx(1_780)


def test_gains_past_the_zero_band_pay_15_percent():
    # 67,800 ordinary leaves 31,100 of the 0% band; the other 18,900 pays 15%.
    assert federal_tax(100_000, 50_000, 0, JOINT).tax == pytest.approx(7_640 + 2_835)


def test_gains_past_the_15_percent_band_pay_20_percent():
    # Ordinary taxable income of 583,900 is already past 545,500.
    added = federal_tax(600_000, 100_000, 0, SINGLE).tax - federal_tax(600_000, 0, 0, SINGLE).tax
    assert added == pytest.approx(20_000)


def test_bracket_top_finds_the_band_a_conversion_fills():
    assert bracket_top(JOINT, 0.12) == 100_800
    assert bracket_top(JOINT, 0.22) == 211_400
    assert bracket_top(SINGLE, 0.24) == 201_775
    assert bracket_top(JOINT, 0.15) == 0


# -----------------------------------------------------------------------------
# One year of withdrawals
# -----------------------------------------------------------------------------

def year(balances, need, *, social_security=0, work=0, rmd=0, age=60, **changes):
    inputs = household(retire_age=55, **changes)
    return plan_year(balances, need, social_security, work, rmd, inputs, age, JOINT, 0)


def test_withdrawals_come_from_taxable_then_pretax_then_roth_then_hsa():
    result = year(Balances(10_000, 10_000, 10_000, 10_000, 10_000), 35_000)
    assert (result.from_taxable, result.from_pretax, result.from_roth, result.from_hsa) == \
        (10_000, 10_000, 10_000, 5_000)
    assert result.balances == Balances(0, 0, 0, 0, 5_000)
    assert result.shortfall == 0


def test_a_need_beyond_every_account_is_a_shortfall():
    result = year(Balances(10_000, 10_000, 10_000, 10_000, 10_000), 50_000)
    assert result.shortfall == 10_000
    assert result.balances.total() == 0


def test_a_taxable_withdrawal_carries_its_share_of_gains():
    # 60% of the account is what was paid in, so 40% of each dollar is gain.
    result = year(Balances(0, 0, 100_000, 60_000, 0), 10_000)
    assert result.gains == pytest.approx(4_000)
    assert result.balances.basis == pytest.approx(54_000)
    assert result.balances.taxable == 90_000


def test_income_beyond_the_need_is_reinvested_as_new_basis():
    result = year(Balances(0, 0, 0, 0, 0), 30_000, social_security=50_000)
    assert result.reinvested == 20_000
    assert result.balances.taxable == 20_000
    assert result.balances.basis == 20_000


def test_the_required_distribution_is_taken_even_when_nothing_is_needed():
    result = year(Balances(100_000, 0, 0, 0, 0), 0, rmd=5_000, age=75)
    assert result.from_pretax == 5_000
    assert result.reinvested == 5_000
    assert result.balances.pretax == 95_000


def test_conversions_fill_the_chosen_bracket_and_the_unused_deduction():
    # No other income: the 32,200 deduction plus the 100,800 of the 12% band.
    result = year(Balances(1_000_000, 0, 0, 0, 0), 0, convert=True, convert_rate=0.12)
    assert result.conversion == 133_000
    assert result.balances.roth == 133_000
    assert result.tax == pytest.approx(2_480 + 76_000 * 0.12)

    wider = year(Balances(1_000_000, 0, 0, 0, 0), 0, convert=True, convert_rate=0.22)
    assert wider.conversion == 211_400 + 32_200


def test_conversions_stop_at_the_required_distribution_age():
    result = year(Balances(1_000_000, 0, 0, 0, 0), 0, convert=True, age=75)
    assert result.conversion == 0


# -----------------------------------------------------------------------------
# A lifetime
# -----------------------------------------------------------------------------

def test_contributions_go_in_every_year_until_retirement():
    inputs = household(age=50, retire_age=52, plan_to=55, roth=100_000,
                       save_pretax=1_000, save_roth=1_000, save_taxable=1_000)
    path = run_path(inputs, flat(inputs))
    assert path.totals == [103_000, 106_000, 106_000, 106_000, 106_000, 106_000]


def test_hsa_contributions_are_counted():
    """Runway had no input for them, so they would have been dropped."""
    inputs = household(age=50, retire_age=52, plan_to=55, save_hsa=4_300)
    assert run_path(inputs, flat(inputs)).totals[:3] == [4_300, 8_600, 8_600]


def test_running_out_records_the_age_and_empties_the_portfolio():
    inputs = household(age=60, retire_age=60, plan_to=70, roth=25_000, spending=10_000)
    path = run_path(inputs, flat(inputs), record=True)
    assert path.failed_at == 62
    assert path.totals[:4] == [15_000, 5_000, 0, 0]
    assert row_at(path, 62)['shortfall'] == 5_000


def test_social_security_starts_at_each_claim_age_with_its_factor():
    inputs = household(age=60, retire_age=60, plan_to=70, roth=10_000_000, spending=50_000,
                       partner=True, partner_age=58,
                       ss=2_000, ss_age=62, partner_ss=1_000, partner_ss_age=64)
    path = steady(inputs)
    assert row_at(path, 61)['social_security'] == 0
    # 2,000 a month claimed at 62 pays 70% of it.
    assert row_at(path, 62)['social_security'] == pytest.approx(16_800)
    assert row_at(path, 65)['social_security'] == pytest.approx(16_800)
    # The partner turns 64 when you turn 66, and is paid 80%.
    assert row_at(path, 66)['social_security'] == pytest.approx(16_800 + 9_600)


def test_health_premiums_are_added_until_medicare():
    inputs = household(age=60, retire_age=60, plan_to=70, roth=10_000_000,
                       spending=50_000, health=12_000)
    path = steady(inputs)
    assert row_at(path, 64)['need'] == 62_000
    assert row_at(path, 65)['need'] == 50_000


def test_part_time_work_runs_for_its_years_after_retiring():
    inputs = household(age=60, retire_age=60, plan_to=70, roth=10_000_000, spending=50_000,
                       part_time=20_000, part_time_years=2)
    path = steady(inputs)
    assert [row_at(path, age)['work'] for age in (60, 61, 62)] == [20_000, 20_000, 0]


def test_a_partners_pay_runs_until_the_partners_own_retirement():
    inputs = household(age=60, partner_age=58, retire_age=60, partner_retire_age=62,
                       plan_to=70, roth=10_000_000, spending=50_000, partner_pay=40_000)
    path = steady(inputs)
    # The partner turns 62 when you turn 64.
    assert [row_at(path, age)['partner_pay'] for age in (60, 63, 64)] == [40_000, 40_000, 0]
    alone = steady({**inputs, 'partner': False})
    assert all(row['partner_pay'] == 0 for row in alone.rows)


def test_a_partners_pay_pays_toward_spending_and_is_taxed():
    inputs = household(age=60, partner_age=60, retire_age=60, partner_retire_age=61,
                       plan_to=70, roth=10_000_000, spending=90_000)
    paid = row_at(steady({**inputs, 'partner_pay': 60_000}), 60)
    unpaid = row_at(steady(inputs), 60)
    assert paid['from_roth'] == pytest.approx(unpaid['from_roth'] - 60_000 + paid['tax'])
    assert paid['tax'] > 0


def test_a_pension_pays_from_its_start_age_for_life():
    inputs = household(age=60, retire_age=60, plan_to=70, roth=10_000_000, spending=50_000,
                       pension=2_000, pension_age=62, pension_cola=True)
    path = steady(inputs)
    assert row_at(path, 61)['pension'] == 0
    assert row_at(path, 62)['pension'] == pytest.approx(24_000)
    assert row_at(path, 70)['pension'] == pytest.approx(24_000)


def test_a_pension_that_starts_while_still_working_counts_from_retiring():
    inputs = household(age=50, retire_age=60, plan_to=70, roth=10_000_000, spending=50_000,
                       pension=1_000, pension_age=55, pension_cola=True)
    path = steady(inputs)
    assert row_at(path, 59)['pension'] == 0
    assert row_at(path, 60)['pension'] == pytest.approx(12_000)


def test_a_fixed_pension_buys_less_each_year_from_today():
    """The dollars stay the same, so in today's dollars the pension shrinks by
    inflation every year, starting now rather than when it starts paying."""
    inputs = household(age=60, retire_age=60, plan_to=70, roth=10_000_000, spending=50_000,
                       pension=2_000, pension_age=63, inflation=2.5)
    path = steady(inputs)
    assert row_at(path, 63)['pension'] == pytest.approx(24_000 / 1.025 ** 3)
    assert row_at(path, 64)['pension'] == pytest.approx(24_000 / 1.025 ** 4)
    assert retirement.pension_income(2_000, True, 4, 2.5) == 24_000


def test_a_partners_pension_starts_at_the_partners_own_age():
    inputs = household(age=60, partner_age=58, retire_age=60, plan_to=70,
                       roth=10_000_000, spending=50_000, partner_pension=1_000,
                       partner_pension_age=60, partner_pension_cola=True)
    path = steady(inputs)
    # The partner turns 60 when you turn 62.
    assert row_at(path, 61)['pension'] == 0
    assert row_at(path, 62)['pension'] == pytest.approx(12_000)
    alone = steady({**inputs, 'partner': False})
    assert all(row['pension'] == 0 for row in alone.rows)


def test_health_costs_switch_to_each_persons_medicare_costs_at_their_own_65():
    inputs = household(age=62, partner_age=60, retire_age=62, plan_to=72,
                       roth=10_000_000, spending=50_000, health=14_000,
                       partner_health=10_000, health_65=6_000, partner_health_65=7_000)
    path = steady(inputs)
    # Both premiums until you turn 65, then your Medicare costs beside your
    # partner's premium, until your partner turns 65 when you turn 67.
    assert row_at(path, 64)['need'] == 74_000
    assert row_at(path, 65)['need'] == 66_000
    assert row_at(path, 66)['need'] == 66_000
    assert row_at(path, 67)['need'] == 63_000
    alone = steady({**inputs, 'partner': False})
    assert row_at(alone, 67)['need'] == 56_000


def test_health_costs_from_65_start_from_a_suggested_figure():
    assert retirement.DETAIL_DEFAULTS['health_65'] == retirement.MEDICARE_COST
    assert retirement.DETAIL_DEFAULTS['partner_health_65'] == retirement.MEDICARE_COST


def test_a_pension_is_taxed_and_spent_like_part_time_pay():
    """Both are ordinary income: they push up the tax on Social Security and
    take room from conversions the same way, so the same amount from either
    gives the same plan."""
    base = household(age=60, retire_age=60, plan_to=70, pretax=600_000, taxable=200_000,
                     spending=70_000, ss=2_000, ss_age=62, convert=True)
    with_pension = steady({**base, 'pension': 2_000, 'pension_age': 60,
                           'pension_cola': True})
    with_work = steady({**base, 'part_time': 24_000, 'part_time_years': 20})
    assert with_pension.lifetime_tax == pytest.approx(with_work.lifetime_tax)
    assert with_pension.totals == pytest.approx(with_work.totals)
    assert [row['tax'] for row in with_pension.rows] == \
        pytest.approx([row['tax'] for row in with_work.rows])


def test_required_distributions_start_at_75():
    inputs = household(age=73, retire_age=73, plan_to=80, pretax=246_000)
    path = run_path(inputs, flat(inputs), record=True)
    assert row_at(path, 74)['rmd'] == 0
    assert row_at(path, 75)['rmd'] == pytest.approx(246_000 / 24.6)


def test_the_steady_path_grows_by_the_real_return():
    inputs = household(age=50, retire_age=60, plan_to=55, roth=100_000)
    growth = 1.06 / 1.025
    assert steady({**inputs, 'return_mean': 6, 'inflation': 2.5}).totals[0] == \
        pytest.approx(100_000 * growth)


# -----------------------------------------------------------------------------
# Markets and odds
# -----------------------------------------------------------------------------

def test_the_same_household_gets_the_same_odds_every_time():
    """The markets are seeded, so two plans are always compared on the same
    ones and a difference between them is never luck."""
    assert simulate(SAMPLE_HOUSEHOLD) == simulate(dict(SAMPLE_HOUSEHOLD))


def test_percentiles_are_in_order_at_every_age():
    result = simulate(SAMPLE_HOUSEHOLD)
    assert len(result.ages) == len(result.p50) == 95 - 48 + 1
    assert all(low <= middle <= high
               for low, middle, high in zip(result.p10, result.p50, result.p90))


def test_a_plan_longer_than_70_years_can_still_run_out():
    """Runway drew 70 years of returns. Past them every balance became NaN,
    and NaN is never short of money, so this household counted as lasting.

    Zero volatility makes every market the steady one: 700,000 at 10,000 a
    year from 30 lasts through 99 and runs out at 100, year 80 of the plan."""
    inputs = household(age=20, retire_age=30, plan_to=105, roth=700_000, spending=10_000,
                       return_mean=2.5, inflation=2.5, volatility=0)
    assert steady(inputs).failed_at == 100
    result = simulate(inputs)
    assert result.success == 0
    assert all(math.isfinite(value) for value in result.p10 + result.p50 + result.p90)


def test_longer_markets_start_with_the_draws_runway_used():
    """Growing the markets past 70 years must not change the first 70, or
    every plan Runway could run would get a different answer here."""
    assert market_draws(retirement.SIM_PATHS, 90)[3][:71] == retirement._normal_row(1003, 70)


def test_the_sample_household_matches_runway():
    """Runway rounds these to 69% on its Plan page and $131K on Withdrawals &
    taxes. If this fails, compare with docs/runway.html before changing it."""
    assert simulate(SAMPLE_HOUSEHOLD).success == 0.693
    path = steady(SAMPLE_HOUSEHOLD)
    assert path.lifetime_tax == pytest.approx(131_002.01, abs=0.01)
    assert path.totals[-1] == pytest.approx(1_885_780.28, abs=0.01)


@pytest.mark.parametrize('success, tone', [
    (0.85, 'good'), (0.849, 'warn'), (0.65, 'warn'), (0.649, 'bad'),
])
def test_verdict_thresholds(success, tone):
    assert verdict(success) == tone


# -----------------------------------------------------------------------------
# Next best moves
# -----------------------------------------------------------------------------

def test_the_sample_household_gets_runways_moves():
    """Runway lists the first four: work 3 more years, spend $12K less,
    convert about $101K in the first retired year, and delay Social Security
    for 4 more points.

    All but the first match. Runway moved the household together; here each
    person works on alone, and while the other is retired the household draws
    on its savings, with only the pay entered coming in (none, in the sample).
    So you work 10 more years, to 65, and no year of your partner's is
    enough."""
    moves = best_moves(SAMPLE_HOUSEHOLD, simulate(SAMPLE_HOUSEHOLD).success)
    assert [move['kind'] for move in moves] == [
        'work_longer', 'spend_less', 'conversions', 'delay_social_security',
        'health_until_medicare',
    ]
    work, spend, convert, delay, health = moves
    assert (work['years'], work['retire_age'], work['partner']) == (10, 65, False)
    assert (spend['cut'], spend['spending']) == (12_000, 72_000)
    assert (convert['first'], convert['years']) == (100_800, 11)
    assert convert['tax_saved'] == pytest.approx(62_797.41, abs=0.01)
    assert convert['gain'] == pytest.approx(36_785.52, abs=0.01)
    assert delay['points'] == 4
    assert (health['years'], health['premium']) == (10, 14_000)


def test_working_longer_is_the_fewest_years_that_reach_the_target():
    moves = best_moves(SAMPLE_HOUSEHOLD, simulate(SAMPLE_HOUSEHOLD).success)
    target = moves[0]['retire_age']

    def odds(retire_age):
        return simulate({**SAMPLE_HOUSEHOLD, 'retire_age': retire_age},
                        retirement.MOVE_PATHS).success

    assert odds(target) >= retirement.ON_TRACK > odds(target - 1)


def test_a_plan_on_track_looks_for_an_earlier_retirement():
    inputs = {**SAMPLE_HOUSEHOLD, 'pretax': 2_000_000}
    success = simulate(inputs).success
    assert success >= retirement.ON_TRACK
    earlier = best_moves(inputs, success)[0]
    assert earlier['kind'] == 'retire_earlier'
    assert earlier['retire_age'] < inputs['retire_age']
    assert simulate({**inputs, 'retire_age': earlier['retire_age']},
                    retirement.MOVE_PATHS).success >= retirement.ON_TRACK


def test_the_moves_search_pauses_after_each_simulation():
    """What lets the window run the search a step at a time. This household
    is on track and already retired, so the only probes are the two behind
    delaying Social Security."""
    inputs = household(age=60, retire_age=60, plan_to=90, roth=5_000_000,
                       spending=40_000, ss=2_000, ss_age=69)
    success = simulate(inputs).success
    steps = best_moves_steps(inputs, success)
    pauses = 0
    while True:
        try:
            next(steps)
        except StopIteration as done:
            moves = done.value
            break
        pauses += 1
    assert pauses == 2
    assert moves == best_moves(inputs, success)


def test_halves_round_up_as_in_runway():
    """Python's round() sends a half to the even neighbour, so a 2.5-point
    gain from delaying Social Security would come out as 2."""
    assert retirement._js_round(2.5) == 3
    assert retirement._js_round(1.5) == 2
    assert retirement._js_round(-0.5) == 0


# -----------------------------------------------------------------------------
# Inputs
# -----------------------------------------------------------------------------

def test_sanitize_puts_the_ages_in_order_without_changing_its_argument():
    inputs = household(age=60, plan_to=62, retire_age=70, partner=False, partner_age=40)
    clean = sanitize(inputs)
    assert clean['plan_to'] == 65
    assert clean['retire_age'] == 64
    assert clean['partner_age'] == 60
    assert inputs['plan_to'] == 62


def test_a_partner_claims_with_you_when_a_lever_moves_the_claim_age():
    assert apply_levers(SAMPLE_HOUSEHOLD, {'ss_age': 70})['partner_ss_age'] == 70
    alone = {**SAMPLE_HOUSEHOLD, 'partner': False}
    assert apply_levers(alone, {'ss_age': 70})['partner_ss_age'] == 67


def test_levers_leave_the_other_inputs_alone():
    changed = apply_levers(SAMPLE_HOUSEHOLD, {'retire_age': 52, 'spending': 90_000})
    assert (changed['retire_age'], changed['spending']) == (52, 90_000)
    assert changed['pretax'] == SAMPLE_HOUSEHOLD['pretax']


# -----------------------------------------------------------------------------
# Two people retiring at different ages
# -----------------------------------------------------------------------------

def couple(**changes) -> dict:
    """You 50 and your partner 50, retiring at 60 and 55, with plenty in a
    Roth so nothing runs out, and no market growth."""
    return household(**{'age': 50, 'partner_age': 50, 'retire_age': 60,
                        'partner_retire_age': 55, 'plan_to': 70, 'roth': 10_000_000,
                        'spending': 50_000, **changes})


def test_drawing_starts_when_the_first_of_you_retires():
    inputs = couple()
    path = run_path(inputs, flat(inputs), record=True)
    assert [row['age'] for row in path.rows if row['retired']][0] == 55
    assert row_at(path, 55)['need'] == 50_000


def test_your_pay_counts_until_you_retire_less_the_contributions_it_covers():
    inputs = couple(pay=100_000, save_pretax=20_000, spending=0)
    path = run_path(inputs, flat(inputs), record=True)
    assert (row_at(path, 59)['pay'], row_at(path, 60)['pay']) == (80_000, 0)
    assert row_at(path, 59)['pretax_end'] == 10 * 20_000   # every year to 60


def test_contributions_beyond_the_pay_entered_stop():
    """With no pay entered, nothing pays for them once the household draws."""
    inputs = couple(save_pretax=20_000, spending=0)
    path = run_path(inputs, flat(inputs), record=True)
    assert row_at(path, 54)['pretax_end'] == 5 * 20_000    # both working
    assert row_at(path, 59)['pretax_end'] == 5 * 20_000    # no pay, so none go in


def test_each_persons_contributions_stop_at_their_own_retirement():
    inputs = couple(retire_age=55, partner_retire_age=65, partner_save_pretax=5_000,
                    partner_pay=60_000, spending=0)
    path = run_path(inputs, flat(inputs), record=True)
    assert (row_at(path, 64)['partner_pay'], row_at(path, 65)['partner_pay']) == (55_000, 0)
    assert row_at(path, 64)['pretax_end'] == row_at(path, 69)['pretax_end'] == 15 * 5_000


def test_each_persons_health_costs_start_at_their_own_retirement():
    inputs = couple(health=10_000, partner_health=4_000, pay=200_000)
    path = run_path(inputs, flat(inputs), record=True)
    assert row_at(path, 55)['need'] == 50_000 + 4_000
    assert row_at(path, 60)['need'] == 50_000 + 4_000 + 10_000


def test_part_time_work_waits_for_your_own_retirement():
    inputs = couple(part_time=30_000, part_time_years=3)
    path = run_path(inputs, flat(inputs), record=True)
    assert [row['age'] for row in path.rows if row['work']] == [60, 61, 62]


def test_a_plan_for_one_counts_a_partners_contributions_as_yours():
    inputs = household(partner=False, age=50, retire_age=55, plan_to=70,
                       partner_save_roth=6_000)
    path = run_path(inputs, flat(inputs), record=True)
    assert path.totals[4] == 5 * 6_000


def test_moves_are_searched_for_each_person():
    """Only your partner's pay keeps this plan going, so only your partner
    working longer is suggested, and it moves your partner's age."""
    inputs = household(age=55, partner_age=55, retire_age=55, partner_retire_age=56,
                       plan_to=90, roth=1_100_000, spending=60_000, partner_pay=150_000)
    success = simulate(inputs).success
    assert success < retirement.ON_TRACK
    work = [move for move in best_moves(inputs, success) if move['kind'] == 'work_longer']
    assert [move['partner'] for move in work] == [True]
    assert work[0]['retire_age'] > 56


# -----------------------------------------------------------------------------
# Extra spending
# -----------------------------------------------------------------------------

def test_an_extra_item_adds_its_amount_for_exactly_its_years():
    """Years count from retiring: start 2 for 3 years is the third to fifth
    years of retirement, nothing before and nothing after."""
    inputs = household(age=60, retire_age=62, plan_to=75, taxable=1_000_000, basis_pct=100,
                       spending=40_000, health_65=0,
                       extras=(('trip', 'Travel', 10_000, 2, 3),))
    path = run_path(inputs, flat(inputs), record=True)
    extra = {row['age']: row['need'] - 40_000 for row in path.rows
             if row['retired'] and row['need'] != 40_000}
    assert extra == {64: 10_000, 65: 10_000, 66: 10_000}


def test_extra_years_past_the_plan_are_not_counted():
    inputs = sanitize(household(age=60, retire_age=60, plan_to=70,
                                extras=(('car', 'Car', 30_000, 9, 5),)))
    assert sum(retirement.extra_spending(inputs)) == 60_000   # ages 69 and 70


def test_overlapping_items_add_up():
    inputs = sanitize(household(age=60, retire_age=60, plan_to=70, extras=(
        ('a', 'Travel', 10_000, 0, 3), ('b', 'Car', 40_000, 1, 1))))
    assert retirement.extra_spending(inputs)[:4] == [10_000, 50_000, 10_000, 0]


def test_extra_items_follow_the_retirement_age():
    """The point of counting from retiring: retire later and the trip moves."""
    inputs = household(age=60, retire_age=62, plan_to=75, extras=(('a', 'Trip', 5_000, 0, 1),))
    later = apply_levers(inputs, {'retire_age': 65})
    assert retirement.extra_spending(sanitize(inputs)).index(5_000) == 2
    assert retirement.extra_spending(later).index(5_000) == 5


def test_a_lever_changes_only_the_named_items_amount():
    inputs = household(extras=(('a', 'Travel', 10_000, 0, 5), ('b', 'Car', 40_000, 3, 1)))
    changed = apply_levers(inputs, {retirement.EXTRA_LEVER: {'a': 2_500, 'gone': 1}})
    assert changed['extras'] == (('a', 'Travel', 2_500, 0, 5), ('b', 'Car', 40_000, 3, 1))


@pytest.mark.parametrize('extras', [
    (('a', '', 1_000, 0, 1),),                            # no name
    (('a', 'Trip', -1, 0, 1),),                           # a negative amount
    (('a', 'Trip', 1_000, 0, 0),),                        # no years
    (('a', 'Trip', 1_000, 0.5, 1),),                      # part of a year
    (('a', 'Trip', 1_000, 0, 1), ('a', 'Car', 1, 0, 1)),  # the same id twice
    tuple((f"x{n}", 'Trip', 1, 0, 1) for n in range(retirement.MAX_EXTRAS + 1)),
])
def test_extra_items_the_plan_cannot_run_on_are_refused(extras):
    with pytest.raises(ValueError):
        validate_inputs({**SAMPLE_HOUSEHOLD, 'extras': extras})


def test_the_sample_household_is_valid_input():
    assert validate_inputs(SAMPLE_HOUSEHOLD) == sanitize(SAMPLE_HOUSEHOLD)


def test_validation_drops_unknown_keys_and_makes_whole_numbers_whole():
    clean = validate_inputs({**SAMPLE_HOUSEHOLD, 'age': 48.0, 'stray': 1})
    assert clean['age'] == 48 and isinstance(clean['age'], int)
    assert 'stray' not in clean


@pytest.mark.parametrize('changes', [
    {'age': True},
    {'age': 48.5},
    {'age': 17},
    {'plan_to': 106},
    {'ss_age': 61},
    {'basis_pct': 101},
    {'hsa': -1},
    {'spending': '84000'},
    {'spending': float('nan')},
    {'pretax': float('inf')},
    {'partner': 1},
    {'convert_rate': 0.15},
], ids=lambda changes: ', '.join(f'{key}={value!r}' for key, value in changes.items()))
def test_validation_refuses_values_the_plan_cannot_run_on(changes):
    with pytest.raises(ValueError):
        validate_inputs({**SAMPLE_HOUSEHOLD, **changes})


def test_validation_refuses_a_missing_input_or_a_non_dict():
    incomplete = dict(SAMPLE_HOUSEHOLD)
    del incomplete['spending']
    with pytest.raises(ValueError):
        validate_inputs(incomplete)
    with pytest.raises(ValueError):
        validate_inputs([])


def test_age_counts_the_birth_month_as_reached():
    today = date(2026, 10, 18)
    assert age_on(1978, 3, today) == 48
    assert age_on(1978, 10, today) == 48
    assert age_on(1978, 11, today) == 47


@pytest.mark.parametrize('year, month', [(1978, 0), (1978, 13), (1978, True), (2026, 11)])
def test_age_refuses_a_birth_month_or_year_that_cannot_be(year, month):
    with pytest.raises(ValueError):
        age_on(year, month, date(2026, 10, 18))

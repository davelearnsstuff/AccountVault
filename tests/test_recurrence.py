# -----------------------------------------------------------------------------
# tests/test_recurrence.py
# Due dates that repeat on something other than a month.
#
# The property worth holding onto is that clamping never accumulates: every
# occurrence is rebuilt from the anchor, so a date due on the 31st borrows the
# 28th for February and hands the 31st back in March. The old monthly-only code
# had that property and this must not lose it while gaining three more cycles.
#
# `today` is frozen where it matters, because 'the next occurrence' is a claim
# about a date relative to now and a test that reads the real clock passes or
# fails depending on the day it is run.
# -----------------------------------------------------------------------------

import datetime

import pytest

import categories
from categories import (
    ANNUAL, DUE_CYCLES, MONTHLY, ONE_OFF, QUARTERLY, WEEKLY, advance_cycle,
    cycle_noun, cycle_of, due_date_level, is_recurring, next_due_date,
)


@pytest.fixture
def today(monkeypatch):
    """Pins datetime.date.today() to a fixed Thursday, 2026-09-17."""
    fixed = datetime.date(2026, 9, 17)

    class _Date(datetime.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr(categories.datetime, 'date', _Date)
    return fixed


# -- the vocabulary -----------------------------------------------------------

def test_the_ladder_reads_in_ascending_period_order():
    assert DUE_CYCLES == [ONE_OFF, WEEKLY, MONTHLY, QUARTERLY, ANNUAL]


@pytest.mark.parametrize('stored, expected', [
    ('Weekly', WEEKLY), ('weekly', WEEKLY), ('week', WEEKLY),
    ('Monthly', MONTHLY), ('MONTHLY', MONTHLY),
    ('Quarterly', QUARTERLY), ('quarter', QUARTERLY),
    ('Annually', ANNUAL), ('annual', ANNUAL), ('yearly', ANNUAL),
    ('One-off', None), ('', None), (None, None), ('fortnightly', None),
])
def test_a_stored_cycle_is_read_by_any_of_its_spellings(stored, expected):
    """Aliases are read on the way in and never written on the way out, the
    same arrangement CATEGORY_ALIASES has — a vault carrying 'yearly' from an
    import behaves as one that says 'Annually'."""
    assert cycle_of(stored) == expected


@pytest.mark.parametrize('cycle, recurring', [
    (WEEKLY, True), (MONTHLY, True), (QUARTERLY, True), (ANNUAL, True),
    (ONE_OFF, False), ('', False), (None, False),
])
def test_is_recurring_still_answers_for_every_cycle(cycle, recurring):
    assert is_recurring(cycle) is recurring


def test_every_category_with_a_due_date_offers_the_whole_ladder():
    for name, config in categories.CATEGORY_CONFIG.items():
        fields = {f['key']: f for f in config['fields']}
        if 'due_date' not in fields:
            continue
        cycle = fields[categories.DUE_CYCLE_KEY]
        assert cycle['options'] == DUE_CYCLES, name
        assert cycle['default'] == MONTHLY, name


# -- next occurrence ----------------------------------------------------------

def test_a_one_off_is_its_own_next_occurrence(today):
    assert next_due_date('2020-01-05', ONE_OFF) == '2020-01-05'
    assert next_due_date('2020-01-05', None) == '2020-01-05'


def test_a_date_still_ahead_is_left_where_it_is(today):
    for cycle in DUE_CYCLES:
        assert next_due_date('2026-12-25', cycle) == '2026-12-25'


def test_a_blank_date_has_no_occurrence(today):
    assert next_due_date('', MONTHLY) is None
    assert next_due_date(None, ANNUAL) is None
    assert next_due_date('not a date', WEEKLY) is None


def test_weekly_lands_on_the_anchors_weekday(today):
    # 2026-01-05 is a Monday; the first Monday on or after 2026-09-17.
    occurrence = next_due_date('2026-01-05', WEEKLY)

    assert occurrence == '2026-09-21'
    assert datetime.date.fromisoformat(occurrence).weekday() == 0


def test_a_weekly_anchor_a_whole_number_of_weeks_back_lands_on_today(today):
    """Rounding up rather than down: an anchor exactly n weeks behind is due
    today, not a week ago."""
    anchor = (today - datetime.timedelta(days=21)).isoformat()

    assert next_due_date(anchor, WEEKLY) == today.isoformat()


def test_monthly_keeps_the_anchors_day(today):
    assert next_due_date('2024-03-08', MONTHLY) == '2026-10-08'
    assert next_due_date('2024-03-25', MONTHLY) == '2026-09-25'


def test_quarterly_steps_three_months_from_the_anchor(today):
    # Anchored in January: January, April, July, October.
    assert next_due_date('2024-01-15', QUARTERLY) == '2026-10-15'
    # Anchored in February: February, May, August, November.
    assert next_due_date('2024-02-15', QUARTERLY) == '2026-11-15'


def test_annually_keeps_the_anchors_month_and_day(today):
    assert next_due_date('2020-03-01', ANNUAL) == '2027-03-01'
    assert next_due_date('2020-11-30', ANNUAL) == '2026-11-30'


def test_the_end_of_month_clamp_never_accumulates(monkeypatch):
    """The whole reason each occurrence is rebuilt from the anchor. Walked
    forward a month at a time, a date due on the 31st comes back from February
    as the 28th and stays there for the rest of the account's life."""
    anchor = '2026-01-31'
    expected = {
        (2026, 2, 1): '2026-02-28',   # February borrows the 28th …
        (2026, 3, 1): '2026-03-31',   # … and March takes the 31st back
        (2026, 4, 1): '2026-04-30',
        (2026, 5, 1): '2026-05-31',
    }
    for (year, month, day), occurrence in expected.items():
        fixed = datetime.date(year, month, day)

        class _Date(datetime.date):
            @classmethod
            def today(cls, _fixed=fixed):
                return _fixed

        monkeypatch.setattr(categories.datetime, 'date', _Date)
        assert next_due_date(anchor, MONTHLY) == occurrence


def test_a_leap_day_anchor_survives_the_years_that_have_no_leap_day(monkeypatch):
    for year, occurrence in ((2026, '2026-02-28'), (2027, '2027-02-28'),
                             (2028, '2028-02-29')):
        fixed = datetime.date(year, 2, 1)

        class _Date(datetime.date):
            @classmethod
            def today(cls, _fixed=fixed):
                return _fixed

        monkeypatch.setattr(categories.datetime, 'date', _Date)
        assert next_due_date('2024-02-29', ANNUAL) == occurrence


# -- urgency ------------------------------------------------------------------

@pytest.mark.parametrize('cycle', [WEEKLY, MONTHLY, QUARTERLY, ANNUAL])
def test_a_recurring_date_can_never_be_lapsed(today, cycle):
    """next_due_date always answers with a day still ahead, which is the whole
    point of recording a cycle — 'lapsed' says the record was never rolled
    forward, and a recurring one rolls itself."""
    assert due_date_level('2015-01-01', cycle) != 'lapsed'


def test_a_one_off_long_past_is_lapsed_rather_than_urgent(today):
    assert due_date_level('2015-01-01', ONE_OFF) == 'lapsed'


def test_the_occurrence_still_ahead_is_what_sets_the_colour(today):
    # 2026-09-20 is three days off, which is 'danger' whatever year the anchor
    # was written in.
    assert due_date_level('2020-09-20', ANNUAL) == 'danger'
    # 2026-09-30 is thirteen days off: 'warn'.
    assert due_date_level('2020-09-30', ANNUAL) == 'warn'


# -- the +1 button ------------------------------------------------------------

@pytest.mark.parametrize('cycle, noun', [
    (WEEKLY, 'week'), (MONTHLY, 'month'), (QUARTERLY, 'quarter'),
    (ANNUAL, 'year'), (ONE_OFF, 'month'), (None, 'month'),
])
def test_the_button_is_named_after_one_period_of_the_cycle(cycle, noun):
    assert cycle_noun(cycle) == noun


@pytest.mark.parametrize('cycle, expected', [
    (WEEKLY, '2026-10-08'),
    (MONTHLY, '2026-11-01'),
    (QUARTERLY, '2027-01-01'),
    (ANNUAL, '2027-10-01'),
])
def test_advancing_moves_on_by_one_period_from_the_occurrence_showing(today, cycle,
                                                                      expected):
    """Anchored on what the user is looking at — the occurrence still ahead —
    so one press moves the account on by exactly one period from there."""
    assert advance_cycle('2026-10-01', cycle) == expected


def test_a_one_off_advances_by_a_month(today):
    """Which is what the button did before any other cycle existed."""
    assert advance_cycle('2026-10-01', ONE_OFF) == '2026-11-01'


def test_advancing_from_nothing_means_one_period_from_today(today):
    assert advance_cycle(None, WEEKLY) == '2026-09-24'
    assert advance_cycle('', QUARTERLY) == '2026-12-17'


def test_advancing_a_recurring_date_starts_from_the_next_occurrence(today):
    """Not from the anchor on file. A card anchored in 2024 and pressed once
    should move to the month after the one currently showing, not to 2024."""
    assert next_due_date('2024-03-08', MONTHLY) == '2026-10-08'
    assert advance_cycle('2024-03-08', MONTHLY) == '2026-11-08'

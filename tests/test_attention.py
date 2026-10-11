# -----------------------------------------------------------------------------
# tests/test_attention.py
# The Needs Attention verdict and the dashboard's change lines.
#
# Both are pure functions over data the vault already holds, so they are tested
# directly rather than through the widgets that show them.
# -----------------------------------------------------------------------------

import calendar
import datetime

import pytest

from core.categories import (
    LAPSED_AFTER_DAYS, attention_counts, attention_level, cycle_of,
    due_date_level, needs_attention, next_due_date,
)
from services.dashboard import change_across, trailing_window
# Presentation only — how the delta is worded and which colour it takes. All
# are pure functions of the numbers above, so they cost nothing to check here
# and none of them touches a widget.
from ui.dashboard_view import _basis_text
from ui.ranges import day_name, delta_object_name, delta_text, month_name


def days_away(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


# -- attention ----------------------------------------------------------------

# A category that declares a staleness interval. Every assertion about staleness
# has to name one now: how long a figure stays believable is the category's call
# rather than one number for the whole vault.
STALE_CATEGORY = 'Credit Cards'      # (21, 28)


@pytest.mark.parametrize('due_in, expected', [
    (-5,  'danger'),   # overdue
    (0,   'danger'),   # today
    (7,   'danger'),   # the boundary of "this week"
    (8,   'warn'),
    (14,  'warn'),     # the boundary of "soon"
    (15,  ''),         # far enough out to be nobody's problem yet
    (400, ''),
])
def test_due_dates_set_the_level(due_in, expected):
    assert attention_level({'due_date': days_away(due_in)}) == expected


@pytest.mark.parametrize('updated, expected', [
    (0,   ''),
    (20,  ''),
    (21,  'warn'),     # three weeks
    (27,  'warn'),
    (28,  'danger'),   # four weeks
    (365, 'danger'),
])
def test_staleness_sets_the_level(updated, expected):
    account = {'category': STALE_CATEGORY, 'last_updated': days_ago(updated)}
    assert attention_level(account) == expected


@pytest.mark.parametrize('category, updated, expected', [
    ('Credit Cards', 30,  'danger'),   # a statement a month
    ('Banking',      30,  'danger'),
    ('Investments',  30,  ''),         # nobody rebalances monthly
    ('Investments',  95,  'danger'),
    ('Loans',        30,  ''),         # a fixed payment is not news monthly
    ('Income',       30,  ''),         # annual pay is current for a year
    ('Income',       200, 'warn'),
    ('Income',       400, 'danger'),
])
def test_staleness_thresholds_come_from_the_category(category, updated, expected):
    account = {'category': category, 'last_updated': days_ago(updated)}
    assert attention_level(account) == expected


@pytest.mark.parametrize('category', ['Secure Notes', 'Websites', 'Something Custom'])
def test_categories_without_an_interval_never_go_stale(category):
    """A password is as correct as the day it was filed, and a note does not go
    out of date by being left alone. All three used to run the credit-card
    clock, which left a three-year-old note permanently urgent and buried the
    accounts that genuinely needed doing."""
    account = {'category': category, 'last_updated': days_ago(4000)}
    assert attention_level(account) == ''
    assert not needs_attention(account)


def test_the_worse_of_the_two_signals_wins():
    both = {'category': STALE_CATEGORY,
            'due_date': days_away(10), 'last_updated': days_ago(30)}
    assert attention_level(both) == 'danger'   # warn (due) vs danger (stale)

    other_way = {'category': STALE_CATEGORY,
                 'due_date': days_away(2), 'last_updated': days_ago(22)}
    assert attention_level(other_way) == 'danger'


def test_closed_accounts_never_need_attention():
    """A closed card with an old due date is history, not a task."""
    account = {'category': STALE_CATEGORY, 'due_date': days_away(-30),
               'last_updated': days_ago(400), 'closed': True}
    assert attention_level(account) == ''
    assert not needs_attention(account)


def test_an_account_with_no_dates_is_quiet():
    assert attention_level({}) == ''
    assert attention_level({'due_date': None, 'last_updated': ''}) == ''
    assert attention_level({'due_date': 'not a date'}) == ''


# -- recurring due dates ------------------------------------------------------

@pytest.mark.parametrize('cycle, expected', [
    ('Monthly', True),
    ('monthly', True),
    (' Monthly ', True),
    ('One-off', False),
    ('', False),
    (None, False),
])
def test_is_recurring_reads_the_stored_cycle(cycle, expected):
    assert (cycle_of(cycle) is not None) is expected


def test_a_one_off_is_its_own_next_occurrence():
    """Absent is the default for every record written before cycles existed, so
    this is the path that has to stay identical to the old behaviour."""
    anchor = days_away(-700)
    assert next_due_date(anchor, '') == anchor
    assert next_due_date(anchor, None) == anchor
    assert next_due_date(days_away(30), '') == days_away(30)


def test_a_monthly_date_rolls_to_the_occurrence_still_ahead():
    today = datetime.date.today()
    rolled = datetime.date.fromisoformat(next_due_date(days_away(-700), 'Monthly'))
    assert rolled >= today
    # The earliest such occurrence: a month earlier would be behind us.
    assert (rolled.year, rolled.month) in {
        (today.year, today.month),
        (today.year + (today.month == 12), today.month % 12 + 1),
    }


def test_a_future_date_is_left_where_it_is():
    assert next_due_date(days_away(20), 'Monthly') == days_away(20)


def test_a_month_end_anchor_does_not_creep_backwards():
    """add_one_month clamps the 31st to the 28th and never recovers it, so
    stepping forward a month at a time would leave a card due on the 31st
    permanently due on the 28th. Each occurrence is rebuilt from the anchor."""
    rolled = datetime.date.fromisoformat(next_due_date('2020-01-31', 'Monthly'))
    assert rolled.day == min(31, calendar.monthrange(rolled.year, rolled.month)[1])
    assert rolled >= datetime.date.today()


def test_a_recurring_date_can_never_lapse():
    """Which is the whole point of recording the cycle: the app rolls it
    forward, so there is no such thing as one nobody rolled forward."""
    assert due_date_level(days_away(-700), 'Monthly', STALE_CATEGORY) != 'lapsed'
    assert due_date_level(days_away(-4000), 'Monthly', STALE_CATEGORY) != 'lapsed'


def test_a_long_dead_one_off_reads_as_lapsed_rather_than_urgent():
    assert due_date_level(days_away(-(LAPSED_AFTER_DAYS + 1)), '', STALE_CATEGORY) == 'lapsed'
    # Still inside the window is still a bill someone can act on today.
    assert due_date_level(days_away(-LAPSED_AFTER_DAYS), '', STALE_CATEGORY) == 'danger'


def test_a_lapsed_date_still_lists():
    account = {'due_date': days_away(-400)}
    assert attention_level(account) == 'lapsed'
    assert needs_attention(account)


def test_lapsed_never_outranks_a_live_signal():
    """A years-old due date must not shout down a balance that has genuinely
    gone stale this month — that is what the rank below 'warn' buys."""
    account = {'category': STALE_CATEGORY,
               'due_date': days_away(-400), 'last_updated': days_ago(30)}
    assert attention_level(account) == 'danger'

    milder = {'category': STALE_CATEGORY,
              'due_date': days_away(-400), 'last_updated': days_ago(22)}
    assert attention_level(milder) == 'warn'


# -- counts -------------------------------------------------------------------

def test_counts_do_not_double_count_a_row_with_two_problems():
    accounts = [
        {'category': STALE_CATEGORY, 'due_date': days_away(3), 'last_updated': days_ago(40)},
        {'category': STALE_CATEGORY, 'due_date': days_away(3), 'last_updated': days_ago(1)},
        {'category': STALE_CATEGORY, 'due_date': None, 'last_updated': days_ago(25)},
        {'category': STALE_CATEGORY, 'due_date': days_away(90), 'last_updated': days_ago(1)},
        {'category': STALE_CATEGORY, 'due_date': days_away(1), 'closed': True},
    ]
    counts = attention_counts(accounts)

    assert counts['total'] == 3       # rows in the list
    assert counts['due'] == 2
    assert counts['stale'] == 2
    assert counts['urgent'] == 2      # the two at 'danger'
    assert counts['lapsed'] == 0
    # The two reasons overlap, so they deliberately exceed the row count.
    assert counts['due'] + counts['stale'] > counts['total']


def test_lapsed_is_counted_apart_from_due():
    """They ask for different things: one wants a payment, the other wants the
    record corrected. Folding them together would put a years-dead date in the
    same sentence as this week's bill."""
    counts = attention_counts([
        {'due_date': days_away(-400)},   # never rolled forward
        {'due_date': days_away(3)},      # genuinely due
    ])
    assert counts['lapsed'] == 1
    assert counts['due'] == 1
    assert counts['total'] == 2


def test_empty_vault_counts_zero():
    assert attention_counts([]) == {
        'total': 0, 'due': 0, 'stale': 0, 'lapsed': 0, 'urgent': 0,
    }


# -- dashboard delta ----------------------------------------------------------

def test_a_range_window_holds_the_month_it_is_measured_from():
    """A year is this month last year to this month, so 13 readings, and a
    month is two. Twelve readings would measure "vs last year" from eleven
    months ago."""
    series = list(range(30))
    assert trailing_window(series, 1) == [28, 29]
    assert len(trailing_window(series, 12)) == 13
    assert trailing_window(series, 12)[0] == 17


def test_a_window_longer_than_the_history_is_all_of_it():
    assert trailing_window([1.0, 2.0, 3.0], 24) == [1.0, 2.0, 3.0]
    assert trailing_window([1.0, 2.0, 3.0], None) == [1.0, 2.0, 3.0]


def test_delta_runs_from_the_first_reading_to_the_last():
    change, fraction, start = change_across([100.0, 120.0, 150.0])
    assert change == 50.0
    assert fraction == pytest.approx(0.5)
    assert start == 0


def test_delta_needs_two_readings():
    assert change_across([]) is None
    assert change_across([100.0]) is None
    assert change_across([None, None]) is None


def test_delta_ignores_months_after_the_last_reading():
    """A series can trail off into None; the comparison is the last reading."""
    change, _, _ = change_across([100.0, 120.0, None, None])
    assert change == 20.0


def test_delta_starts_where_the_chart_does():
    """A category first recorded partway through the window is measured from
    that first reading, the point its chart begins on."""
    change, _, start = change_across([None, None, 400.0, 500.0])
    assert change == 100.0
    assert start == 2


def test_one_month_with_a_gap_before_the_latest_has_nothing_to_compare():
    """Carrying across a hole would invent a month-over-month that never was."""
    assert change_across(trailing_window([100.0, None, 150.0], 1)) is None


def test_delta_percentage_is_undefined_from_zero():
    change, fraction, _ = change_across([0.0, 500.0])
    assert change == 500.0
    assert fraction is None
    assert '%' not in delta_text(change, fraction, 'vs last month')


def test_delta_percentage_uses_magnitude_for_negative_bases():
    """Going from -200 to -100 is a 50% improvement, not -50%."""
    change, fraction, _ = change_across([-200.0, -100.0])
    assert change == 100.0
    assert fraction == pytest.approx(0.5)


def months_from(first: str, count: int) -> list[str]:
    """`count` month keys starting at `first`."""
    year, month = int(first[:4]), int(first[5:7])
    keys = []
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return keys


@pytest.mark.parametrize('months, expected', [
    (1, 'vs last month'),
    (3, 'vs 3 months ago'),
    (6, 'vs 6 months ago'),
    (12, 'vs last year'),
    (24, 'vs 2 years ago'),
])
def test_a_full_window_is_named_by_its_range(months, expected):
    assert _basis_text(months, months_from('2024-01', months + 1), 0) == expected


def test_a_short_history_names_the_month_it_starts_in():
    """Eight months of history under 1Y must not claim a year."""
    keys = months_from('2026-02', 9)
    assert _basis_text(12, keys, 0) == 'since Feb 2026'


def test_a_late_first_reading_names_its_own_month():
    """A full-length window whose category was first recorded partway in."""
    keys = months_from('2025-10', 13)
    assert _basis_text(12, keys, 4) == 'since Feb 2026'


def test_all_history_always_names_its_first_month():
    assert _basis_text(None, months_from('2021-03', 40), 0) == 'since Mar 2021'


def test_day_names_are_spelled_out():
    """A balance history is dated to the day, so its "since" says which one."""
    assert day_name('2025-03-04') == 'Mar 4, 2025'
    assert day_name('2025-12-31T09:15:00') == 'Dec 31, 2025'
    assert day_name('2025-13-01') == '2025-13-01'
    assert day_name('not a date') == 'not a date'


def test_month_names_are_spelled_out():
    assert month_name('2021-03') == 'Mar 2021'
    assert month_name('2026-12') == 'Dec 2026'
    # Anything else is left as it came rather than misnamed.
    assert month_name('2021-13') == '2021-13'


@pytest.mark.parametrize('change, higher_is_better, expected', [
    (100.0,  True,  'DeltaGood'),    # savings up
    (-100.0, True,  'DeltaBad'),     # savings down
    (100.0,  False, 'DeltaBad'),     # a debt climbing is bad news
    (-100.0, False, 'DeltaGood'),    # paying it down is good
    (0.0,    True,  'DeltaFlat'),
    (0.0,    False, 'DeltaFlat'),
])
def test_delta_colour_follows_the_outcome_not_the_sign(change, higher_is_better, expected):
    assert delta_object_name(change, higher_is_better) == expected


def test_delta_text_reads_as_a_sentence():
    assert delta_text(1204.0, 0.023, 'vs last month') == '▲ $1,204.00 (2.3%) vs last month'
    assert delta_text(-50.5, -0.1, 'vs last year') == '▼ $50.50 (10.0%) vs last year'
    assert delta_text(0.0, 0.0, 'since Mar 2021') == '■ $0.00 (0.0%) since Mar 2021'


def test_absurd_percentages_are_dropped_but_the_amount_survives():
    """A month starting near zero yields a correct, useless ratio like 2846%."""
    text = delta_text(183566.45, 28.46, 'vs last month')
    assert '%' not in text
    assert '$183,566.45' in text
    assert text.startswith('▲')
    # Just inside the threshold still shows.
    assert '%' in delta_text(100.0, 9.9, 'vs last month')

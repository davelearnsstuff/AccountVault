# -----------------------------------------------------------------------------
# tests/test_dashboard.py
# monthly_summary — the trends view's agreement with the accounts themselves.
#
# The dashboard reads the same balance history the detail view does. When the
# two disagree there is nothing on screen to say which is right, and the figure
# people act on is whichever they happened to open.
# -----------------------------------------------------------------------------

from datetime import date

from conftest import make_account
from services import accounts as accounts_service
from services import dashboard as dashboard_service


def test_month_bucket_matches_what_the_account_shows(vault):
    """A save stamps a full timestamp on its history entry and a hand-filed one
    carries a bare date. Ranked on the raw value, the bare date sorted first
    whatever order they were added in, so the month closed on the timestamped
    entry and the dashboard reported a figure the account itself disagreed with.
    """
    account = make_account(category='Banking', current_balance=100)
    accounts_service.update_account(account['id'], {**account, 'current_balance': 200})
    accounts_service.add_balance_entry(account['id'], '777', date.today().isoformat())

    summary = dashboard_service.monthly_summary()

    assert accounts_service.get_account(account['id'])['current_balance'] == 777
    assert summary['categories']['Banking'][-1] == 777.0
    assert summary['net_worth'][-1] == 777.0


def test_empty_summary_does_not_leak_between_calls(vault):
    """The empty result used to be a shallow copy of one module-level dict, so
    its lists were shared: a single caller appending to what it was handed
    changed the answer every later caller got."""
    first = dashboard_service.monthly_summary()
    first['months'].append('2020-01')
    first['categories']['Banking'] = []
    first['net_worth'].append(1.0)

    second = dashboard_service.monthly_summary()

    assert second == {'months': [], 'categories': {}, 'net_worth': []}


def test_closed_accounts_stay_out_of_the_trends(vault):
    open_account = make_account(name='Open', category='Banking', current_balance=100)
    make_account(name='Shut', category='Banking', current_balance=900, closed=True)

    summary = dashboard_service.monthly_summary()

    assert summary['categories']['Banking'][-1] == 100.0
    assert accounts_service.get_account(open_account['id'])['current_balance'] == 100


# -- composition ---------------------------------------------------------------
#
# Pure functions over the summary above, so they are tested directly rather than
# through the widgets that draw them — the same stance test_attention.py takes
# with month_over_month.

def summary(**categories) -> dict:
    """A summary carrying one reading per category, which is all composition
    looks at."""
    return {'months': ['2026-08'],
            'categories': {name: [value] for name, value in categories.items()},
            'net_worth': [0.0]}


def test_latest_value_ignores_trailing_gaps():
    assert dashboard_service.latest_value([1.0, 2.0, None, None]) == 2.0
    assert dashboard_service.latest_value([None, None]) is None
    assert dashboard_service.latest_value([]) is None
    assert dashboard_service.latest_value(None) is None


def test_composition_splits_the_two_sides():
    """Liabilities are stored as positive magnitudes and only negated when the
    net worth is summed, so the side an amount belongs to comes from its
    category and never from its sign."""
    parts = dashboard_service.composition(
        summary(Banking=1000.0, Investments=4000.0, **{'Credit Cards': 2000.0})
    )

    assert parts['assets']['total'] == 5000.0
    assert [p['category'] for p in parts['assets']['parts']] == ['Investments', 'Banking']
    assert parts['liabilities']['total'] == 2000.0
    assert [p['category'] for p in parts['liabilities']['parts']] == ['Credit Cards']


def test_shares_are_of_their_own_side_not_of_everything():
    """A mortgage is 100% of the debt and says nothing about the savings. One
    shared denominator would make every share a fraction of a total that has no
    meaning, since the two sides subtract rather than add."""
    parts = dashboard_service.composition(summary(Banking=250.0, Loans=750.0))

    assert parts['assets']['parts'][0]['share'] == 1.0
    assert parts['liabilities']['parts'][0]['share'] == 1.0


def test_parts_come_back_largest_first():
    """What the dashboard orders its category cards by. Alphabetical put a
    current account above a mortgage and left the reader to find the figure
    that mattered."""
    parts = dashboard_service.composition(
        summary(Banking=10.0, Investments=500.0, Zebra=90.0)
    )
    assert [p['category'] for p in parts['assets']['parts']] == \
        ['Investments', 'Zebra', 'Banking']


def test_custom_categories_count_as_assets():
    parts = dashboard_service.composition(summary(Crypto=400.0, Banking=100.0))
    assert [p['category'] for p in parts['assets']['parts']] == ['Crypto', 'Banking']
    assert parts['assets']['parts'][0]['share'] == 0.8


def test_non_positive_balances_take_no_share():
    """A share of a total that one of its own parts is pulling downwards has no
    reading as a length on a bar, so an overdrawn account is left out of it
    rather than drawn as a negative segment."""
    parts = dashboard_service.composition(
        summary(Banking=-500.0, Investments=1000.0, Loans=0.0)
    )

    assert [p['category'] for p in parts['assets']['parts']] == ['Investments']
    assert parts['assets']['total'] == 1000.0
    assert parts['liabilities']['parts'] == []
    assert parts['liabilities']['total'] == 0


def test_shares_of_one_side_sum_to_one():
    parts = dashboard_service.composition(
        summary(Banking=1.0, Investments=2.0, Crypto=97.0)
    )
    assert sum(p['share'] for p in parts['assets']['parts']) == 1.0


def test_an_empty_summary_yields_two_empty_sides():
    for empty in ({'categories': {}}, {}, None):
        parts = dashboard_service.composition(empty)
        assert parts['assets'] == {'total': 0, 'parts': []}
        assert parts['liabilities'] == {'total': 0, 'parts': []}


# Presentation only, and a pure function of the number — the same reason
# test_attention.py checks _delta_text without going near a widget.

def test_a_tiny_share_never_prints_as_zero_percent():
    """A credit-card balance against a mortgage is routinely under one percent.
    '0%' printed beside a real amount reads as a broken figure rather than as
    'very small'."""
    from ui.dashboard_view import _share_text

    assert _share_text(0.0001) == '<1%'    # 0.01%, which would round to nothing
    assert _share_text(0.004) == '<1%'     # 0.4%, still under the rounding floor
    assert _share_text(0.0066) == '1%'     # 0.66% rounds to 1% honestly
    assert _share_text(0.0) == '0%'        # genuinely nothing, not merely small
    assert _share_text(0.94) == '94%'
    assert _share_text(1.0) == '100%'

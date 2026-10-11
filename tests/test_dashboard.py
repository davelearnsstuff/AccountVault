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
from core import database
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
    first['accounts']['Banking'] = []

    second = dashboard_service.monthly_summary()

    assert second == {'months': [], 'categories': {}, 'net_worth': [], 'accounts': {}}


def test_closed_accounts_stay_out_of_the_trends(vault):
    open_account = make_account(name='Open', category='Banking', current_balance=100)
    make_account(name='Shut', category='Banking', current_balance=900, closed=True)

    summary = dashboard_service.monthly_summary()

    assert summary['categories']['Banking'][-1] == 100.0
    assert accounts_service.get_account(open_account['id'])['current_balance'] == 100


def test_property_counts_against_the_loan_on_it(vault):
    """Before Property, a mortgage under Loans was subtracted from net worth
    with nothing to add the house back, so anyone with one read as poorer than
    they were."""
    make_account(name='House', category='Property', current_balance=300000)
    make_account(name='Mortgage', category='Loans', current_balance=200000)

    summary = dashboard_service.monthly_summary()
    sheet = dashboard_service.balance_sheet(summary)

    assert summary['net_worth'][-1] == 100000.0
    assert [row['category'] for row in sheet['assets']['rows']] == ['Property']
    assert sheet['net_worth'] == 100000.0


def test_each_category_lists_the_accounts_its_figure_is_made_of(vault):
    checking = make_account(name='Checking', category='Banking', current_balance=300)
    savings = make_account(name='Savings', category='Banking', current_balance=1200)
    make_account(name='Shut', category='Banking', current_balance=900, closed=True)
    card = make_account(name='Visa', category='Credit Cards', current_balance=250)

    sheet = dashboard_service.balance_sheet(dashboard_service.monthly_summary())

    banking, = sheet['assets']['rows']
    assert banking['accounts'] == [
        {'id': savings['id'], 'name': 'Savings', 'value': 1200.0, 'share': 0.8},
        {'id': checking['id'], 'name': 'Checking', 'value': 300.0, 'share': 0.2},
    ]
    assert sum(account['value'] for account in banking['accounts']) == banking['value']
    assert sheet['liabilities']['rows'][0]['accounts'] == [
        {'id': card['id'], 'name': 'Visa', 'value': 250.0, 'share': 1.0}]


def test_an_overdrawn_account_has_no_share_of_its_category():
    """Shared over the positive figures, as the categories are: over the
    category's net figure, the account beside it would read as 125%."""
    data = summary(Banking=800.0)
    data['accounts'] = {'Banking': [{'id': 1, 'name': 'Checking', 'value': -200.0},
                                    {'id': 2, 'name': 'Savings', 'value': 1000.0}]}

    banking, = dashboard_service.balance_sheet(data)['assets']['rows']

    assert [(account['name'], account['share']) for account in banking['accounts']] == \
        [('Savings', 1.0), ('Checking', None)]


def test_an_account_carries_its_last_figure_forward(vault):
    """An account not updated this month still counts toward its category,
    so it must still be listed under it, at the figure it last held."""
    account = make_account(name='Old', category='Banking', current_balance=100)
    with database.connect() as db:
        db.execute("UPDATE balance_history SET recorded_at = '2020-01-15' WHERE account_id = ?",
                   (account['id'],))

    summary = dashboard_service.monthly_summary()

    assert summary['accounts'] == {
        'Banking': [{'id': account['id'], 'name': 'Old', 'value': 100.0}]}


# -- composition ---------------------------------------------------------------
#
# Pure functions over the summary above, so they are tested directly rather than
# through the widgets that draw them — the same stance test_attention.py takes
# with change_across.

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


# -- an account's dated history -------------------------------------------------

def test_a_month_before_a_longer_months_end_is_that_months_last_day():
    from datetime import date
    months_before = dashboard_service.months_before

    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
    assert months_before(date(2024, 3, 31), 1) == date(2024, 2, 29)
    assert months_before(date(2026, 1, 15), 1) == date(2025, 12, 15)
    assert months_before(date(2026, 10, 10), 24) == date(2024, 10, 10)


def entries(*dates: str) -> list[dict]:
    return [{'recorded_at': stamp, 'balance': str(index)} for index, stamp in enumerate(dates)]


def test_a_dated_window_opens_on_what_the_account_held_when_the_range_began():
    """Entries rarely fall on the range's first day. The one before it is
    what the account held then, so the window starts there, the way the
    dashboard carries a balance forward."""
    from datetime import date
    history = entries('2025-01-20', '2025-08-02', '2025-11-15', '2026-09-30')

    window, full = dashboard_service.dated_window(history, 12, today=date(2026, 10, 10))

    assert [e['recorded_at'] for e in window] == ['2025-08-02', '2025-11-15', '2026-09-30']
    assert full


def test_an_entry_on_the_ranges_first_day_opens_it():
    from datetime import date
    history = entries('2025-10-10', '2026-09-30')

    window, full = dashboard_service.dated_window(history, 12, today=date(2026, 10, 10))

    assert window == history
    assert full


def test_a_history_shorter_than_the_range_is_all_of_it_and_says_so():
    from datetime import date
    history = entries('2026-02-01', '2026-09-30')
    today = date(2026, 10, 10)

    assert dashboard_service.dated_window(history, 12, today=today) == (history, False)
    assert dashboard_service.dated_window(history, None, today=today) == (history, False)


# -- balance sheet -------------------------------------------------------------

def test_the_balance_sheet_adds_up_to_net_worth():
    """The bars it replaced could not draw a negative, so an overdrawn
    category fell out of them and their totals stopped adding up. The table
    lists it and counts it."""
    sheet = dashboard_service.balance_sheet(
        summary(Banking=-500.0, Investments=4000.0, Loans=1500.0))

    assert sheet['assets']['total'] == 3500.0
    assert sheet['liabilities']['total'] == 1500.0
    assert sheet['net_worth'] == 2000.0
    assert [row['category'] for row in sheet['assets']['rows']] == ['Investments', 'Banking']


def test_a_negative_balance_has_no_share():
    sheet = dashboard_service.balance_sheet(summary(Banking=-500.0, Investments=4000.0))
    rows = {row['category']: row for row in sheet['assets']['rows']}

    assert rows['Banking']['share'] is None
    assert rows['Banking']['value'] == -500.0
    assert rows['Investments']['share'] == 1.0


def test_balance_sheet_shares_match_the_category_cards():
    """The cards caption themselves from composition(); the table must never
    give the same category a different share."""
    data = summary(Banking=1000.0, Investments=3000.0, Loans=750.0,
                   **{'Credit Cards': 250.0})
    parts = dashboard_service.composition(data)
    sheet = dashboard_service.balance_sheet(data)

    for side in ('assets', 'liabilities'):
        assert [(row['category'], row['share']) for row in sheet[side]['rows']] == \
            [(part['category'], part['share']) for part in parts[side]['parts']]


def test_balance_sheet_reads_a_pre_rename_slug_as_a_liability():
    sheet = dashboard_service.balance_sheet(summary(creditcards=400.0, Banking=1000.0))

    assert [row['category'] for row in sheet['liabilities']['rows']] == ['creditcards']
    assert sheet['net_worth'] == 600.0


def test_an_empty_balance_sheet_has_no_rows():
    for empty in ({'categories': {}}, {}, None):
        sheet = dashboard_service.balance_sheet(empty)
        assert sheet['assets']['rows'] == []
        assert sheet['liabilities']['rows'] == []
        assert sheet['net_worth'] == 0.0


# Presentation only, and a pure function of the number — the same reason
# test_attention.py checks delta_text without going near a widget.

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


def test_copied_cells_paste_into_a_spreadsheet_as_cells(qt_app):
    """Tabs between columns and a line per row. A gap in the selection is an
    empty cell rather than nothing, or every cell after it would paste one
    column to the left of where it was."""
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QTableWidgetSelectionRange
    from ui.dashboard_view import _copy_cells

    table = QTableWidget(3, 3)
    for row in range(3):
        for column in range(3):
            table.setItem(row, column, QTableWidgetItem(f'{row}{column}'))
    table.setSelectionMode(QTableWidget.ExtendedSelection)
    table.setRangeSelected(QTableWidgetSelectionRange(0, 0, 0, 1), True)
    table.setRangeSelected(QTableWidgetSelectionRange(2, 1, 2, 2), True)

    _copy_cells(table)

    assert QGuiApplication.clipboard().text() == '00\t01\t\n\t21\t22'


# -- investing against income --------------------------------------------------
#
# investing_vs_income reads current figures rather than history, so these build
# accounts and read the answer back, the way the first tests in this file do.

def income(name: str, pay, **fields) -> dict:
    return make_account(category='Income', name=name, current_balance=pay, **fields)


def investment(name: str, amount, period=None, **fields) -> dict:
    if period is not None:
        fields['contribution_period'] = period
    return make_account(category='Investments', name=name,
                        contribution_amount=amount, **fields)


def by_name(rows) -> dict:
    return {row['name']: row for row in rows}


def test_every_period_becomes_a_yearly_figure(vault):
    """52 weeks and 26 fortnights, not 4 and 2 a month: a year holds a whole
    number of every period, which is why the section is yearly at all."""
    income('Job A', 78000)
    income('Job B', 62000)
    investment('Roth', 50, 'Weekly')
    investment('401k', 180, 'Every 2 Weeks')
    investment('IRA', 200, 'Monthly')
    investment('HSA', 3100, 'Yearly')

    result = dashboard_service.investing_vs_income()
    rows = by_name(result['contributions'])

    assert rows['Roth']['yearly'] == 2600
    assert rows['401k']['yearly'] == 4680
    assert rows['IRA']['yearly'] == 2400
    assert rows['HSA']['yearly'] == 3100
    assert result['income_total'] == 140000
    assert result['contribution_total'] == 12780
    assert result['rate'] == 12780 / 140000


def test_each_rows_share_of_income_adds_up_to_the_rate(vault):
    income('Job', 100000)
    investment('401k', 200, 'Every 2 Weeks')
    investment('Brokerage', 100, 'Weekly')

    result = dashboard_service.investing_vs_income()
    rows = by_name(result['contributions'])

    assert rows['401k']['of_income'] == 5200 / 100000
    assert rows['Brokerage']['of_income'] == 5200 / 100000
    assert sum(r['of_income'] for r in result['contributions']) == result['rate']


def test_an_account_with_no_contribution_is_not_listed(vault):
    """No amount means the account is not being paid into. Listing it would
    fill the section with rows of nothing."""
    income('Job', 50000)
    investment('Paid Into', 100, 'Monthly')
    investment('Blank', '', 'Monthly')
    investment('Zero', '0', 'Weekly')
    make_account(category='Investments', name='Never Set')

    result = dashboard_service.investing_vs_income()

    assert [r['name'] for r in result['contributions']] == ['Paid Into']


def test_an_amount_with_no_period_is_listed_but_not_counted(vault):
    """Every vault written before the period existed is in this state. Guessing
    a period would put a figure on the dashboard nobody chose."""
    income('Job', 50000)
    investment('Counted', 100, 'Monthly')
    investment('Old Record', 310)

    result = dashboard_service.investing_vs_income()
    old = by_name(result['contributions'])['Old Record']

    assert old['yearly'] is None
    assert old['period'] is None
    assert old['amount'] == 310
    assert old['of_income'] is None
    assert result['contribution_total'] == 1200


def test_a_period_is_read_exactly_as_the_form_shows_it(vault):
    """The edit form shows a value outside its options as blank. If the
    dashboard read 'biweekly' as Every 2 Weeks, it would count a period the
    form says was never set, and the first save of that form would clear it."""
    income('Job', 50000)
    investment('Imported', 100, 'biweekly')
    investment('Lowercase', 100, 'weekly')
    investment('Not text', 100, ['Weekly'])

    rows = by_name(dashboard_service.investing_vs_income()['contributions'])

    assert rows['Imported']['yearly'] is None
    assert rows['Lowercase']['yearly'] is None
    assert rows['Not text']['yearly'] is None


def test_closed_accounts_are_left_out_on_both_sides(vault):
    """With no history behind the figures, a closed job's pay would otherwise
    count as income for good."""
    income('Current Job', 60000)
    income('Old Job', 40000, closed=True)
    investment('Open', 100, 'Monthly')
    investment('Closed', 500, 'Monthly', closed=True)

    result = dashboard_service.investing_vs_income()

    assert [r['name'] for r in result['income']] == ['Current Job']
    assert [r['name'] for r in result['contributions']] == ['Open']
    assert result['income_total'] == 60000


def test_an_income_with_no_pay_is_listed_but_not_counted(vault):
    income('Paid', 50000)
    make_account(category='Income', name='No Figure')

    result = dashboard_service.investing_vs_income()

    assert by_name(result['income'])['No Figure']['yearly'] is None
    assert result['income_total'] == 50000


def test_no_income_means_no_rate(vault):
    """A share of nothing is either infinite or a lie, so the amount is shown
    without one."""
    investment('401k', 100, 'Weekly')
    make_account(category='Income', name='No Figure')

    result = dashboard_service.investing_vs_income()

    assert result['contribution_total'] == 5200
    assert result['rate'] is None
    assert result['contributions'][0]['of_income'] is None


def test_only_investments_and_income_take_part(vault):
    """A record moved out of Investments keeps contribution_amount in its
    extra_data. It is no longer an investment, so it is not counted as one."""
    income('Job', 50000)
    investment('Real', 100, 'Monthly')
    make_account(category='Banking', name='Moved', contribution_amount=999,
                 contribution_period='Monthly', current_balance=5000)

    result = dashboard_service.investing_vs_income()

    assert [r['name'] for r in result['contributions']] == ['Real']
    assert [r['name'] for r in result['income']] == ['Job']


def test_a_pre_rename_slug_still_counts(vault):
    make_account(category='investments', name='Old Slug',
                 contribution_amount=100, contribution_period='Monthly')

    result = dashboard_service.investing_vs_income()

    assert result['contributions'][0]['category'] == 'Investments'
    assert result['contribution_total'] == 1200


def test_rows_come_back_largest_first_and_uncounted_last(vault):
    income('Small Job', 20000)
    income('Big Job', 80000)
    make_account(category='Income', name='Blank Job')
    investment('Small', 10, 'Monthly')
    investment('No Period', 9999)
    investment('Big', 1000, 'Monthly')

    result = dashboard_service.investing_vs_income()

    assert [r['name'] for r in result['income']] == ['Big Job', 'Small Job', 'Blank Job']
    assert [r['name'] for r in result['contributions']] == ['Big', 'Small', 'No Period']


def test_an_empty_vault_has_nothing_to_list(vault):
    result = dashboard_service.investing_vs_income()

    assert result == {'income': [], 'contributions': [], 'income_total': 0,
                      'contribution_total': 0, 'rate': None}


def test_the_period_field_has_no_default():
    """A new record would otherwise arrive with a period nobody picked, and the
    dashboard would count it as one somebody had."""
    from core.categories import CONTRIBUTIONS_PER_YEAR, get_config

    field = next(f for f in get_config('Investments')['fields']
                 if f['key'] == 'contribution_period')

    assert 'default' not in field
    assert field['options'] == list(CONTRIBUTIONS_PER_YEAR)


def test_a_rate_is_shown_to_a_tenth_of_a_percent():
    """9.3% and 9.7% are different decisions, so whole percents are too
    coarse, and a real contribution must never print as 0.0%."""
    from ui.dashboard_view import _per_period_text, _rate_text

    assert _rate_text(12780 / 140000) == '9.1%'
    assert _rate_text(0.0004) == '<0.1%'
    assert _rate_text(0.0) == '0.0%'
    assert _rate_text(1.25) == '125.0%'
    assert _per_period_text(100, 'Every 2 Weeks') == '$100.00 every 2 weeks'
    assert _per_period_text(310, None) == '$310.00'

# -----------------------------------------------------------------------------
# services/dashboard.py
# Builds the current balance sheet, the monthly net-worth and per-category
# balance trends, and the yearly investing-against-income figures.
# -----------------------------------------------------------------------------

import datetime
import re
from collections import defaultdict

from core.auth import require_session
from core.categories import (
    CONTRIBUTION_PERIOD_KEY, CONTRIBUTIONS_PER_YEAR, normalize_category, parse_money,
)
from core.database import connect
from core.encryption import field_aad, safe_decrypt
from services.accounts import BALANCE_AAD, list_accounts

_MONTH_KEY = re.compile(r'\d{4}-\d{2}')

# Where investing_vs_income reads each side from.
INCOME_CATEGORY = 'Income'
INVESTMENTS_CATEGORY = 'Investments'

# Both sets name categories by their current spelling; every category read here
# goes through normalize_category first, so a legacy slug ('creditcards') lands
# on the right side.
LIABILITY_CATEGORIES = {'Credit Cards', 'Loans'}

# Where a figure falling is the good news, for colouring a change: what is
# owed, and a bill. Utilities is no liability, so it stays out of the set
# above and out of net worth.
LOWER_IS_BETTER = LIABILITY_CATEGORIES | {'Utilities'}

# Kept out of the trends entirely. Income tracks a yearly pay rate, which
# carried forward into net worth would add a salary every month. Utilities is a
# recurring bill rather than a holding, and Secure Notes and Websites have no
# balance field — a stray current_balance left in extra_data must not count as
# an asset.
_EXCLUDED_CATEGORIES = {'Utilities', 'Secure Notes', 'Websites', 'Income'}


def trailing_window(series, months: int | None) -> list:
    """The part of a monthly series a range shows: its last `months` months
    plus the month they are measured from, so a year runs from this month last
    year to this month, and a month is two readings. None means all of it."""
    if months is None:
        return list(series)
    return list(series[-(months + 1):])


def months_before(day: datetime.date, months: int) -> datetime.date:
    """The same day `months` calendar months earlier, or that month's last
    day where it is shorter: a month before March 31 is February 28."""
    total = day.year * 12 + day.month - 1 - months
    year, month = divmod(total, 12)
    following = datetime.date(year + (month == 11), (month + 1) % 12 + 1, 1)
    last_day = (following - datetime.timedelta(days=1)).day
    return datetime.date(year, month + 1, min(day.day, last_day))


def dated_window(entries, months: int | None,
                 today: datetime.date | None = None) -> tuple[list, bool]:
    """The part of one account's dated history a range shows, and whether
    it reaches back the whole range.

    `entries` run oldest first, each with a 'recorded_at'. Entries are filed
    whenever someone records a figure, so the range's start rarely falls on
    one. The window opens on the last entry on or before that day, which is
    what the account held then, the way the dashboard carries a balance
    forward to a month it has no entry for. Every later entry follows.

    With no entry that old, the history is shorter than the range: all of it
    comes back, and False. None (All) is always the whole history and False,
    since it is measured from the first entry, not from a range's start."""
    entries = list(entries)
    if months is None:
        return entries, False
    start = months_before(today or datetime.date.today(), months).isoformat()
    anchor = None
    for index, entry in enumerate(entries):
        if str(entry['recorded_at'])[:10] <= start:
            anchor = index
    if anchor is None:
        return entries, False
    return entries[anchor:], True


def change_across(series) -> tuple[float, float | None, int] | None:
    """(change, fraction, start) from the first reading in a series to its
    latest, where start is the index of the first. The same two points a chart
    of the series begins and ends on, so the figure matches the line. None when
    there is nothing to compare: an empty series or a single reading.

    The fraction is None when the earlier figure was zero, where a percentage is
    either infinite or a lie. It is taken against the magnitude of the earlier
    figure, so a debt going from -200 to -100 reads as a 50% improvement rather
    than -50%."""
    readings = [index for index, value in enumerate(series) if value is not None]
    if len(readings) < 2:
        return None

    start, latest = readings[0], readings[-1]
    earlier = series[start]
    change = series[latest] - earlier
    return change, (change / abs(earlier)) if earlier else None, start


def latest_value(series):
    """The most recent reading in a series, or None if it never had one."""
    for value in reversed(series or []):
        if value is not None:
            return value
    return None


def side_totals(summary) -> tuple[float, float]:
    """(assets, liabilities) from each category's latest reading.

    Unlike composition(), this counts negative categories too, so the pair
    reconciles with net worth."""
    assets = liabilities = 0.0
    for category, series in ((summary or {}).get('categories') or {}).items():
        latest = latest_value(series)
        if latest is None:
            continue
        if normalize_category(category) in LIABILITY_CATEGORIES:
            liabilities += latest
        else:
            assets += latest
    return assets, liabilities


def composition(summary) -> dict:
    """How the latest assets and liabilities totals divide between categories.

    Returns {side: {'total': float, 'parts': [{category, value, share}]}} with
    each side's parts largest first.

    The two sides are kept apart: they are opposite signs, so one bar mixing
    them would measure nothing. Only positive latest values take part — a
    negative one has no honest reading as a length on a bar — so 'total' is
    the total *of the bar* and can differ from net worth by exactly the
    categories left out."""
    categories = (summary or {}).get('categories') or {}
    sides: dict[str, list] = {'assets': [], 'liabilities': []}

    for category, series in categories.items():
        value = latest_value(series)
        if value is None or value <= 0:
            continue
        # normalize_category for the same reason monthly_summary uses it: a
        # legacy 'creditcards' key would land on the assets side of the bar.
        side = ('liabilities' if normalize_category(category) in LIABILITY_CATEGORIES
                else 'assets')
        sides[side].append((category, value))

    result = {}
    for side, parts in sides.items():
        parts.sort(key=lambda part: part[1], reverse=True)
        total = sum(value for _, value in parts)
        result[side] = {
            'total': total,
            'parts': [
                {'category': category, 'value': value,
                 'share': (value / total) if total else 0.0}
                for category, value in parts
            ],
        }
    return result


def balance_sheet(summary) -> dict:
    """Every category's latest reading by side, each side's total, and the net
    worth the two make.

    Returns {'assets': {'rows', 'total'}, 'liabilities': {'rows', 'total'},
    'net_worth'}, each row {category, value, share, accounts} and each side's
    rows largest first. accounts is the open accounts the category's figure is
    the sum of, each {id, name, value, share}, largest first, where share is
    the account's part of its category.

    Unlike composition(), a negative reading is listed and counted, so the two
    totals always come to the net worth beneath them. Its share is None: a
    negative has no honest share of a side. The shares that are given are
    composition()'s, so the table and the category cards never disagree."""
    categories = (summary or {}).get('categories') or {}
    accounts = (summary or {}).get('accounts') or {}
    shares = {part['category']: part['share']
              for side in composition(summary).values() for part in side['parts']}

    sides: dict[str, list] = {'assets': [], 'liabilities': []}
    for category, series in categories.items():
        value = latest_value(series)
        if value is None:
            continue
        side = ('liabilities' if normalize_category(category) in LIABILITY_CATEGORIES
                else 'assets')
        # Ties break on the name, as the rows below do.
        members = sorted(({**account, 'share': None} for account in accounts.get(category) or []),
                         key=lambda account: (-account['value'], str(account['name']).lower()))
        # Shared out the way composition() shares out a side: over the
        # positive figures only, so an overdrawn account cannot push the
        # others past 100%, and has no share itself.
        positive = sum(account['value'] for account in members if account['value'] > 0)
        for account in members:
            if account['value'] > 0:
                account['share'] = account['value'] / positive
        sides[side].append({'category': category, 'value': value,
                            'share': shares.get(category), 'accounts': members})

    # Ties break on the name, so the table holds still between renders.
    for rows in sides.values():
        rows.sort(key=lambda row: (-row['value'], row['category']))

    assets, liabilities = side_totals(summary)
    return {
        'assets': {'rows': sides['assets'], 'total': assets},
        'liabilities': {'rows': sides['liabilities'], 'total': liabilities},
        'net_worth': assets - liabilities,
    }


def _empty() -> dict:
    """A fresh empty summary. Built per call so no two callers share its lists."""
    return {'months': [], 'categories': {}, 'net_worth': [], 'accounts': {}}


def monthly_summary() -> dict:
    """Returns {months, categories, net_worth, accounts} where each series is
    aligned to months and carries the last known balance forward through gaps.
    accounts maps each category to its accounts' latest figures, each
    {id, name, value}: the parts its latest figure is the sum of."""
    require_session()

    with connect() as db:
        account_rows = db.execute(
            "SELECT id, category, name, closed FROM accounts").fetchall()

        # Exclude closed accounts and categories that don't belong in the trends view
        accounts = {}
        names = {}
        for row in account_rows:
            category = normalize_category(row['category'])
            if not (row['closed'] or 0) and category not in _EXCLUDED_CATEGORIES:
                accounts[row['id']] = category
                names[row['id']] = row['name']
        if not accounts:
            return _empty()

        # Oldest first, so the last entry written into each month is the one
        # that month closed on. Ordered on the date alone, as services/accounts
        # orders history, so this view settles on the same figure the account
        # shows.
        placeholders = ','.join('?' * len(accounts))
        entries = db.execute(
            f"SELECT account_id, balance, recorded_at FROM balance_history "
            f"WHERE account_id IN ({placeholders}) "
            f"ORDER BY substr(recorded_at, 1, 10) ASC, id ASC",
            list(accounts.keys()),
        ).fetchall()

    if not entries:
        return _empty()

    # Latest balance per (account, month). safe_decrypt re-raises when locked,
    # so a lock surfaces rather than rendering an empty dashboard; an entry that
    # will not open comes back as the placeholder, which is not a number and is
    # skipped.
    account_month_balance: dict[int, dict[str, float]] = defaultdict(dict)
    usable_months = []
    for entry in entries:
        try:
            balance = float(safe_decrypt(entry['balance'], BALANCE_AAD))
        except (TypeError, ValueError):
            continue
        month_key = str(entry['recorded_at'] or '')[:7]
        if not _MONTH_KEY.fullmatch(month_key):
            # A row written before recorded_at was validated can hold anything,
            # and the month range below parses this with a bare int().
            continue
        usable_months.append(month_key)
        account_month_balance[entry['account_id']][month_key] = balance

    if not usable_months:
        return _empty()

    # Month range: earliest *usable* month → current month, so a broken row
    # cannot open the chart on a run of empty months.
    today = datetime.date.today()
    months = []
    first_month = min(usable_months)
    year, month = int(first_month[:4]), int(first_month[5:7])
    while (year, month) <= (today.year, today.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month, year = 1, year + 1

    # Carry-forward: fill each month from the last known value
    account_series = {}
    for account_id, monthly_map in account_month_balance.items():
        last = None
        series = []
        for month_key in months:
            if month_key in monthly_map:
                last = monthly_map[month_key]
            series.append(last)
        account_series[account_id] = series

    # Sum by category
    category_series: dict[str, list] = {}
    for account_id, series in account_series.items():
        category = accounts[account_id]
        target = category_series.setdefault(category, [None] * len(months))
        for i, value in enumerate(series):
            if value is not None:
                target[i] = (target[i] or 0.0) + value

    # Net worth: assets − liabilities
    net_worth = []
    for i in range(len(months)):
        total = None
        for category, series in category_series.items():
            value = series[i]
            if value is not None:
                total = (total or 0.0) + (-value if category in LIABILITY_CATEGORIES else value)
        net_worth.append(total)

    # Each account's figure this month, under its category. Carried forward,
    # so the last month holds a figure for every account with one to give, and
    # a category's parts add up to its latest figure. Names are decrypted here
    # only, for the accounts listed: one that will not open shows the
    # placeholder, as it does everywhere else.
    category_accounts: dict[str, list] = {}
    for account_id, series in account_series.items():
        if series[-1] is None:
            continue
        name = names[account_id]
        category_accounts.setdefault(accounts[account_id], []).append({
            'id': account_id,
            'name': safe_decrypt(name, field_aad('accounts', 'name')) if name else '',
            'value': series[-1],
        })

    return {'months': months, 'categories': category_series, 'net_worth': net_worth,
            'accounts': category_accounts}


# -----------------------------------------------------------------------------
# Investing against income
# -----------------------------------------------------------------------------

def investing_vs_income() -> dict:
    """What the household puts into investments each year against what it
    earns, account by account.

    Returns {income, contributions, income_total, contribution_total, rate}.
    Each row is {id, name, category, yearly}; a contribution row also carries
    the amount and period it was worked out from, and 'of_income', its share of
    income_total. rate is contribution_total over income_total, None with no
    income to divide by.

    A row whose yearly figure is None is listed but not counted: an Income
    source with no pay entered, or a contribution with no period. Neither is
    guessed at.

    Yearly rather than monthly: Income stores annual pay, and a year holds a
    whole number of every period, where a month holds two or three fortnightly
    paydays.

    Current figures only. Closed accounts are left out on both sides, or a
    closed job's pay would count as income for good. An Investments account
    with no contribution amount, or zero, is not being paid into, and is left
    off the list rather than shown as a row of nothing."""
    income, contributions = [], []
    for account in list_accounts():
        if account.get('closed'):
            continue
        category = normalize_category(account.get('category') or '')
        row = {'id': account['id'], 'name': account.get('name') or '',
               'category': category}

        if category == INCOME_CATEGORY:
            row['yearly'] = parse_money(account.get('current_balance'))
            income.append(row)

        elif category == INVESTMENTS_CATEGORY:
            amount = parse_money(account.get('contribution_amount'))
            if not amount:
                continue
            period = account.get(CONTRIBUTION_PERIOD_KEY)
            # isinstance first: extra_data is JSON, and an imported list or
            # object here would not even hash.
            per_year = CONTRIBUTIONS_PER_YEAR.get(period) if isinstance(period, str) else None
            row.update(amount=amount, period=period if per_year else None,
                       yearly=amount * per_year if per_year else None)
            contributions.append(row)

    income_total = sum(row['yearly'] for row in income if row['yearly'] is not None)
    contribution_total = sum(row['yearly'] for row in contributions
                             if row['yearly'] is not None)

    # Only positive income divides: a share of nothing, or of a negative, is
    # either infinite or a lie.
    divisor = income_total if income_total > 0 else None
    for row in contributions:
        row['of_income'] = (row['yearly'] / divisor
                            if divisor and row['yearly'] is not None else None)

    _largest_first(income)
    _largest_first(contributions)
    return {
        'income': income,
        'contributions': contributions,
        'income_total': income_total,
        'contribution_total': contribution_total,
        'rate': contribution_total / divisor if divisor else None,
    }


def _largest_first(rows: list):
    """Largest yearly figure first, the way the category cards are ordered, and
    the uncounted rows last. Ties break on the name, so the list holds still
    between renders."""
    rows.sort(key=lambda row: (row['yearly'] is None, -(row['yearly'] or 0.0),
                               str(row['name']).lower()))

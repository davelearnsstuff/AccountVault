# -----------------------------------------------------------------------------
# services/dashboard.py
# Builds the monthly net-worth and per-category balance trends.
# -----------------------------------------------------------------------------

import datetime
import re
from collections import defaultdict

from auth import require_session
from categories import normalize_category
from database import connect
from encryption import safe_decrypt
from services.accounts import BALANCE_AAD

_MONTH_KEY = re.compile(r'\d{4}-\d{2}')

LIABILITY_CATEGORIES = {'Credit Cards', 'Loans'}

# Kept out of the trends entirely. Income tracks a yearly pay rate rather than a
# balance, so carrying it forward into net worth would add a salary to the total
# every month and never take it back out. Utilities is a recurring bill rather
# than a holding, and Secure Notes and Websites have no balance field at all —
# Websites was the one missing from this set, so an account carrying a stray
# current_balance in extra_data (moved in from a category that had the field, or
# arrived that way in an imported .vault) was silently counted as an asset.
_EXCLUDED_CATEGORIES = {'Utilities', 'Secure Notes', 'Websites', 'Income'}

# Both sets above name categories by their current spelling. A vault written
# before the rename stores slug keys ('creditcards', 'securenotes'), which
# matched neither set: a legacy credit card was added to net worth as an asset
# rather than subtracted as a liability, and a legacy secure note was not
# excluded at all. Every category read here goes through normalize_category
# first, which is what CATEGORY_ALIASES exists for.


def month_over_month(series) -> tuple[float, float | None] | None:
    """(change, fraction) between the latest figure in a series and the one
    before it, or None when there is nothing to compare against.

    Lives here rather than in the view because it is a fact about the series
    monthly_summary() produces, not about how the dashboard draws it. The view
    only decides how to word and colour the answer.

    None comes back in three cases, all of them "no honest comparison exists":
    an empty series, a single reading, and a gap in the month immediately before
    the latest one — carrying across that hole would invent a month-over-month
    change that never happened.

    The fraction is None when the earlier figure was zero, where a percentage is
    either infinite or a lie. It is taken against the magnitude of the earlier
    figure, so a debt going from -200 to -100 reads as a 50% improvement rather
    than -50%."""
    latest = None
    for index in range(len(series) - 1, -1, -1):
        if series[index] is not None:
            latest = index
            break
    if latest is None or latest == 0:
        return None

    previous = series[latest - 1]
    if previous is None:
        return None

    change = series[latest] - previous
    return change, (change / abs(previous)) if previous else None


def latest_value(series):
    """The most recent reading in a series, or None if it never had one.

    Lives here rather than in the view for the same reason month_over_month
    does: which reading is current is a fact about the series, and the
    composition figures below and the dashboard cards have to agree on it."""
    for value in reversed(series or []):
        if value is not None:
            return value
    return None


def side_totals(summary) -> tuple[float, float]:
    """(assets, liabilities) from each category's latest reading.

    Here rather than in the view for the same reason latest_value and
    month_over_month are: which side a category falls on is this module's rule,
    and the dashboard was carrying its own copy of the loop — including its own
    LIABILITY_CATEGORIES test, which would have to be kept in step with the one
    net_worth uses two functions down.

    Unlike composition(), this counts negative categories too, so the pair
    reconciles with net_worth. That is exactly why it cannot be read off
    composition()'s totals, which are the totals of a bar that has no way to
    draw a negative length."""
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
    each side's parts largest first. The ordering is what lets the dashboard
    rank its category cards by weight rather than alphabetically, and the shares
    are what let a card state how much of the total it actually is — a sparkline
    is the same size whether it is drawing a current account or a mortgage.

    Assets and liabilities are kept apart rather than stacked into one bar. They
    are opposite signs, so a single bar mixing them would be measuring nothing:
    a 400k mortgage beside 400k of investments would read as an even split of a
    net worth that is actually zero.

    Only positive latest values take part. A category can go negative — an
    overdrawn current account, an overpaid card — and a share of a total that
    one of its own parts is pulling downwards has no honest reading as a length
    on a bar. Such a category is left out of the bar rather than drawn as a
    negative segment; its amount still has its own card in the grid below.

    'total' is therefore the total *of the bar*, which is what the label above
    it describes and what the shares are taken against. It is deliberately not
    the same figure net_worth uses: whenever any category's latest reading is
    negative, net worth counts it and this does not. The two disagreeing is the
    correct behaviour for a bar that cannot draw a negative length, not a
    rounding difference — anything comparing them will find a gap exactly the
    size of the excluded categories."""
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


def _empty() -> dict:
    """A fresh empty summary. Built per call rather than copied from a module
    constant: dict() copies only the outer mapping, so every caller handed the
    "empty" result shared one set of lists, and the first one to append to what
    it was given changed the answer for the rest of the session."""
    return {'months': [], 'categories': {}, 'net_worth': []}


def monthly_summary() -> dict:
    """Returns {months, categories, net_worth} where each series is aligned to months
    and carries the last known balance forward through gaps."""
    require_session()

    with connect() as db:
        account_rows = db.execute("SELECT id, category, closed FROM accounts").fetchall()

        # Exclude closed accounts and categories that don't belong in the trends view
        accounts = {}
        for row in account_rows:
            category = normalize_category(row['category'])
            if not (row['closed'] or 0) and category not in _EXCLUDED_CATEGORIES:
                accounts[row['id']] = category
        if not accounts:
            return _empty()

        # Oldest first, so the last entry written into each month below is the
        # one that month closed on. Ordered on the date alone, exactly as
        # services/accounts.py orders history: a save stamps a full timestamp
        # and a hand-filed entry a bare date, so comparing the raw values ranks
        # same-day entries by their format and this view settles on a different
        # figure than the account itself shows. Same date falls back to the id,
        # which is the order they were added in.
        placeholders = ','.join('?' * len(accounts))
        entries = db.execute(
            f"SELECT account_id, balance, recorded_at FROM balance_history "
            f"WHERE account_id IN ({placeholders}) "
            f"ORDER BY substr(recorded_at, 1, 10) ASC, id ASC",
            list(accounts.keys()),
        ).fetchall()

    if not entries:
        return _empty()

    # Latest balance per (account, month).
    #
    # safe_decrypt rather than decrypt plus a float() fallback on the raw
    # column: it already returns legacy plaintext as-is and re-raises
    # VaultLockedError, so the lock still surfaces instead of silently
    # rendering an empty dashboard. Ciphertext this key cannot open comes back
    # as the UNREADABLE placeholder, which is not a number and is skipped —
    # the same way it is left out of a history CSV export.
    account_month_balance: dict[int, dict[str, float]] = defaultdict(dict)
    usable_months = []
    for entry in entries:
        try:
            balance = float(safe_decrypt(entry['balance'], BALANCE_AAD))
        except (TypeError, ValueError):
            continue
        month_key = str(entry['recorded_at'] or '')[:7]
        if not _MONTH_KEY.fullmatch(month_key):
            # Every write path validates recorded_at, but a row predating that
            # can hold anything, and the month range below parses this with a
            # bare int() — one bad row used to take the whole dashboard down.
            continue
        usable_months.append(month_key)
        account_month_balance[entry['account_id']][month_key] = balance

    if not usable_months:
        return _empty()

    # Build month range: earliest *usable* month → current month. Taken from the
    # entries that survived decryption rather than from all of them, so a
    # broken row does not open the chart on a run of empty months no reading
    # backs.
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

    return {'months': months, 'categories': category_series, 'net_worth': net_worth}

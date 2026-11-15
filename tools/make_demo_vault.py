# -----------------------------------------------------------------------------
# tools/make_demo_vault.py
# Builds a throwaway vault full of demonstration data.
#
#   python tools/make_demo_vault.py [path]
#
# The vault chooser's Create Demo Vault… button runs the same build_vault,
# after asking for the household's figures (see Household). The script uses
# the defaults.
#
# Everything in it is fictional and deliberately recognisable as such: it is a
# Springfield household, one or two incomes, a mortgage most of the way
# through its life, two car loans, credit-card balances coming down, retirement
# savings going up, and one crypto experiment that went the way they do. The
# figures are the point — they are sized so the dashboard has something real to
# draw rather than a flat line, and so every view in the app has something in it.
#
# What it exercises, on purpose:
#
#   * all nine built-in categories, with at least six accounts in each but
#     Property, which holds the house and the two cars the loans are against,
#     and Income, which holds a job or two and the closed ones before them
#   * 31 months of balance history per money account, so the net-worth trend,
#     the per-category sparklines and every dashboard range, 2Y included, have
#     a run of readings behind them rather than two points
#   * the four due-date cycles, including bills due this week and one one-off
#     that lapsed months ago
#   * accounts that have gone stale at four different rates, so Needs Attention
#     is populated for both of its reasons
#   * pinned accounts, closed accounts, and a category the app does not ship
#   * every contribution period, one amount left without a period, and two
#     accounts nobody pays into, so the dashboard's investing section shows
#     every kind of row it has
#   * every Tax Treatment: Traditional, Roth, Both with its two amounts, and one
#     left blank for the planner to assume, plus types it leaves out
#   * the children's accounts ticked Exclude from Retirement, and one account
#     that is somebody else's money ticked out of the dashboard as well
#   * a saved retirement plan, so the Retirement tab opens on this household
#     rather than the sample one
#
# It is a tool, not a fixture: the tests build their own vaults and never touch
# this. Re-running it against an existing path refuses rather than overwrites,
# because the file it would replace is a vault and this script has no way to
# know it is the demo one.
#
# The password is printed at the end rather than hidden. That is the whole
# point of a demo vault, and it is also why nothing in here is a real secret:
# every password below is visibly a joke.
# -----------------------------------------------------------------------------

import datetime
import math
import random
import sys
from dataclasses import astuple, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import auth                                       # noqa: E402
from core import database                                   # noqa: E402
from core import preferences                                # noqa: E402
from core import retirement                                 # noqa: E402
from core.categories import (                               # noqa: E402
    BOTH, CHECKED, CONTRIBUTIONS_PER_YEAR, SPLIT_FIELDS, TRADITIONAL, trim_number,
)
from services import accounts as accounts_service  # noqa: E402
from services import retirement as retirement_service  # noqa: E402

PASSWORD = 'EatMyShorts123!@#'
# Relative, so a run with no path writes to the current directory. The vault
# chooser's button only takes the name from it, as the suggestion in its dialog.
DEFAULT_PATH = Path('springfield.vault')

# The history window. 31 monthly readings ending in the current month, which is
# long enough for the trend chart to have a shape, more than the 25 the 2Y range
# shows so that All still shows more, and short enough that the x-axis labels
# stay readable.
MONTHS = 31

# Fixed so two runs produce the same vault. The wobble this drives is cosmetic —
# it stops every series being a straight line — and a demo that changes shape
# each time it is built is one you cannot point at twice.
SEED = 1989

TODAY = datetime.date.today()


# -----------------------------------------------------------------------------
# The household's figures
#
# They size the money accounts: the jobs in Income, every Investments account,
# the household's own bank accounts, and the saved retirement plan. Everything
# else in Springfield is the same whatever they are.
#
# Each figure lands near what was asked rather than on it. 145,000 invested
# comes out as accounts holding 148,316.27 between them, because no real
# household's statements add up to a round number. The randomness is seeded
# from the figures, so the same answers build the same vault.
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Household:
    """The defaults are about where the Springfield demo always was."""
    two_incomes: bool = False   # False: Homer works and Marge does not
    income: int = 58000         # yearly pay, both jobs together
    age: int = 39               # Homer's
    partner_age: int = 36       # Marge's
    invested: int = 145000      # every Investments account together
    saving_pct: int = 15        # every contribution together, as a % of pay

    def check(self):
        """ValueError for a figure outside LIMITS."""
        if not isinstance(self.two_incomes, bool):
            raise ValueError('two_incomes must be True or False.')
        for key, (low, high) in LIMITS.items():
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not low <= value <= high:
                raise ValueError(f'{key} must be from {low:,} to {high:,}.')


# What each figure may be. The ages stop at 80, the planner's latest retirement
# age: the household is still working.
LIMITS = {
    'income':      (10_000, 2_000_000),
    'age':         (18, 80),
    'partner_age': (18, 80),
    'invested':    (0, 20_000_000),
    'saving_pct':  (0, 50),
}

# The pay the bank balances below were written for. A household earning twice
# as much keeps about twice as much in the bank.
_BASE_PAY = Household().income

# Each Investments account's share of the total invested and of the yearly
# contributions, as (single income, two incomes). Only their sizes relative to
# each other matter. An account missing from the contributions is one nobody
# pays into; a zero is one the household does not have.
_BALANCE_SHARES = {
    '401k':      (0.46, 0.34),
    'roth_ira':  (0.13, 0.11),
    '403b':      (0,    0.15),
    'hsa':       (0.04, 0.04),
    '529':       (0.06, 0.06),
    'ugma':      (0.015, 0.015),
    'brokerage': (0.07, 0.07),
    'espp':      (0.025, 0.025),
    'crypto':    (0.005, 0.005),
    'index_ira': (0.11, 0.09),
    'rollover':  (0.07, 0.06),
}
_CONTRIBUTION_SHARES = {
    '401k':      (0.55, 0.36),
    'roth_ira':  (0.14, 0.12),
    '403b':      (0,    0.24),
    'hsa':       (0.07, 0.07),
    '529':       (0.06, 0.06),
    'brokerage': (0.06, 0.06),
    'index_ira': (0.12, 0.09),
}

# How often each contribution goes in, and what one payment is rounded to:
# paycheck deductions to the cent, the ones a person types to something round.
_PAYMENTS = {
    '401k':      ('Every 2 Weeks', 0.01),
    'roth_ira':  ('Monthly', 25),
    '403b':      ('Every 2 Weeks', 0.01),
    'hsa':       ('Every 2 Weeks', 0.01),
    '529':       ('Monthly', 10),
    'brokerage': ('Weekly', 5),
    'index_ira': ('Yearly', 100),
}

# Marge's accounts, whose limits go by her age. The rest go by Homer's.
_MARGES = {'roth_ira', '403b'}

# Approximate 2026 yearly limits on what one person pays in, as (limit,
# catch-up, age the catch-up starts). They need replacing each year, as the
# planner's tax tables do. Whatever a limit turns away goes to the brokerage
# account instead, as it would in life.
_LIMITS = {
    '401k':      (24500, 8000, 50),
    '403b':      (24500, 8000, 50),
    'roth_ira':  (7500, 1100, 50),
    'index_ira': (7500, 1100, 50),
    'hsa':       (8750, 1000, 55),
}

# How an investment history is worked back from today's balance: this return
# a year over the months it covers, with the contributions going in on the way.
_HISTORY_RETURN = 0.06

# Approximate 2026 Social Security figures: the most pay a year that counts,
# and the benefit formula's two bend points, monthly. They need replacing
# each year too.
SS_WAGE_BASE = 184_500
SS_BEND_POINTS = (1286, 7749)

# Payroll tax: Social Security up to the wage base, Medicare on all of it. And
# a flat stand-in for state income tax, Springfield's state never being named.
_SS_TAX, _MEDICARE_TAX = 0.062, 0.0145
_STATE_TAX = 0.05


def _round_to(amount: float, step: float, down: bool = False):
    """amount to the nearest step, or the step below it."""
    steps = math.floor(amount / step) if down else round(amount / step)
    return trim_number(round(steps * step, 2))


def _cents(amount: float) -> str:
    """An amount as it is stored: to the cent, with no trailing zeros."""
    return str(trim_number(round(amount, 2)))


def _spread(total: float, shares: dict, column: int, rng, drift: float) -> dict:
    """total divided by the shares in one column, each part moved by up to
    ±12%, and the lot then scaled to add up to within ±drift of the total:
    near it, but not on it. Left to the parts' own movement, the biggest one
    would carry the sum 5% or more off by itself."""
    weights = {key: pair[column] * rng.uniform(0.88, 1.12)
               for key, pair in shares.items() if pair[column]}
    scale = total * rng.uniform(1 - drift, 1 + drift) / sum(weights.values())
    return {key: weight * scale for key, weight in weights.items()}


def _split(total: float, share: float) -> tuple[str, str]:
    """total as Traditional and Roth parts that add up to it to the cent."""
    cents = round(total * 100)
    traditional = round(cents * share)
    return _cents(traditional / 100), _cents((cents - traditional) / 100)


def figures(household: Household, rng) -> dict:
    """Every generated amount: each job's pay, each Investments account's
    balance and payment, the 401(k)'s Traditional and Roth parts, and how far
    the bank balances are scaled.

    The payments add up to about saving_pct of pay, and the balances to about
    the total invested. A payment a limit holds back is rounded down, so it
    never lands a few cents over."""
    column = 1 if household.two_incomes else 0

    total = household.income * rng.uniform(0.97, 1.03)
    if household.two_incomes:
        homer = _round_to(total * rng.uniform(0.55, 0.65), 10)
        pays = {'homer': homer, 'marge': _round_to(total - homer, 10)}
    else:
        pays = {'homer': _round_to(total, 10)}
    # Rounded to tens, pay lands on the figure asked about one time in 350.
    # Near it is the point, so that one time it moves.
    if sum(pays.values()) == household.income:
        pays['homer'] += 10
    pay = sum(pays.values())

    balances = {key: round(value, 2) for key, value in
                _spread(household.invested, _BALANCE_SHARES, column, rng, 0.03).items()}
    yearly = _spread(pay * household.saving_pct / 100, _CONTRIBUTION_SHARES, column, rng, 0.03)
    capped = set()
    for key, (limit, catch_up, from_age) in _LIMITS.items():
        if key not in yearly:
            continue
        age = household.partner_age if key in _MARGES else household.age
        cap = limit + (catch_up if age >= from_age else 0)
        if yearly[key] > cap:
            yearly['brokerage'] += yearly[key] - cap
            yearly[key] = cap
            capped.add(key)

    payments = {}
    for key, value in yearly.items():
        period, step = _PAYMENTS[key]
        payments[key] = _round_to(value / CONTRIBUTIONS_PER_YEAR[period], step,
                                  down=key in capped)

    return {
        'pays': pays,
        'balances': balances,
        'payments': payments,
        # Most of a 401(k) went in before the Roth option arrived; a younger
        # share of what goes in now is Roth.
        'balance_parts': _split(balances['401k'], rng.uniform(0.78, 0.88)),
        'payment_parts': _split(payments['401k'], rng.uniform(0.6, 0.75)),
        'scale': pay / _BASE_PAY,
    }


def yearly_payments(money: dict) -> dict:
    """Each account's payment as a yearly figure, as the dashboard counts it."""
    return {key: amount * CONTRIBUTIONS_PER_YEAR[_PAYMENTS[key][0]]
            for key, amount in money['payments'].items()}


def _history_start(balance: float, yearly: float) -> float:
    """Where an Investments account stood when its history begins: today's
    balance less what was paid in since and what the market added to it.

    Never below a fifth of today's, so an account that is mostly new money
    still draws a line rather than starting from nothing."""
    years = (MONTHS - 1) / 12
    growth = (1 + _HISTORY_RETURN) ** years
    # Paid in all the way through, so on average each payment grew for half.
    paid_in = yearly * years * (1 + (growth - 1) / 2)
    return max((balance - paid_in) / growth, balance * 0.2)


def social_security(pay: float) -> int:
    """A rough monthly benefit at 67 for someone paid `pay` a year.

    The real formula averages the best 35 years of pay, indexed for wage
    growth. Here today's pay stands in for every one of them, which is as
    close as a demo needs."""
    monthly = min(pay, SS_WAGE_BASE) / 12
    low, high = SS_BEND_POINTS
    return math.floor(0.9 * min(monthly, low)
                      + 0.32 * max(0.0, min(monthly, high) - low)
                      + 0.15 * max(0.0, monthly - high))


def benefits(pays: dict) -> tuple[int, int]:
    """Homer's and Marge's monthly Social Security at 67: each one's own
    rough benefit, or half the other's where that is more, as a spouse's
    benefit pays."""
    homer = social_security(pays['homer'])
    marge = social_security(pays.get('marge', 0))
    return max(homer, marge // 2), max(marge, homer // 2)


def take_home(pays, pretax: float) -> float:
    """A year's pay after tax, before any of it is saved. Married filing
    jointly, with pre-tax contributions kept out of income tax but not payroll
    tax, as a 401(k)'s are."""
    gross = sum(pays)
    table = retirement.TAX_TABLES['joint']
    taxed = max(0.0, gross - pretax)
    federal = retirement.ordinary_tax(max(0.0, taxed - table.standard), table)
    payroll = sum(min(pay, SS_WAGE_BASE) * _SS_TAX + pay * _MEDICARE_TAX for pay in pays)
    return gross - federal - payroll - taxed * _STATE_TAX


def plan_details(household: Household, rng, money: dict) -> dict:
    """The retirement plan's details for the household.

    Spending is take-home pay less everything saved: what the household lives
    on now, carried into retirement. Social Security is not here: the plan
    reads it from the Income records (see benefits). Birth months are picked
    at random, and the years follow from the ages."""
    pays = money['pays']
    yearly = yearly_payments(money)
    per_year = CONTRIBUTIONS_PER_YEAR[_PAYMENTS['401k'][0]]
    pretax = (float(money['payment_parts'][0]) * per_year
              + yearly.get('403b', 0) + yearly.get('hsa', 0))
    spending = take_home(pays.values(), pretax) - sum(yearly.values())
    # A household saving half its pay on a big salary can come out with
    # almost nothing left; nobody lives on that.
    spending = max(spending, 0.1 * sum(pays.values()))

    details = {
        'partner': True,
        'spending': _round_to(spending, 100),
    }
    for prefix, age in (('', household.age), ('partner_', household.partner_age)):
        month = rng.randint(1, 12)
        details[f'{prefix}birth_month'] = month
        details[f'{prefix}birth_year'] = TODAY.year - age - (1 if TODAY.month < month else 0)
    return details


# -----------------------------------------------------------------------------
# Series shapes
#
# Each money account declares where it started, where it ended and how it got
# there. The shape is what makes the dashboard worth looking at: a column of
# straight lines would draw fine and demonstrate nothing.
# -----------------------------------------------------------------------------

def _month_starts(count: int) -> list[datetime.date]:
    """The first of each of the last `count` months, oldest first."""
    year, month = TODAY.year, TODAY.month
    months = []
    for _ in range(count):
        months.append(datetime.date(year, month, 1))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return list(reversed(months))


def _days_in_month(year: int, month: int) -> int:
    following = datetime.date(year + (month == 12), (month % 12) + 1, 1)
    return (following - datetime.timedelta(days=1)).day


def series(rng, start: float, end: float, shape: str = 'linear',
           wobble: float = 0.0, day: int = 12, stop_before_days: int = 0,
           finish: float | None = None):
    """(amount, YYYY-MM-DD) readings, oldest first.

    `wobble` is a fraction of the value, so a checking account swings by
    hundreds and a mortgage by tens — the same number would make one look
    frozen and the other look like a heart monitor.

    `stop_before_days` ends the series that many days before today, which is
    how an account is made to look stale without pretending anything else about
    it changed.

    `finish` is what the newest reading says, exactly, whatever the shape and
    wobble made of it. The Investments accounts end on theirs, so they add up
    to the total the household asked for.
    """
    cutoff = TODAY - datetime.timedelta(days=stop_before_days)
    readings = []
    starts = _month_starts(MONTHS)
    for index, first in enumerate(starts):
        fraction = index / (len(starts) - 1)

        if shape == 'linear':
            value = start + (end - start) * fraction
        elif shape == 'saw':
            # A paycheck arrives, the month spends it down. The trend underneath
            # still moves, so the account is not merely oscillating.
            trend = start + (end - start) * fraction
            value = trend * (1.0 + 0.42 * rng.uniform(-1, 1))
        elif shape == 'vol':
            # Markets. An eased trend plus swings big enough to be visible.
            trend = start + (end - start) * (fraction ** 0.85)
            value = trend * (1.0 + 0.11 * rng.uniform(-1, 1))
        elif shape == 'steps':
            # Mostly flat, occasionally jumps — birthday money, a big purchase.
            value = start + (end - start) * (round(fraction * 5) / 5)
        elif shape == 'seasonal':
            # Heating and cooling. Two peaks a year, trending gently.
            import math
            trend = start + (end - start) * fraction
            month_index = first.month
            value = trend * (1.0 + 0.55 * math.cos((month_index - 1) * math.pi / 6))
        else:
            raise ValueError(f'unknown shape {shape!r}')

        if wobble:
            value *= 1.0 + wobble * rng.uniform(-1, 1)

        # Clamped at both ends: the jitter can push a 2nd-of-the-month reading
        # to the 0th, and a 29th into a February that has not got one.
        jittered = min(max(day + rng.randint(-2, 2), 1),
                       _days_in_month(first.year, first.month))
        stamp = datetime.date(first.year, first.month, jittered)
        if stamp > cutoff:
            continue
        readings.append((str(trim_number(round(max(value, 0.0), 2))), stamp.isoformat()))

    if finish is not None and readings:
        readings[-1] = (_cents(finish), readings[-1][1])

    # An account whose reading day falls after today loses this month's entry
    # and starts the demo looking a month stale — which is the right behaviour
    # and the wrong data. Anything not deliberately left to age gets a closing
    # reading from the last few days, so staleness in this vault is something
    # the account was given rather than an artefact of the day it was built.
    if not stop_before_days and readings:
        recent = TODAY - datetime.timedelta(days=rng.randint(1, 6))
        if readings[-1][1] < recent.isoformat():
            readings.append((readings[-1][0], recent.isoformat()))
    return readings


def flat_series(rng, values: list[tuple[str, str]]):
    """Readings given outright, for the handful that are a story rather than a
    shape — a raise, a windfall, a bill that arrives four times a year."""
    return [(str(trim_number(float(amount))), date) for amount, date in values]


def _raises(rng, pay: int, days_back) -> list[tuple[str, str]]:
    """Yearly pay readings ending on `pay`, taken that many days back, with a
    raise of 2.5 to 4% between each. Oldest first."""
    readings = []
    for days in sorted(days_back):
        readings.append((str(pay), behind(days)))
        pay = _round_to(pay / rng.uniform(1.025, 1.04), 10)
    return list(reversed(readings))


def ahead(days: int) -> str:
    return (TODAY + datetime.timedelta(days=days)).isoformat()


def behind(days: int) -> str:
    return (TODAY - datetime.timedelta(days=days)).isoformat()


# -----------------------------------------------------------------------------
# The household
#
# Each entry is (fields, history spec). The history spec is None for the
# categories that track no balance, a dict for a generated shape, or a list for
# readings given outright.
# -----------------------------------------------------------------------------

def build(household: Household, rng, money: dict) -> list[tuple[dict, object]]:
    """The accounts, with `money` (see figures) in the ones the household's
    figures size."""
    rows = []

    def add(category, history=None, **fields):
        rows.append(({'category': category, **fields}, history))

    # -- Banking --------------------------------------------------------------
    # The household's own accounts follow its pay; the children's do not.
    def cash(start, end):
        factor = money['scale'] * rng.uniform(0.9, 1.1)
        return {'start': round(start * factor, 2), 'end': round(end * factor, 2)}

    paychecks = 'Both paychecks land' if household.two_incomes else 'Paycheck lands'
    add('Banking', {**cash(2400, 3150), 'shape': 'saw', 'day': 4},
        name='Springfield National — Joint Checking', bank_name='Springfield National Bank',
        account_number='4404102938', routing_number='061000227', account_type='Checking',
        username='homer.j.simpson', password='D0ughnut-Fri4y-Checking!',
        website='https://springfieldnational.example.com',
        notes=f"{paychecks} here on the 1st and the 15th. Marge moves the rest out "
              f"before Homer notices it.", favorite=True)

    transfer = max(5, _round_to(180 * money['scale'], 5))
    add('Banking', {**cash(3200, 9800), 'shape': 'linear', 'wobble': 0.03, 'day': 6},
        name='Springfield National — Family Savings', bank_name='Springfield National Bank',
        account_number='4404102946', routing_number='061000227', account_type='Savings',
        username='marge.simpson', password='Blue-H4ir-Savings-Plan!',
        website='https://springfieldnational.example.com',
        notes=f'Automatic transfer of ${transfer:,} every payday. Do not mention to Homer.')

    add('Banking', {**cash(180, 540), 'shape': 'saw', 'day': 9,
                    'stop_before_days': 62},
        name="First Bank of Springfield — Homer's Duff Money",
        bank_name='First Bank of Springfield', account_number='7781204455',
        routing_number='061000552', account_type='Checking',
        username='chunkylover53', password='Mmm-Fl04ting-Money!',
        website='https://firstbankofspringfield.example.com',
        notes='"Discretionary." Not reconciled since the summer.')

    add('Banking', {**cash(850, 2150), 'shape': 'linear', 'wobble': 0.06, 'day': 11},
        name='Springfield Credit Union — Household Account',
        bank_name='Springfield Credit Union', account_number='2200198374',
        routing_number='271070801', account_type='Money Market',
        username='marge.b.simpson', password='Pretzel-W4gon-Float!',
        website='https://springfieldcu.example.com',
        notes='Groceries, school trips, and whatever the kids have broken this month.')

    add('Banking', {'start': 35, 'end': 290, 'shape': 'steps', 'day': 18,
                    'stop_before_days': 41},
        name="Bart's Kid Saver", bank_name='Springfield National Bank',
        account_number='4404880021', routing_number='061000227', account_type='Savings',
        username='elbarto', password='Ay-Carumb4-Savings!',
        website='https://springfieldnational.example.com',
        notes='Birthday money and the proceeds of at least one scheme.',
        exclude_retirement=CHECKED)

    add('Banking', {'start': 110, 'end': 830, 'shape': 'linear', 'wobble': 0.04, 'day': 20},
        name="Lisa's Saxophone Fund", bank_name='Springfield National Bank',
        account_number='4404880039', routing_number='061000227', account_type='Savings',
        username='lisa.m.simpson', password='Baritone-R33d-Fund!',
        website='https://springfieldnational.example.com',
        notes='Saving for a Yamaha. Tracks every cent of it herself.',
        exclude_retirement=CHECKED)

    add('Banking', {'start': 60, 'end': 385, 'shape': 'steps', 'day': 22},
        name="Maggie's Passbook", bank_name='Springfield Credit Union',
        account_number='2200440117', routing_number='271070801', account_type='Savings',
        username='marge.simpson', password='P4cifier-Passbook-2019!',
        website='https://springfieldcu.example.com',
        notes='Opened the week she was born. Grandparents top it up at Christmas.',
        exclude_retirement=CHECKED)

    add('Banking', {**cash(1200, 5600), 'shape': 'linear', 'wobble': 0.02, 'day': 15},
        name='Nuclear Plant CU — Emergency Fund',
        bank_name='Springfield Nuclear Employees Credit Union',
        account_number='9903441266', routing_number='271071140', account_type='Savings',
        username='hjs.employee', password='Sector-7G-Emergency!',
        website='https://sncecu.example.com',
        notes='Three months of expenses is the target. Currently about six weeks.')

    # Somebody else's money: off the dashboard and out of the plan both.
    add('Banking', {'start': 4100, 'end': 3350, 'shape': 'saw', 'day': 3},
        name="Grampa's Pension Account", bank_name='Springfield Credit Union',
        account_number='2200071953', routing_number='271070801', account_type='Checking',
        username='abe.simpson', password='Back-In-My-D4y-Pension!',
        website='https://springfieldcu.example.com',
        notes="Marge pays the Retirement Castle out of it. It's his money, not "
              "theirs, so it stays off the dashboard and out of the plan.",
        exclude_dashboard=CHECKED, exclude_retirement=CHECKED)

    add('Banking', {'start': 2100, 'end': 0, 'shape': 'linear', 'day': 14,
                    'stop_before_days': 300},
        name='Shelbyville Savings — Old Joint Account', bank_name='Shelbyville Savings',
        account_number='5512003388', routing_number='062000019', account_type='Checking',
        username='homer.simpson', password='Shelbyville-St1nks-99!',
        closed=True, notes='Closed after the branch shut. Balance moved to Springfield National.')

    # -- Credit Cards ---------------------------------------------------------
    add('Credit Cards', {'start': 1850, 'end': 610, 'shape': 'linear', 'wobble': 0.08, 'day': 8},
        name='Kwik-E-Mart Rewards Visa', card_number='4539221166702847',
        cardholder_name='Homer J Simpson', credit_limit='4000', minimum_payment='35',
        due_date=ahead(2), due_cycle='Monthly', interest_rate='22.9',
        username='chunkylover53', password='Th4nk-You-Come-Again!',
        website='https://kwikemartcard.example.com',
        notes='Squishee points. Homer insists these are an investment.', favorite=True)

    add('Credit Cards', {'start': 3400, 'end': 1980, 'shape': 'linear', 'wobble': 0.05, 'day': 5},
        name='Springfield National Platinum', card_number='5425233430109903',
        cardholder_name='Marge B Simpson', credit_limit='9000', minimum_payment='75',
        due_date=ahead(15), due_cycle='Monthly', interest_rate='18.4',
        username='marge.simpson', password='Platinum-Bouff4nt-Card!',
        website='https://springfieldnational.example.com',
        notes='The one that gets paid first. Balance from the furnace replacement.')

    add('Credit Cards', {'start': 380, 'end': 820, 'shape': 'saw', 'day': 17},
        name='Duff Rewards Card', card_number='4024007196953842',
        cardholder_name='Homer J Simpson', credit_limit='1500', minimum_payment='25',
        due_date=ahead(22), due_cycle='Monthly', interest_rate='26.5',
        username='duffman.fan1', password='Oh-Y34h-Duff-Rewards!',
        website='https://duffrewards.example.com',
        notes='Going the wrong way. Marge has raised this twice.')

    add('Credit Cards', {'start': 290, 'end': 85, 'shape': 'linear', 'wobble': 0.1, 'day': 21},
        name='Try-N-Save Store Card', card_number='6011445566778899',
        cardholder_name='Marge B Simpson', credit_limit='800', minimum_payment='20',
        due_date=ahead(26), due_cycle='Monthly', interest_rate='24.9',
        username='marge.simpson', password='Try-N-Sav3-Store-Card!',
        website='https://trynsave.example.com', notes='Nearly cleared.')

    add('Credit Cards', {'start': 720, 'end': 240, 'shape': 'linear', 'wobble': 0.09, 'day': 13},
        name="Costington's Department Store Card", card_number='6011992288334455',
        cardholder_name='Marge B Simpson', credit_limit='2500', minimum_payment='30',
        due_date=ahead(19), due_cycle='Monthly', interest_rate='25.4',
        username='marge.b.simpson', password='Costingtons-S4le-Rack!',
        website='https://costingtons.example.com',
        notes='"Over a century of trust." And 25.4% APR.')

    add('Credit Cards', {'start': 60, 'end': 155, 'shape': 'saw', 'day': 24,
                         'stop_before_days': 26},
        name='Krusty Burger Bonus Card', card_number='4716330099114488',
        cardholder_name='Homer J Simpson', credit_limit='600', minimum_payment='15',
        due_date=ahead(6), due_cycle='Monthly', interest_rate='27.9',
        username='chunkylover53', password='Krusty-B0nus-Burger!',
        website='https://krustyburger.example.com',
        notes='Statement has not been opened since August.')

    add('Credit Cards', flat_series(None, [
            ('0', behind(210)), ('95', behind(150)), ('140', behind(95)),
            ('175', behind(50)), ('195', behind(12))]),
        name="Bart's Emergency-Only Card", card_number='4539880022116677',
        cardholder_name='Bartholomew J Simpson', credit_limit='300',
        minimum_payment='15', due_date=ahead(23), due_cycle='Monthly',
        interest_rate='29.9', username='elbarto',
        password='Eat-My-Sh0rts-Emergency!', website='https://springfieldnational.example.com',
        notes='Definition of "emergency" under active negotiation.')

    add('Credit Cards', {'start': 900, 'end': 0, 'shape': 'linear', 'day': 16,
                         'stop_before_days': 240},
        name='Shelbyville Trust Mastercard', card_number='5218774411229900',
        cardholder_name='Homer J Simpson', credit_limit='2000', minimum_payment='25',
        interest_rate='23.9', username='homer.simpson',
        password='Shelbyville-Tru5t-Closed!', closed=True,
        notes='Paid off and cut up. Kept for the credit history.')

    # -- Loans ----------------------------------------------------------------
    add('Loans', {'start': 161800, 'end': 149400, 'shape': 'linear', 'day': 2},
        name='742 Evergreen Terrace Mortgage', lender='Springfield National Bank',
        account_number='MTG-0742-EVG', loan_type='Mortgage', original_amount='198000',
        interest_rate='5.875', monthly_payment='1284',
        due_date=ahead(11), due_cycle='Monthly', payoff_date='2041-06-01',
        username='homer.j.simpson', password='Evergr33n-Terrace-742!',
        website='https://springfieldnational.example.com',
        notes='Refinanced once, in a year nobody wants to talk about.', favorite=True)

    add('Loans', {'start': 13900, 'end': 5800, 'shape': 'linear', 'day': 7},
        name='Pink Sedan Auto Loan', lender='Springfield Credit Union',
        account_number='AUTO-118823', loan_type='Auto', original_amount='24500',
        interest_rate='7.4', monthly_payment='412',
        due_date=ahead(4), due_cycle='Monthly', payoff_date='2028-02-07',
        username='marge.b.simpson', password='Pink-S3dan-Payment!',
        website='https://springfieldcu.example.com',
        notes='The family car. Held together by optimism.')

    add('Loans', {'start': 21600, 'end': 13500, 'shape': 'linear', 'day': 19},
        name='Canyonero Auto Loan', lender='Springfield Auto Finance',
        account_number='AUTO-770145', loan_type='Auto', original_amount='31900',
        interest_rate='8.1', monthly_payment='498',
        due_date=ahead(21), due_cycle='Monthly', payoff_date='2029-11-19',
        username='marge.simpson', password='Canyoner0-Twelve-Yards!',
        website='https://springfieldautofinance.example.com',
        notes='Squirrel-crushing, deer-smacking. Also 12 miles per gallon.')

    add('Loans', {'start': 12000, 'end': 8300, 'shape': 'linear', 'day': 3},
        name='Home Equity Line of Credit', lender='Springfield National Bank',
        account_number='HELOC-0742', loan_type='Home Equity', original_amount='15000',
        interest_rate='9.25', monthly_payment='245',
        due_date=ahead(25), due_cycle='Monthly',
        username='homer.j.simpson', password='Basement-R3model-HELOC!',
        website='https://springfieldnational.example.com',
        notes='Drawn for the roof, the furnace, and one trampoline.')

    add('Loans', {'start': 6000, 'end': 4750, 'shape': 'linear', 'day': 26},
        name="Lisa's College Prep Loan", lender='Springfield Education Trust',
        account_number='EDU-449012', loan_type='Student', original_amount='7500',
        interest_rate='4.5', monthly_payment='96',
        due_date=ahead(45), due_cycle='Quarterly', payoff_date='2032-09-26',
        username='marge.b.simpson', password='Ivy-L34gue-Someday!',
        website='https://springfieldedtrust.example.com',
        notes='Quarterly billing. She will get a scholarship anyway.')

    add('Loans', flat_series(None, [
            ('520', behind(420)), ('470', behind(330)), ('395', behind(240)),
            ('330', behind(150)), ('285', behind(80)), ('240', behind(21))]),
        name="Moe's Tavern Running Tab", lender="Moe's Tavern",
        account_number='TAB-HJS-001', loan_type='Personal', original_amount='800',
        interest_rate='0', monthly_payment='45',
        due_date=behind(142), due_cycle='One-off',
        username='—', password='—',
        notes='One-off "settle by May" that was not settled by May. '
              'Moe has stopped mentioning it, which is worse.')

    add('Loans', {'start': 2400, 'end': 0, 'shape': 'linear', 'day': 10,
                  'stop_before_days': 190},
        name='Nuclear Plant Employee Advance', lender='Springfield Nuclear Power Plant',
        account_number='ADV-SEC7G-0042', loan_type='Personal', original_amount='3000',
        interest_rate='0', monthly_payment='150',
        username='hjs.employee', password='Payroll-Adv4nce-7G!', closed=True,
        notes='Paid off through payroll. Mr. Burns wants it on record that he noticed.')

    add('Loans', {'start': 4100, 'end': 0, 'shape': 'linear', 'day': 23,
                  'stop_before_days': 400},
        name="Boat Loan — 'The Li'l Bastard'", lender='Springfield Marine Credit',
        account_number='MAR-220041', loan_type='Personal', original_amount='9200',
        interest_rate='9.9', monthly_payment='210',
        username='homer.j.simpson', password='Sailing-Aw4y-Boat-Loan!', closed=True,
        notes='Boat sold. Loan closed. Nobody discusses the boat.')

    # -- Investments ----------------------------------------------------------
    # Every balance and payment here comes from the household's figures. Each
    # history is worked back from today's balance and ends on it exactly.
    balances, payments = money['balances'], money['payments']
    yearly = yearly_payments(money)

    def invested(key, day, start=None, **spec):
        balance = balances[key]
        if start is None:
            start = _history_start(balance, yearly.get(key, 0))
        return {'start': start, 'end': balance, 'shape': 'vol', 'day': day,
                'finish': balance, **spec}

    def paid(key):
        return {'contribution_amount': _cents(payments[key]),
                'contribution_period': _PAYMENTS[key][0]}

    traditional, roth = money['balance_parts']
    traditional_paid, roth_paid = money['payment_parts']
    add('Investments', invested('401k', 28),
        name='Nuclear Plant 401(k)', institution='Springfield Nuclear Power Plant',
        account_number='401K-7G-0042', account_type='401k', tax_treatment=BOTH,
        traditional_balance=traditional, roth_balance=roth,
        traditional_contribution=traditional_paid, roth_contribution=roth_paid,
        contribution_period=_PAYMENTS['401k'][0],
        username='hjs.employee', password='Sector-7G-R3tirement!',
        website='https://sncplan.example.com',
        notes='Part Traditional, part Roth, out of every paycheck. Vested. '
              'The biggest account they have.',
        favorite=True)

    since = ('Contributed to every month since.' if household.two_incomes else
             "A spousal IRA now, paid in from Homer's pay every month.")
    add('Investments', invested('roth_ira', 27),
        name="Marge's Roth IRA", institution='Springfield Mutual',
        account_number='IRA-558820', account_type='Roth IRA', **paid('roth_ira'),
        username='marge.b.simpson', password='Roth-IRA-Bouff4nt-2020!',
        website='https://springfieldmutual.example.com',
        notes=f'Opened with pretzel-wagon money. {since}')

    if household.two_incomes:
        add('Investments', invested('403b', 26),
            name='Springfield Elementary 403(b)', institution='Springfield School District',
            account_number='403B-SE-1187', account_type='403b', tax_treatment=TRADITIONAL,
            **paid('403b'),
            username='marge.b.simpson', password='Sch00l-District-403b!',
            website='https://springfieldschools.example.com',
            notes='Through the school district, Traditional only. The district has '
                  'been "looking into" a Roth option for years.')

    add('Investments', invested('hsa', 21),
        name='Nuclear Plant HSA', institution='Springfield Mutual',
        account_number='HSA-7G-0042', account_type='HSA', **paid('hsa'),
        username='hjs.employee', password='Health-S4vings-Sector7G!',
        website='https://sncplan.example.com',
        notes='Family coverage through the plant. Invested, not spent: the '
              'receipts are in a shoebox.')

    add('Investments', invested('529', 25),
        name="Lisa's 529 College Savings", institution='Springfield Education Trust',
        account_number='529-LMS-0008', account_type='Other', **paid('529'),
        username='marge.b.simpson', password='529-Saxoph0ne-Scholar!',
        website='https://springfieldedtrust.example.com',
        notes='Target is an out-of-state private university. Current pace: not that.',
        exclude_retirement=CHECKED)

    add('Investments', invested('ugma', 24),
        name="Bart's UGMA Custodial Account", institution='Springfield Mutual',
        account_number='UGMA-BJS-0010', account_type='Brokerage',
        contribution_amount='40', username='marge.b.simpson',
        password='Custodial-Und3rachiever!',
        website='https://springfieldmutual.example.com',
        notes='He gets control of it at 21. Marge is not looking forward to it.',
        exclude_retirement=CHECKED)

    add('Investments', invested('brokerage', 14, wobble=0.05),
        name='Springfield Mutual Brokerage', institution='Springfield Mutual',
        account_number='BRK-441002', account_type='Brokerage', **paid('brokerage'),
        username='homer.j.simpson', password='Br0kerage-Hold-The-Line!',
        website='https://springfieldmutual.example.com',
        notes='Taxable account. Three funds and one very bad stock tip from Lenny.')

    add('Investments', invested('espp', 16, start=balances['espp'] * 0.6,
                                stop_before_days=77),
        name='Duff Corp Employee Stock', institution='Duff Corporation',
        account_number='ESPP-DUFF-0199', account_type='Brokerage',
        contribution_amount='0', username='duffman.fan1',
        password='ESPP-Duff-Sh4res-Yeah!',
        website='https://duffcorp.example.com',
        notes='From the taste-tester stipend years. Statement not opened since July.')

    bought_at = rng.uniform(4.0, 4.6)
    add('Investments', invested('crypto', 11, start=balances['crypto'] * bought_at,
                                wobble=0.2, stop_before_days=113),
        name="Homer's Crypto Experiment", institution='CoinDuff Exchange',
        account_number='WALLET-0xD0H', account_type='Other', contribution_amount='0',
        username='chunkylover53', password='D0h-Coin-To-The-Moon!',
        website='https://coinduff.example.com',
        notes=f'Down {round(100 * (1 - 1 / bought_at))}%. Homer describes this as '
              f'"a long-term position". Not looked at since the spring.')

    add('Investments', invested('index_ira', 29),
        name='Springfield Index Fund', institution='Springfield Mutual',
        account_number='IDX-770033', account_type='IRA', tax_treatment=TRADITIONAL,
        **paid('index_ira'),
        username='marge.b.simpson', password='Index-Fund-Bor1ng-Good!',
        website='https://springfieldmutual.example.com',
        notes="Marge's idea. Quietly the best-performing thing in the house.")

    # Tax Treatment left blank on purpose: the planner counts it as
    # Traditional and says it assumed so.
    add('Investments', invested('rollover', 19),
        name="Rollover IRA — Bowlarama Job", institution='Springfield Mutual',
        account_number='IRA-RO-330917', account_type='IRA',
        username='homer.j.simpson', password='Pin-M0nkey-Rollover!',
        website='https://springfieldmutual.example.com',
        notes="Rolled over from the pin-monkey job at Barney's Bowlarama. Nobody "
              "wrote down whether it was Traditional, so the planner assumes it is.")

    # -- Property -------------------------------------------------------------
    # Revalued a few times a year rather than monthly, as a house or a car is.
    # Each sits against its loan, so net worth counts the asset as well as the
    # debt on it.
    add('Property', flat_series(None, [
            ('268000', behind(905)), ('272500', behind(720)), ('276000', behind(540)),
            ('281000', behind(360)), ('285500', behind(180)), ('289500', behind(25))]),
        name='742 Evergreen Terrace', property_type='Home',
        username='homer.j.simpson', password='Mmm-Pr0perty-T4xes!',
        website='https://springfieldassessor.example.com',
        notes="The county's figure. It has not noticed the treehouse.", favorite=True)

    add('Property', flat_series(None, [
            ('9400', behind(900)), ('8400', behind(545)), ('7300', behind(180)),
            ('7000', behind(30))]),
        name='Pink Sedan', property_type='Vehicle',
        username='marge.b.simpson', password='Pink-S3dan-Bluebook!',
        website='https://springfielddmv.example.com',
        notes='Worth more to this family than to any dealer in town.')

    add('Property', flat_series(None, [
            ('27500', behind(900)), ('24300', behind(545)), ('21200', behind(180)),
            ('19800', behind(35))]),
        name='Canyonero', property_type='Vehicle',
        notes='Twelve yards long, two lanes wide. Depreciating at about the same scale.')

    # -- Income ---------------------------------------------------------------
    # The jobs are the only pay still open, so the income the dashboard counts
    # is the pay the household's figures asked for. The rest are closed:
    # income that was.
    pays = money['pays']
    add('Income', _raises(rng, pays['homer'], (900, 540, 160)),
        name='Springfield Nuclear Power Plant — Safety Inspector', income_type='Pay',
        username='hjs.employee', password='Payroll-Sector-7G-2026!',
        website='https://sncpayroll.example.com',
        notes='Grade 4 technician, Sector 7-G. Last raise in April. '
              'Mr. Burns still does not know his name.')

    if household.two_incomes:
        add('Income', _raises(rng, pays['marge'], (700, 130)),
            name='Springfield Elementary — Front Office', income_type='Pay',
            username='marge.b.simpson',
            password='Hall-P4ss-Front-Office!',
            website='https://springfieldelementary.example.com',
            notes="Started when Maggie went to daycare. Has now read Bart's "
                  "permanent record in full.")

    # Social Security still to come: a future figure, not a current one, so
    # it is ticked off the dashboard. The retirement plan reads it, Homer's as
    # yours and Marge's as your partner's, as the plan has them.
    homer_ss, marge_ss = benefits(pays)
    add('Income', name='Social Security — Homer', income_type='Social Security',
        monthly_benefit=str(homer_ss), whose='You', exclude_dashboard=CHECKED,
        username='homer.j.simpson',
        password='My-SSA-D0h-742!', website='https://ssa.example.gov',
        notes='Benefit at 67 from the last statement. Has asked twice whether '
              'it can be paid in donuts.')
    add('Income', name='Social Security — Marge', income_type='Social Security',
        monthly_benefit=str(marge_ss), whose='Partner', exclude_dashboard=CHECKED,
        username='marge.b.simpson',
        password='Blue-Hair-SSA-742!', website='https://ssa.example.gov',
        notes='Benefit at 67, or half of Homer\'s if that is more.')

    add('Income', flat_series(None, [
            ('3200', behind(880)), ('4100', behind(620)), ('5400', behind(400))]),
        name="Marge's Pretzel Wagon", income_type='Other', username='marge.b.simpson',
        password='Pretzel-Wag0n-Books!', website='https://springfieldcu.example.com',
        notes='Sold when the franchise war started. Kept for the tax records.',
        closed=True)

    add('Income', flat_series(None, [('2800', behind(640))]),
        name='Mr. Plow — Winter Income', income_type='Other', username='mrplow.homer',
        password='Call-Mr-Pl0w-3550!', website='https://springfieldshopper.example.com',
        notes='"That\'s my name, that name again is Mr. Plow." Ended when the '
              'Plow King came to town.', closed=True)

    add('Income', flat_series(None, [('1800', behind(600))]),
        name='Duff Taste-Tester Stipend', income_type='Other', username='duffman.fan1',
        password='T4ste-Tester-Stipend!', website='https://duffcorp.example.com',
        notes='Ended when the programme did. Kept for the tax records.', closed=True)

    # -- Utilities ------------------------------------------------------------
    add('Utilities', {'start': 168, 'end': 196, 'shape': 'seasonal', 'day': 24},
        name='Springfield Power & Light', provider='Springfield Power & Light',
        account_number='SPL-0742-EVG', service_type='Electric',
        due_date=ahead(4), due_cycle='Monthly', auto_pay='No',
        username='homer.j.simpson', password='Power-And-L1ght-742!',
        website='https://springfieldpower.example.com',
        notes='Spikes every summer. The air conditioning is not negotiable.')

    add('Utilities', {'start': 58, 'end': 71, 'shape': 'linear', 'wobble': 0.12, 'day': 26},
        name='Springfield Water Works', provider='Springfield Water Works',
        account_number='SWW-0742', service_type='Water',
        due_date=ahead(20), due_cycle='Monthly', auto_pay='Yes',
        username='marge.b.simpson', password='W4ter-Works-Springfield!',
        website='https://springfieldwater.example.com',
        notes='Auto-pay. The lawn is Homer\'s responsibility and it shows.')

    add('Utilities', {'start': 94, 'end': 112, 'shape': 'seasonal', 'day': 20},
        name='Springfield Gas Company', provider='Springfield Gas Company',
        account_number='SGC-774412', service_type='Gas',
        due_date=ahead(24), due_cycle='Monthly', auto_pay='No',
        username='marge.b.simpson', password='G4s-Company-Furnace!',
        website='https://springfieldgas.example.com',
        notes='New furnace cut this by a third. The HELOC paid for the furnace.')

    add('Utilities', {'start': 118, 'end': 149, 'shape': 'linear', 'wobble': 0.04, 'day': 18},
        name='Springfield Cable & Internet', provider='Springfield Cable',
        account_number='SCI-889900', service_type='Internet',
        due_date=ahead(6), due_cycle='Monthly', auto_pay='Yes',
        username='chunkylover53', password='C4ble-Internet-Bundle!',
        website='https://springfieldcable.example.com',
        notes='Went up again. Bart says he can fix it. Bart may not fix it.')

    add('Utilities', flat_series(None, [
            ('132', behind(380)), ('132', behind(290)), ('138', behind(200)),
            ('138', behind(110)), ('144', behind(20))]),
        name='Ma Bell Springfield — Home Phone', provider='Ma Bell Springfield',
        account_number='MBS-5550199', service_type='Phone',
        due_date=ahead(38), due_cycle='Quarterly', auto_pay='No',
        username='marge.b.simpson', password='H0me-Phone-Quarterly!',
        website='https://mabellspringfield.example.com',
        notes='Billed quarterly. Kept because Grampa will not call a mobile.')

    add('Utilities', flat_series(None, [
            ('96', behind(370)), ('96', behind(280)), ('102', behind(190)),
            ('102', behind(100)), ('108', behind(14))]),
        name='Springfield Waste Management', provider='Springfield Sanitation',
        account_number='SSD-0742-A', service_type='Trash',
        due_date=ahead(29), due_cycle='Quarterly', auto_pay='No',
        username='homer.j.simpson', password='Garbageman-Can-3550!',
        website='https://springfieldsanitation.example.com',
        notes='Do not let Homer run for Sanitation Commissioner again.')

    add('Utilities', flat_series(None, [
            ('310', behind(730)), ('325', behind(365)), ('340', behind(8))]),
        name='Springfield Heights Water District — Annual Levy',
        provider='Springfield Heights Water District', account_number='SHWD-0742',
        service_type='Water', due_date=ahead(357), due_cycle='Annually', auto_pay='No',
        username='marge.b.simpson', password='Annual-L3vy-Water-District!',
        notes='Once a year, and it always arrives as a surprise.')

    add('Utilities', flat_series(None, [
            ('180', behind(720)), ('190', behind(355)), ('205', behind(5))]),
        name='Nuclear Plant Parking Permit', provider='Springfield Nuclear Power Plant',
        account_number='PARK-7G-0042', service_type='Other',
        due_date=ahead(5), due_cycle='Annually', auto_pay='No',
        username='hjs.employee', password='P4rking-Permit-Sector7G!',
        notes='Renews next week. Space 42, nowhere near the door.')

    # -- Websites -------------------------------------------------------------
    add('Websites', None, name='Kwik-E-Mart Online', website='https://kwikemart.example.com',
        username='chunkylover53', email='chunkylover53@springfieldnet.example.com',
        password='Squishee-Rewards-2026!',
        security_question='Favourite flavour?', security_answer='Purple',
        notes='Apu knows the account number by heart anyway.')

    add('Websites', None, name='Duff Beer Fan Club', website='https://duff.example.com',
        username='duffman.fan1', email='chunkylover53@springfieldnet.example.com',
        password='Duffman-Says-Oh-Y34h!',
        security_question='First Duff?', security_answer='Duff Dry, age nineteen',
        notes='Newsletter, coupons, and one regrettable forum history.')

    add('Websites', None, name='Krusty Burger Rewards',
        website='https://krustyburger.example.com', username='chunkylover53',
        email='chunkylover53@springfieldnet.example.com',
        password='Krusty-Rew4rds-Meal-Deal!',
        security_question='Favourite menu item?', security_answer='Ribwich',
        notes='Points expire and Homer takes it personally every time.')

    add('Websites', None, name='Springfield Shopper',
        website='https://springfieldshopper.example.com', username='marge.b.simpson',
        email='marge.simpson@springfieldnet.example.com',
        password='Shopper-Subscr1ption-2026!',
        security_question='Street you grew up on?', security_answer='Highland Avenue',
        notes='Digital subscription plus the Sunday paper Bart delivers.')

    add('Websites', None, name='SpringfieldNet Webmail',
        website='https://mail.springfieldnet.example.com', username='chunkylover53',
        email='chunkylover53@springfieldnet.example.com',
        password='W3bmail-Master-Login-42!',
        security_question='Name of first pet?', security_answer='Santa\'s Little Helper',
        notes='The recovery address for almost everything else. Change this first '
              'if anything ever goes wrong.', favorite=True)

    add('Websites', None, name='Itchy & Scratchy Land Tickets',
        website='https://itchyandscratchyland.example.com', username='marge.b.simpson',
        email='marge.simpson@springfieldnet.example.com',
        password='The-Violence-Pl4ce-Tix!',
        security_question='Last visit?', security_answer='The year of the parking incident',
        notes='Season passes. "The amusement park of the future, today."')

    add('Websites', None, name='MyPods & Broomsticks Store',
        website='https://mypods.example.com', username='lisa.m.simpson',
        email='lisa.simpson@springfieldnet.example.com',
        password='MyP0d-Store-Account-88!',
        security_question='First album bought?', security_answer='Bleeding Gums Murphy, Live',
        notes='Family sharing is on. Bart has been warned.')

    add('Websites', None, name='Springfield Elementary Parent Portal',
        website='https://springfieldelementary.example.com', username='marge.b.simpson',
        email='marge.simpson@springfieldnet.example.com',
        password='Parent-Port4l-Skinner!',
        security_question='Principal\'s name?', security_answer='Seymour Skinner',
        notes='Grades, detentions, and the detention appeals form. Bookmarked.')

    add('Websites', None, name='Bowl-A-Rama League Site',
        website='https://bowlarama.example.com', username='homer.j.simpson',
        email='chunkylover53@springfieldnet.example.com',
        password='Pin-P4ls-League-Night!',
        security_question='Team name?', security_answer='The Pin Pals',
        notes='Tuesday league. Standings, handicaps, and Moe\'s running commentary.')

    add('Websites', None, name='CompuGlobalHyperMegaNet (old)',
        website='https://compuglobalhypermeganet.example.com',
        username='homer.j.simpson', email='chunkylover53@springfieldnet.example.com',
        password='Compu-Gl0bal-Hyper-Mega!', closed=True,
        notes='Homer\'s internet company. Bought out by Bill Gates, in a manner of speaking.')

    # -- Secure Notes ---------------------------------------------------------
    add('Secure Notes', None, name='Wi-Fi — 742 Evergreen Terrace',
        content='Network: EvergreenTerrace_5G\n'
                'Password: D0nutsAndDuff-Guest-2026\n\n'
                'Guest network: EvergreenTerrace_Guest\n'
                'Password: WelcomeToSpringfield-Guest\n\n'
                'Router admin: 192.168.1.1 — admin / R0uter-Admin-742\n'
                'Bart is not to have the admin password. Again.',
        tags='wifi, home, router', favorite=True)

    add('Secure Notes', None, name='Basement Safe Combination',
        content='Fireproof safe, behind the shelving.\n'
                'Combination: 22 - 07 - 14\n\n'
                'Contents: birth certificates, the deed, marriage licence, '
                'two savings bonds from Grampa, and the good silver.\n'
                'Spare key to the safe is taped under the third stair.',
        tags='home, documents, emergency')

    add('Secure Notes', None, name='Insurance Policy Numbers',
        content='Auto — Springfield Mutual Insurance\n'
                '  Pink Sedan: SM-AUTO-4471902 (comprehensive)\n'
                '  Canyonero: SM-AUTO-4471910 (comprehensive)\n'
                '  Claims: 1-555-0142\n\n'
                'Home — Springfield Mutual Insurance\n'
                '  Policy: SM-HOME-0742EVG\n'
                '  Covers: dwelling, contents, and one trampoline (excluded after 2019)\n\n'
                'Life — Nuclear Plant group policy\n'
                '  Policy: SNPP-LIFE-0042, 2x salary, beneficiary Marge',
        tags='insurance, auto, home, life')

    add('Secure Notes', None, name='Passport Numbers — Simpson Family',
        content='Homer J Simpson — 4471 9023 88 — expires 2029-04-11\n'
                'Marge B Simpson — 4471 9023 96 — expires 2029-04-11\n'
                'Bartholomew J Simpson — 5580 1147 02 — expires 2028-08-30\n'
                'Lisa M Simpson — 5580 1147 10 — expires 2028-08-30\n'
                'Margaret Simpson — 5580 1147 29 — expires 2031-01-15\n\n'
                'All five are in the basement safe.',
        tags='travel, documents, family')

    add('Secure Notes', None, name='Nuclear Plant — Badge & Locker',
        content='Employee number: 0042\n'
                'Badge PIN: 7734\n'
                'Locker: Sector 7-G, number 19 — combination 03 - 31 - 08\n\n'
                'Payroll portal answers:\n'
                '  Mother\'s maiden name: Bouvier\n'
                '  First car: the one with the door that did not match\n\n'
                'Do NOT write the badge PIN anywhere at work.',
        tags='work, badge, payroll')

    add('Secure Notes', None, name='Home Alarm & Door Codes',
        content='Front door keypad: 1 - 9 - 8 - 9\n'
                'Alarm disarm: 5 - 5 - 0 - 1\n'
                'Alarm duress code: 9 - 1 - 1 - 9 (silent, calls the police)\n'
                'Garage remote pairing PIN: 4402\n\n'
                'Monitoring: Springfield Home Security, 1-555-0177, account SHS-0742.',
        tags='home, alarm, emergency')

    add('Secure Notes', None, name='Emergency Contacts',
        content='Marge mobile: 555-0113\n'
                'Homer mobile: 555-0114\n'
                'Grampa — Springfield Retirement Castle: 555-0176, room 12\n'
                'Patty & Selma: 555-0188\n'
                'Dr. Hibbert (family): 555-0130\n'
                'Dr. Nick (do not call first): 555-NICK\n'
                'Springfield Elementary: 555-0155\n'
                'Ned Flanders (spare key, next door): 555-0119',
        tags='emergency, family, medical')

    add('Secure Notes', None, name="Marge's Recipe Vault",
        content='Springfield Community Cookbook — contributor login\n'
                '  Site: recipes.springfield.example.com\n'
                '  Username: marge.b.simpson\n'
                '  Password: Pork-Ch0ps-And-Applesauce!\n\n'
                'Pretzel wagon dough ratio (do not share):\n'
                '  5 parts flour, 2 parts water, 1 part patience, '
                'and the salt goes on AFTER the bake.',
        tags='recipes, pretzel wagon')

    # -- A custom category, because the app allows any name -------------------
    add('Bowling League', None, name='Pin Pals — Team Dues',
        username='homer.j.simpson', password='Pin-P4ls-Dues-2026!',
        website='https://bowlarama.example.com',
        notes='$15 a week, collected by Moe, recorded by nobody. '
              'Custom category — the app takes whatever name you type.')

    add('Bowling League', None, name='Barney\'s Bowlarama — Locker 7',
        username='homer.j.simpson', password='L0cker-Seven-Bowling!',
        notes='Combination 07-11-07. Holds the good ball and one spare shirt.')

    return rows


# -----------------------------------------------------------------------------
# Writing
# -----------------------------------------------------------------------------

def _apply_history(rng, account_id: int, spec, parts: dict):
    """Files an account's readings. Returns the date of the newest one.

    A Both account's Traditional and Roth parts go on with its newest reading,
    saved as a dated balance through the same path the form uses, so the
    account ends up holding the parts of the balance it shows. Given at
    creation, they would file their sum as a reading dated today instead."""
    if spec is None:
        return None
    entries = spec if isinstance(spec, list) else series(rng, **spec)
    if not entries:
        return None
    if parts:
        *earlier, (_, newest) = entries
        if earlier:
            accounts_service.add_balance_entries(account_id, earlier)
        account = accounts_service.get_account(account_id)
        accounts_service.update_account(account_id, {**account, **parts},
                                        balance_date=newest)
    else:
        accounts_service.add_balance_entries(account_id, entries)
    return max(date for _, date in entries)


def _backdate(account_id: int, last_updated: str, date_added: str):
    """Sets the two stamps directly.

    Demo scaffolding, and the only thing in this script that goes round the
    service layer. There is no API for it because the app deliberately has
    none: last_updated means "when this record was last known good", and
    nothing in normal use is allowed to move it backwards. A demo vault needs
    records that are already stale on the day it is opened, which is a thing
    only time can otherwise produce.
    """
    with database.connect() as db:
        db.execute("UPDATE accounts SET last_updated = ?, date_added = ? WHERE id = ?",
                   (last_updated, date_added, account_id))
    accounts_service.invalidate_cache()


def build_vault(path, household: Household | None = None) -> dict:
    """Builds the demo vault at `path` for `household` (the defaults when
    None), and leaves it open and unlocked, as creating a vault does. Returns
    how many accounts went into each category, and how many of them are
    pinned and closed.

    Raises FileExistsError for a path already in use, and ValueError for a
    household figure outside LIMITS, both before any file is made. The checks
    are here rather than in either caller, so the script and the app's button
    both keep them.
    """
    global TODAY
    # Re-read on every build, not trusted from import time: the app imports
    # this once and may still be running tomorrow, and a vault built a day out
    # of step has every due date and stale account one day off.
    TODAY = datetime.date.today()

    household = household or Household()
    household.check()
    path = Path(path)
    if path.exists():
        raise FileExistsError(f'{path} already exists. Delete it first, or name another file.')

    path.parent.mkdir(parents=True, exist_ok=True)
    database.open_vault(path, create=True)
    auth.setup_master_password(PASSWORD)
    # PASSWORD is public, so the app warns whenever this vault is in use.
    preferences.mark_demo_vault()

    # Seeded from the figures as well as SEED: the same answers build the same
    # vault, and different ones a different scatter around their totals.
    rng = random.Random(f'{SEED}:{astuple(household)}')
    money = figures(household, rng)
    rows = build(household, rng, money)
    details = plan_details(household, rng, money)

    counts = {}
    pinned = []
    closed = []
    for fields, history in rows:
        fields = dict(fields)
        favorite = fields.pop('favorite', False)
        is_closed = fields.pop('closed', False)
        parts = {key: fields.pop(key) for key in SPLIT_FIELDS['current_balance']
                 if key in fields}
        # current_balance is deliberately not set here for anything with a
        # history. create_account would file a snapshot dated today, which would
        # then outrank every reading below it and hold the account on a figure
        # the series never produced. The readings are what set the balance —
        # _sync_current_balance points the account at its newest entry.
        account = accounts_service.create_account(fields)
        newest = _apply_history(rng, account['id'], history, parts)

        # date_added sits a little before the first reading, so an account that
        # has been going for years does not claim to have been filed today.
        opened = (newest and (datetime.date.fromisoformat(newest)
                              - datetime.timedelta(days=rng.randint(400, 1500)))
                  or TODAY - datetime.timedelta(days=rng.randint(200, 1400)))
        _backdate(account['id'], newest or TODAY.isoformat(),
                  (opened if isinstance(opened, datetime.date) else TODAY).isoformat())

        if favorite:
            pinned.append(account['id'])
        if is_closed:
            closed.append(account['id'])
        counts[fields['category']] = counts.get(fields['category'], 0) + 1

    for account_id in pinned:
        accounts_service.set_favorite(account_id, True)
    if closed:
        accounts_service.set_closed(closed, True)

    retirement_service.save_plan({**retirement.empty_plan(), 'details': details})

    return {'categories': counts, 'pinned': len(pinned), 'closed': len(closed)}


def main() -> int:
    path = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else DEFAULT_PATH
    if path.suffix != database.VAULT_SUFFIX:
        path = path.with_suffix(database.VAULT_SUFFIX)
    try:
        summary = build_vault(path)
    except FileExistsError as exc:
        print(exc)
        return 1

    counts = summary['categories']
    print(f'\nWrote {path}')
    print(f'Master password: {PASSWORD}\n')
    for category, count in sorted(counts.items()):
        print(f'  {category:<16} {count}')
    print(f'\n  {"pinned":<16} {summary["pinned"]}')
    print(f'  {"closed":<16} {summary["closed"]}')
    print(f'  {"total":<16} {sum(counts.values())}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

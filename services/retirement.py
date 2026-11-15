# -----------------------------------------------------------------------------
# services/retirement.py
# The retirement planner's link to the vault: the stored plan, and the savings,
# yearly contributions, Social Security and pensions worked out from the open
# accounts.
# -----------------------------------------------------------------------------

from core import preferences, retirement
from core.auth import require_session
from core.categories import (
    BENEFIT_TYPES, BOTH, CONTRIBUTION_PERIOD_KEY, CONTRIBUTIONS_PER_YEAR,
    EXCLUDE_RETIREMENT_KEY, INCOME_TYPE_KEY, MONTHLY_BENEFIT_KEY, PARTNERS, PENSION,
    RECEIVING_KEY, ROTH, SPLIT_FIELDS, TAX_TREATMENT_KEY, TAX_TREATMENT_TYPES,
    TRADITIONAL, WHOSE_KEY, add_amounts, is_checked, normalize_category, parse_money,
)
from services.accounts import list_accounts

INVESTMENTS = 'Investments'
BANKING = 'Banking'
INCOME = 'Income'

# The monthly benefits the Income records supply, by plan key. ss and
# partner_ss are benefits at 67; the _received two are being paid now.
BENEFIT_FIGURES = ('ss', 'partner_ss', 'ss_received', 'partner_ss_received',
                   'pension', 'partner_pension')

# Bank accounts counted as cash, in the taxable bucket.
CASH_TYPES = ('Checking', 'Savings', 'Money Market')

# Investments account types that settle their own bucket. The types in
# TAX_TREATMENT_TYPES go by their Tax Treatment instead.
_TYPE_BUCKETS = {'Roth IRA': 'roth', 'HSA': 'hsa', 'Brokerage': 'taxable'}

# Where each bucket's yearly contributions go in the plan.
_SAVE_KEYS = {'pretax': 'save_pretax', 'roth': 'save_roth',
              'taxable': 'save_taxable', 'hsa': 'save_hsa'}

# Notes on a counted account, for the view to put into words.
ASSUMED = 'assumed'        # no tax treatment (or a Both account without its
                           # amounts): counted as Traditional
ESTIMATED = 'estimated'    # its Traditional and Roth amounts no longer add up
                           # to its balance, so they were scaled to fit
NO_BALANCE = 'no_balance'  # no balance entered, or below zero: counted as 0
NO_PERIOD = 'no_period'    # a contribution amount with no period: not counted

# Why an account is left out, likewise.
NO_TYPE = 'no_type'
OTHER_TYPE = 'other_type'
OVERDRAWN = 'overdrawn'
EXCLUDED = 'excluded'      # ticked Exclude from Retirement


def load_plan() -> dict:
    """The saved plan, or an empty one when nothing is saved."""
    require_session()
    return preferences.get_retirement_plan() or retirement.empty_plan()


def save_plan(plan: dict) -> dict:
    """Stores the plan and returns it as stored."""
    require_session()
    return preferences.save_retirement_plan(plan)


def _amount(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    return parse_money(value)


def _investment_buckets(account: dict, balance: float) -> tuple[dict, str | None]:
    """How an Investments account's balance divides between the plan's buckets,
    and a note on how it was decided. ({}, None) for a type the plan leaves out."""
    kind = account.get('account_type')
    if kind in _TYPE_BUCKETS:
        return {_TYPE_BUCKETS[kind]: balance}, None
    if kind not in TAX_TREATMENT_TYPES:
        return {}, None

    treatment = account.get(TAX_TREATMENT_KEY)
    if treatment == ROTH:
        return {'roth': balance}, None
    if treatment == TRADITIONAL:
        return {'pretax': balance}, None
    if treatment == BOTH:
        traditional, roth = (_amount(account.get(key))
                             for key in SPLIT_FIELDS['current_balance'])
        if traditional is not None and roth is not None and traditional + roth > 0:
            if add_amounts(traditional, roth) == balance:
                return {'pretax': traditional, 'roth': roth}, None
            # Balance History moved the balance after the parts were entered.
            # The parts' shares are the best guess at how it divides now.
            pretax = balance * traditional / (traditional + roth)
            return {'pretax': pretax, 'roth': balance - pretax}, ESTIMATED
    # No treatment chosen, or a Both account without its two amounts.
    return {'pretax': balance}, ASSUMED


def _yearly_contributions(account: dict, buckets: dict) -> tuple[dict, bool]:
    """What goes into the account each year, by plan key, and whether an
    amount was left out for want of a period. A Both account's two
    contributions go to their own buckets; any other contribution goes where
    the balance went, or to pre-tax when the balance went nowhere (an empty
    account). An account marked as the partner's pays into the partner_save_
    keys, which stop when the partner retires."""
    per_year = CONTRIBUTIONS_PER_YEAR.get(account.get(CONTRIBUTION_PERIOD_KEY)) \
        if isinstance(account.get(CONTRIBUTION_PERIOD_KEY), str) else None
    traditional, roth = (_amount(account.get(key))
                         for key in SPLIT_FIELDS['contribution_amount'])
    if account.get(TAX_TREATMENT_KEY) == BOTH and traditional is not None and roth is not None:
        amounts = {'pretax': traditional, 'roth': roth}
    else:
        amount = _amount(account.get('contribution_amount'))
        target = max(buckets, key=buckets.get) if buckets else 'pretax'
        amounts = {target: amount} if amount else {}

    amounts = {bucket: value for bucket, value in amounts.items() if value and value > 0}
    if not amounts:
        return {}, False
    if per_year is None:
        return {}, True
    prefix = 'partner_' if account.get(WHOSE_KEY) == PARTNERS else ''
    return {prefix + _SAVE_KEYS[bucket]: value * per_year
            for bucket, value in amounts.items()}, False


def _benefit_figure(account: dict) -> str | None:
    """Which figure an Income record's monthly benefit adds to, or None for
    pay. Social Security already being received has a figure of its own: the
    plan pays it as it is, where a benefit still to come is the one at 67."""
    kind = account.get(INCOME_TYPE_KEY)
    if kind not in BENEFIT_TYPES:
        return None
    prefix = 'partner_' if account.get(WHOSE_KEY) == PARTNERS else ''
    if kind == PENSION:
        return f'{prefix}pension'
    received = '_received' if is_checked(account.get(RECEIVING_KEY)) else ''
    return f'{prefix}ss{received}'


def savings_from_accounts() -> dict:
    """What the open Investments and Banking accounts hold for the plan, and
    what goes into them each year, and the monthly Social Security and
    pensions the open Income records hold.

    Returns {'figures', 'counted', 'skipped'}. figures maps every key in
    retirement.SAVINGS_KEYS and BENEFIT_FIGURES to its total. counted lists
    each Investments or Banking account that
    added to them, as {id, name, category, amounts, yearly, notes, partner},
    where amounts and yearly are by plan key, partner is whether it is marked
    as the partner's, and notes holds any of ASSUMED,
    ESTIMATED, NO_BALANCE and NO_PERIOD. skipped lists the accounts left out,
    as {id, name, category, reason, type, balance}, reason being NO_TYPE,
    OTHER_TYPE, OVERDRAWN or EXCLUDED, and balance what it holds (0 when
    blank or below zero), for the Plan page to say how much was left out.

    Closed accounts are left out without a mention, as they are everywhere
    else. Nothing is moved between buckets by guesswork beyond what the notes
    say: an account the plan cannot place is listed, not counted."""
    figures = dict.fromkeys(retirement.SAVINGS_KEYS, 0)
    counted, skipped = [], []

    figures.update(dict.fromkeys(BENEFIT_FIGURES, 0))

    for account in list_accounts():
        if account.get('closed'):
            continue
        category = normalize_category(account.get('category') or '')
        if category == INCOME:
            key = _benefit_figure(account)
            if key:
                figures[key] += max(0, _amount(account.get(MONTHLY_BENEFIT_KEY)) or 0)
            continue
        if category not in (INVESTMENTS, BANKING):
            continue
        kind = account.get('account_type')
        balance = _amount(account.get('current_balance'))
        row = {'id': account['id'], 'name': account.get('name') or '', 'category': category}
        left_out = {'type': kind, 'balance': max(0, balance or 0)}
        if is_checked(account.get(EXCLUDE_RETIREMENT_KEY)):
            skipped.append({**row, 'reason': EXCLUDED, **left_out})
            continue
        notes = []

        if category == BANKING:
            if kind not in CASH_TYPES:
                skipped.append({**row, 'reason': OTHER_TYPE if kind else NO_TYPE, **left_out})
                continue
            if balance is not None and balance < 0:
                skipped.append({**row, 'reason': OVERDRAWN, **left_out})
                continue
            if balance is None:
                notes.append(NO_BALANCE)
            buckets, yearly = {'taxable': balance or 0}, {}

        else:
            if kind not in _TYPE_BUCKETS and kind not in TAX_TREATMENT_TYPES:
                skipped.append({**row, 'reason': OTHER_TYPE if kind else NO_TYPE, **left_out})
                continue
            if balance is None or balance < 0:
                notes.append(NO_BALANCE)
                balance = 0
            buckets, note = _investment_buckets(account, balance)
            if note:
                notes.append(note)
            yearly, missing_period = _yearly_contributions(account, buckets)
            if missing_period:
                notes.append(NO_PERIOD)

        for bucket, value in buckets.items():
            figures[bucket] += value
        for key, value in yearly.items():
            figures[key] += value
        counted.append({**row, 'amounts': buckets, 'yearly': yearly, 'notes': notes,
                        'partner': account.get(WHOSE_KEY) == PARTNERS})

    # Largest first, as the dashboard lists accounts, with ties on the name.
    counted.sort(key=lambda r: (-sum(r['amounts'].values()), str(r['name']).lower()))
    skipped.sort(key=lambda r: str(r['name']).lower())
    return {'figures': figures, 'counted': counted, 'skipped': skipped}

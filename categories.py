# -----------------------------------------------------------------------------
# categories.py
# Per-category field schemas and the value formatting helpers built on them.
#
# Each category maps to:
#   title        — label shown in the left panel
#   item_label   — singular noun used in headings and empty states
#   name_field   — field used for the list title
#   value_noun   — what current_balance is called here (default 'Balance')
#   stale_after  — (warn, danger) day thresholds for the last-updated stamp.
#                  Absent means this category never goes stale — see
#                  updated_age_level for why that is the right default.
#   fields       — fields rendered in the detail view and edit form
#
# Field flags:
#   sensitive    — masked by default with a show/hide toggle
#   copy         — gets a copy button. On a sensitive field the button copies
#                  the real value while it is still masked, so the number never
#                  has to go on screen at all — see the note in detail_view's
#                  _build_field about why that is the private path and
#                  drag-selecting a revealed value is not
#   link         — rendered as a clickable link
#   hide_in_form — shown in the detail view but not editable directly
#   default      — value a *new* record starts on. Select fields only, and
#                  deliberately never applied to an existing one
# -----------------------------------------------------------------------------

import datetime
import math
import re

# -----------------------------------------------------------------------------
# Field schemas
# -----------------------------------------------------------------------------

# Due-date recurrence, stored in extra_data beside the date it governs. Absent
# reads as ONE_OFF, so every record written before this existed keeps exactly
# the behaviour it had and nothing needs migrating.
#
# Declared up here rather than beside is_recurring because the schemas below
# spell these values out, and a literal in two places is a literal that can
# drift out of agreement with the function that matches on it.
# Whether the user has pinned this account, stored in extra_data beside the
# due cycle above and for the same reason: an encrypted blob already exists,
# and a plaintext column would say which handful of a vault's accounts its
# owner reaches for most — structure the file does not otherwise give away.
#
# No category declares it as a field, so nothing renders or exports it as one;
# it reaches a record only through services.accounts.set_favorite. Absent reads
# as not pinned, so every vault written before this existed is simply a vault
# with nothing pinned.
FAVORITE_KEY = 'favorite'


DUE_CYCLE_KEY = 'due_cycle'
ONE_OFF = 'One-off'
WEEKLY = 'Weekly'
MONTHLY = 'Monthly'
QUARTERLY = 'Quarterly'
ANNUAL = 'Annually'

# The options every category with a due date offers, in ascending period order
# so the list reads as a scale rather than as a set. One name per period: the
# stored value is this string, and a second spelling for the same period would
# be a second thing every reader has to know about.
DUE_CYCLES = [ONE_OFF, WEEKLY, MONTHLY, QUARTERLY, ANNUAL]

# How far one period of each cycle reaches, as (unit, size).
#
# 'month' rather than a day count for the last three, because a month is not a
# fixed number of days and the whole point of these is to land on the same day
# of the month every time. 'day' for weekly, where a week genuinely is seven
# days and a weekday is what recurs.
_CYCLE_STEPS = {
    WEEKLY:    ('day', 7),
    MONTHLY:   ('month', 1),
    QUARTERLY: ('month', 3),
    ANNUAL:    ('month', 12),
}

# Stored spellings that mean one of the cycles above, lowercased.
#
# The canonical names are here too, which is what makes this the whole lookup
# rather than a fallback beside one. The rest are spellings a user might type
# into a custom field, or that an import might carry in from another tool —
# read on the way in, never written on the way out, exactly like
# CATEGORY_ALIASES. 'Annually' is the name this app uses; 'annual' and 'yearly'
# are the two other ways everybody writes it.
_CYCLE_ALIASES = {
    **{name.lower(): name for name in _CYCLE_STEPS},
    'annual': ANNUAL, 'yearly': ANNUAL, 'year': ANNUAL,
    'quarter': QUARTERLY, 'every 3 months': QUARTERLY,
    'month': MONTHLY, 'every month': MONTHLY,
    'week': WEEKLY, 'every week': WEEKLY,
}


CATEGORY_CONFIG = {
    'Credit Cards': {
        'title': 'Credit Cards',
        'item_label': 'Credit Card',
        'name_field': 'name',
        # A statement a month, so three weeks quiet is worth a nudge.
        'stale_after': (21, 28),
        'fields': [
            {'key': 'name',            'label': 'Account Name',    'type': 'text',     'required': True},
            {'key': 'card_number',     'label': 'Card Number',     'type': 'text',     'sensitive': True, 'copy': True},
            {'key': 'cardholder_name', 'label': 'Cardholder Name', 'type': 'text'},
            {'key': 'credit_limit',    'label': 'Credit Limit',    'type': 'number',   'money': True},
            {'key': 'current_balance', 'label': 'Current Balance', 'type': 'number',   'money': True},
            {'key': 'minimum_payment', 'label': 'Minimum Payment', 'type': 'number',   'money': True},
            {'key': 'due_date',        'label': 'Due Date',        'type': 'date'},
            {'key': DUE_CYCLE_KEY,     'label': 'Repeats',         'type': 'select',
             'options': DUE_CYCLES, 'default': MONTHLY},
            {'key': 'interest_rate',   'label': 'Interest Rate',   'type': 'number',   'percent': True},
            {'key': 'username',        'label': 'Username',        'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',        'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',         'type': 'url',      'link': True, 'copy': True},
            {'key': 'last_updated',    'label': 'Last Updated',    'type': 'date',     'hide_in_form': True},
            {'key': 'notes',           'label': 'Notes',           'type': 'textarea'},
        ],
    },

    'Banking': {
        'title': 'Banking',
        'item_label': 'Bank Account',
        'name_field': 'name',
        'stale_after': (21, 28),
        'fields': [
            {'key': 'name',            'label': 'Account Name',    'type': 'text',     'required': True},
            {'key': 'bank_name',       'label': 'Bank Name',       'type': 'text'},
            {'key': 'account_number',  'label': 'Account Number',  'type': 'text',     'sensitive': True, 'copy': True},
            {'key': 'routing_number',  'label': 'Routing Number',  'type': 'text',     'sensitive': True, 'copy': True},
            {'key': 'account_type',    'label': 'Account Type',    'type': 'select',
             'options': ['Checking', 'Savings', 'Money Market', 'Other']},
            {'key': 'current_balance', 'label': 'Current Balance', 'type': 'number',   'money': True},
            {'key': 'username',        'label': 'Username',        'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',        'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',         'type': 'url',      'link': True, 'copy': True},
            {'key': 'last_updated',    'label': 'Last Updated',    'type': 'date',     'hide_in_form': True},
            {'key': 'notes',           'label': 'Notes',           'type': 'textarea'},
        ],
    },

    'Loans': {
        'title': 'Loans',
        'item_label': 'Loan',
        'name_field': 'name',
        # A fixed payment against a known schedule. The balance moves, but it
        # moves the way it already said it would, so it is not news monthly.
        'stale_after': (60, 90),
        'fields': [
            {'key': 'name',            'label': 'Account Name',   'type': 'text',     'required': True},
            {'key': 'lender',          'label': 'Lender',         'type': 'text'},
            {'key': 'account_number',  'label': 'Account Number', 'type': 'text',     'sensitive': True, 'copy': True},
            {'key': 'loan_type',       'label': 'Loan Type',      'type': 'select',
             'options': ['Auto', 'Mortgage', 'Personal', 'Student', 'Home Equity', 'Other']},
            {'key': 'original_amount', 'label': 'Original Amount', 'type': 'number',  'money': True},
            {'key': 'current_balance', 'label': 'Current Balance', 'type': 'number',  'money': True},
            {'key': 'interest_rate',   'label': 'Interest Rate',   'type': 'number',  'percent': True},
            {'key': 'monthly_payment', 'label': 'Monthly Payment', 'type': 'number',  'money': True},
            {'key': 'due_date',        'label': 'Due Date',        'type': 'date'},
            {'key': DUE_CYCLE_KEY,     'label': 'Repeats',         'type': 'select',
             'options': DUE_CYCLES, 'default': MONTHLY},
            # payoff_date takes no cycle: a loan is paid off once.
            {'key': 'payoff_date',     'label': 'Payoff Date',     'type': 'date'},
            {'key': 'username',        'label': 'Username',        'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',        'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',         'type': 'url',      'link': True, 'copy': True},
            {'key': 'last_updated',    'label': 'Last Updated',    'type': 'date',     'hide_in_form': True},
            {'key': 'notes',           'label': 'Notes',           'type': 'textarea'},
        ],
    },

    'Investments': {
        'title': 'Investments',
        'item_label': 'Investment Account',
        'name_field': 'name',
        # Nobody rebalances monthly, and a portfolio that moved on its own is
        # not a task — a quarter is the honest interval to re-read one on.
        'stale_after': (60, 90),
        'fields': [
            {'key': 'name',                'label': 'Account Name',        'type': 'text',   'required': True},
            {'key': 'institution',         'label': 'Institution',         'type': 'text'},
            {'key': 'account_number',      'label': 'Account Number',      'type': 'text',   'sensitive': True, 'copy': True},
            {'key': 'account_type',        'label': 'Account Type',        'type': 'select',
             'options': ['401k', 'IRA', 'Roth IRA', 'HSA', 'Brokerage', 'Other']},
            {'key': 'current_balance',     'label': 'Current Balance',     'type': 'number', 'money': True},
            {'key': 'contribution_amount', 'label': 'Contribution Amount', 'type': 'number', 'money': True},
            {'key': 'username',            'label': 'Username',            'type': 'text',   'copy': True},
            {'key': 'password',            'label': 'Password',            'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',             'label': 'Website',             'type': 'url',    'link': True, 'copy': True},
            {'key': 'last_updated',        'label': 'Last Updated',        'type': 'date',   'hide_in_form': True},
            {'key': 'notes',               'label': 'Notes',               'type': 'textarea'},
        ],
    },

    # current_balance holds the yearly figure — the history it drives is a record
    # of the pay rate over time, not of income received.
    'Income': {
        'title': 'Income',
        'item_label': 'Income Source',
        'name_field': 'name',
        'value_noun': 'Annual Pay',
        # Annual pay changes about once a year. Flagging it red four weeks after
        # a raise was recorded asked the user to re-confirm a figure that by
        # definition had not moved.
        'stale_after': (180, 365),
        'fields': [
            {'key': 'name',            'label': 'Source',       'type': 'text',     'required': True},
            {'key': 'current_balance', 'label': 'Annual Pay',   'type': 'number',   'money': True},
            {'key': 'username',        'label': 'Username',     'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',     'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',      'type': 'url',      'link': True, 'copy': True},
            {'key': 'last_updated',    'label': 'Last Updated', 'type': 'date',     'hide_in_form': True},
            {'key': 'notes',           'label': 'Notes',        'type': 'textarea'},
        ],
    },

    'Utilities': {
        'title': 'Utilities',
        'item_label': 'Utility Account',
        'name_field': 'name',
        'stale_after': (21, 28),
        'fields': [
            {'key': 'name',            'label': 'Account Name',   'type': 'text',   'required': True},
            {'key': 'provider',        'label': 'Provider',       'type': 'text'},
            {'key': 'account_number',  'label': 'Account Number', 'type': 'text',   'sensitive': True, 'copy': True},
            {'key': 'service_type',    'label': 'Service Type',   'type': 'select',
             'options': ['Electric', 'Gas', 'Water', 'Internet', 'Phone', 'Trash', 'Other']},
            {'key': 'current_balance', 'label': 'Current Balance', 'type': 'number', 'money': True},
            {'key': 'due_date',        'label': 'Due Date',        'type': 'date'},
            {'key': DUE_CYCLE_KEY,     'label': 'Repeats',         'type': 'select',
             'options': DUE_CYCLES, 'default': MONTHLY},
            {'key': 'auto_pay',        'label': 'Auto Pay',        'type': 'select', 'options': ['No', 'Yes']},
            {'key': 'username',        'label': 'Username',        'type': 'text',   'copy': True},
            {'key': 'password',        'label': 'Password',        'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',         'type': 'url',    'link': True, 'copy': True},
            {'key': 'last_updated',    'label': 'Last Updated',    'type': 'date',   'hide_in_form': True},
            {'key': 'notes',           'label': 'Notes',           'type': 'textarea'},
        ],
    },

    # No stale_after here, none on Secure Notes, and none on GENERIC_CONFIG
    # below. Nothing these hold drifts while nobody is looking: a password is as
    # correct today as the day it was filed, and a note does not go out of date
    # by being left alone. All three used to run the same 21/28 day clock as a
    # credit-card balance, which made a three-year-old note permanently urgent
    # and buried the accounts that genuinely needed doing.
    'Websites': {
        'title': 'Websites',
        'item_label': 'Website',
        'name_field': 'name',
        'fields': [
            {'key': 'name',              'label': 'Site Name',         'type': 'text',     'required': True},
            {'key': 'website',           'label': 'URL',               'type': 'url',      'link': True, 'copy': True},
            {'key': 'username',          'label': 'Username',          'type': 'text',     'copy': True},
            {'key': 'email',             'label': 'Email',             'type': 'email'},
            {'key': 'password',          'label': 'Password',          'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'security_question', 'label': 'Security Question', 'type': 'text',     'sensitive': True},
            {'key': 'security_answer',   'label': 'Security Answer',   'type': 'text',     'sensitive': True},
            {'key': 'last_updated',      'label': 'Last Updated',      'type': 'date',     'hide_in_form': True},
            {'key': 'notes',             'label': 'Notes',             'type': 'textarea'},
        ],
    },

    'Secure Notes': {
        'title': 'Secure Notes',
        'item_label': 'Note',
        'name_field': 'name',
        'fields': [
            {'key': 'name',         'label': 'Title',        'type': 'text',     'required': True},
            {'key': 'content',      'label': 'Content',      'type': 'textarea', 'sensitive': True, 'multiline': True},
            {'key': 'tags',         'label': 'Tags',         'type': 'text'},
            {'key': 'last_updated', 'label': 'Last Updated', 'type': 'date',     'hide_in_form': True},
        ],
    },
}


# Used wherever a category doesn't name the amount it tracks.
DEFAULT_VALUE_NOUN = 'Balance'


# Fallback schema used for custom or unknown categories.
GENERIC_CONFIG = {
    'item_label': 'Account',
    'name_field': 'name',
    'fields': [
        {'key': 'name',         'label': 'Name',         'type': 'text',     'required': True},
        {'key': 'username',     'label': 'Username',     'type': 'text',     'copy': True},
        {'key': 'password',     'label': 'Password',     'type': 'password', 'sensitive': True, 'copy': True},
        {'key': 'website',      'label': 'Website',      'type': 'url',      'link': True, 'copy': True},
        {'key': 'last_updated', 'label': 'Last Updated', 'type': 'date',     'hide_in_form': True},
        {'key': 'notes',        'label': 'Notes',        'type': 'textarea'},
    ],
}


# Slug-style category keys stored by versions predating the rename.
CATEGORY_ALIASES = {
    'creditcards': 'Credit Cards',
    'banking':     'Banking',
    'loans':       'Loans',
    'investments': 'Investments',
    'utilities':   'Utilities',
    'websites':    'Websites',
    'securenotes': 'Secure Notes',
}


def normalize_category(key: str) -> str:
    return CATEGORY_ALIASES.get(key, key)


def get_config(category: str | None) -> dict:
    """Schema for a category, falling back to the generic one for custom categories."""
    if category in CATEGORY_CONFIG:
        return CATEGORY_CONFIG[category]
    label = category or 'Account'
    return {**GENERIC_CONFIG, 'title': label, 'item_label': label}


def has_field(category: str | None, key: str) -> bool:
    return any(field['key'] == key for field in get_config(category)['fields'])


def searchable_fields(category: str | None) -> list[dict]:
    """The fields a search query may be matched against.

    Sensitive fields are left out, and not merely hidden from the results: a
    search that matches a secret is an oracle for it. Type a password into the
    box and the one account still listed is the account that uses it — the value
    never appears on screen, and it does not have to. Card numbers, account and
    routing numbers, security answers and note bodies all confirm the same way.

    Fields outside the category's own schema are skipped too. extra_data keeps
    whatever key space a record was written with, so a card moved to another
    category can still be carrying its old card_number, and nothing in the new
    schema marks that key sensitive any more."""
    return [
        field for field in get_config(category)['fields']
        if not field.get('sensitive') and field['type'] != 'password'
    ]


def stale_after(category: str | None) -> tuple[int, int] | None:
    """The (warn, danger) day thresholds for a category's last-updated stamp,
    or None where staleness is not a thing this category can be.

    None is the default on purpose. Staleness is a claim that the record has
    probably drifted since anyone looked, which only a value that moves on its
    own can make — so a category earns the check by declaring an interval,
    rather than being exempted from one it never wanted."""
    thresholds = get_config(category).get('stale_after')
    return tuple(thresholds) if thresholds else None


def value_noun(category: str | None) -> str:
    """What this category calls the amount current_balance tracks. Labels the
    history table and the amount dialogs so 'Balance' doesn't invite a monthly
    figure where a yearly one belongs."""
    return get_config(category).get('value_noun', DEFAULT_VALUE_NOUN)


# -----------------------------------------------------------------------------
# Value formatting
# -----------------------------------------------------------------------------

def format_money(value) -> str:
    """Formats a number as USD. Returns the input unchanged if it isn't numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '' if value is None else str(value)
    return f"-${abs(number):,.2f}" if number < 0 else f"${number:,.2f}"


def format_money_compact(value) -> str:
    """Money abbreviated to fit a chart axis: $1.2k, $3.4M, $950.

    Lives beside format_money rather than in the chart widget that draws it.
    Both spell an amount in the app's one currency, and a widget carrying its
    own '$' is a second place to find if that ever stops being true — which is
    the only reason this is not simply inlined where it is used."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '' if value is None else str(value)
    magnitude = abs(number)
    if magnitude >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"${number / 1_000:.1f}k"
    return f"${number:.0f}"


def format_date(value) -> str | None:
    """YYYY-MM-DD → MM-DD-YYYY. Returns None if the value isn't a date."""
    if not value:
        return None
    parts = str(value)[:10].split('-')
    if len(parts) != 3:
        return None
    year, month, day = parts
    return f"{month}-{day}-{year}"


def format_month(value) -> str:
    """YYYY-MM → MM-YY."""
    parts = str(value).split('-')
    return f"{parts[1]}-{parts[0][2:]}" if len(parts) >= 2 else str(value)


def format_value(value, field: dict) -> str:
    """Renders a stored value for the detail view."""
    if value is None or value == '':
        return '—'

    if field.get('money'):
        # format_money already handles the non-numeric case by returning the
        # value unchanged, which is the fallthrough this used to spell out with
        # a float() and a try/except of its own.
        return format_money(value)

    if field.get('percent'):
        try:
            return f"{trim_number(float(value))}%"
        except (TypeError, ValueError):
            pass

    if field.get('type') == 'date':
        return format_date(value) or str(value)

    return str(value)


def trim_number(number: float):
    """Drops a trailing .0 so 5.0 shows as 5 but 5.25 stays put.

    The one definition of "whole numbers stay whole" in the app. Public because
    three places need the same answer and used to each carry their own copy:
    the detail view's percent formatting here, services/history_io's
    canonical_amount (the stored form of an imported figure), and
    services/accounts._sync_current_balance (the figure copied off a history
    entry onto the account). A figure that round-trips through an export and
    back has to come out spelled the same way it went in, which only holds
    while one function decides the spelling."""
    return int(number) if number == int(number) else number


_MONEY_STRIP = re.compile(r'[$£€¥₹,\s]')


def parse_money(value) -> float | None:
    """Reads a user-entered amount, tolerating currency symbols and thousands
    separators ('$2,501.99', '2,501.99', '2501.99'). None if unreadable."""
    if value is None:
        return None
    cleaned = _MONEY_STRIP.sub('', str(value).strip())
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


# -----------------------------------------------------------------------------
# Date age helpers — drive the amber/red colouring in the list and detail views
# -----------------------------------------------------------------------------

_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def parse_iso_date(value) -> datetime.date | None:
    """A stored YYYY-MM-DD date as a date, or None. Accepts a full ISO
    datetime by reading its date half.

    The one place the app decides whether a stored date is a date. Public
    because three modules used to answer this separately and did not agree:
    services/csv_export guarded fromisoformat with this regex, while this module
    and services/accounts called it bare. Bare, it accepts spellings nobody
    writes and no other layer recognises — datetime.date.fromisoformat reads
    ISO week dates and the compact form, so '2024-W01-1' came back as
    2024-01-01 and '20240115' as 2024-01-15. A .vault import dropped such a
    row; the same string handed to add_balance_entry was stored under a date
    the user never wrote, in the one table where that cannot be undone.

    The regex is the whole point: only the spelling the app itself writes is
    admitted, and everything else is None rather than a guess."""
    text = str(value).strip()[:10] if value is not None else ''
    if not _ISO_DATE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def updated_age_level(value, category) -> str:
    """How stale a last-updated stamp is: 'danger', 'warn', or ''.

    The thresholds come from the category rather than being one number for the
    whole vault, because how long a figure stays believable is a property of
    what it measures. A credit-card balance is a month old at best; annual pay
    is current until the next raise. A category that declares no interval never
    goes stale at all.

    category is required rather than defaulting to None. With the thresholds
    absent meaning "never stale", a caller that forgot to pass one would switch
    the check off silently for every account it touched — a missing argument
    should break the call, not quietly empty the Needs Attention tab."""
    thresholds = stale_after(category)
    if thresholds is None:
        return ''
    date = parse_iso_date(value)
    if date is None:
        return ''
    warn, danger = thresholds
    days = (datetime.date.today() - date).days
    if days >= danger:
        return 'danger'
    if days >= warn:
        return 'warn'
    return ''


def cycle_of(value) -> str | None:
    """The canonical cycle a stored value names, or None for a one-off.

    None is the answer for 'One-off', for a blank, and for anything this module
    does not recognise. All three mean the same thing to every caller — the
    date on file is the only occurrence there is — and answering them alike is
    what keeps a vault written before a cycle existed, or carrying a spelling
    from somewhere else, behaving as it always did rather than failing."""
    return _CYCLE_ALIASES.get(str(value or '').strip().lower())


def is_recurring(cycle) -> bool:
    return cycle_of(cycle) is not None


def _add_months(date: datetime.date, count: int) -> datetime.date:
    """A date `count` calendar months on, clamping a day the target month has
    not got — Jan 31 + 1 lands on Feb 28/29."""
    index = date.month - 1 + count
    year = date.year + index // 12
    month = index % 12 + 1
    return datetime.date(year, month, min(date.day, _days_in_month(year, month)))


def next_due_date(value, cycle) -> str | None:
    """The occurrence of a due date that has not gone by yet.

    For a one-off this is the stored date itself. For a recurring one the
    stored date is an anchor — what recurs is its position, the weekday for a
    weekly cycle and the day of the month for the rest — and the answer is the
    first occurrence on or after today.

    Every occurrence is rebuilt from the anchor rather than stepped forward
    from the last one, because the month-based clamping is one-way: a date due
    on the 31st, walked through February a month at a time, comes back as the
    28th and stays there for the rest of the account's life. Counting the
    whole periods elapsed and adding them to the anchor in one go means
    February borrows the 28th for one occurrence and hands the 31st back."""
    date = parse_iso_date(value)
    if date is None:
        return None

    step = _CYCLE_STEPS.get(cycle_of(cycle))
    if step is None:
        return date.isoformat()

    today = datetime.date.today()
    if date >= today:
        return date.isoformat()

    unit, size = step
    if unit == 'day':
        # Rounded up, so an anchor exactly a whole number of periods behind
        # today lands on today rather than a period before it.
        periods = -(-(today - date).days // size)
        return (date + datetime.timedelta(days=periods * size)).isoformat()

    # Whole periods between the anchor's month and this one. The occurrence it
    # gives may still be earlier in the month than today, in which case the
    # next one is the answer — and that one is always ahead, because it falls
    # in a later month than today by construction. There is no third case.
    elapsed = (today.year - date.year) * 12 + (today.month - date.month)
    periods = elapsed // size
    candidate = _add_months(date, periods * size)
    if candidate < today:
        candidate = _add_months(date, (periods + 1) * size)
    return candidate.isoformat()


def advance_cycle(value, cycle) -> str:
    """One period past the due date on file, for the "next one" button.

    Anchored on the occurrence currently showing rather than on the stored
    date, so pressing it once moves the account on by exactly one period from
    what the user is looking at. A one-off has no period, so it advances by a
    month — the button is the same button, and a month is what it did before
    any other cycle existed.

    From a blank field this means one period from today, not one period past
    whatever sentinel stood in for no date at all."""
    base = parse_iso_date(next_due_date(value, cycle)) or datetime.date.today()
    unit, size = _CYCLE_STEPS.get(cycle_of(cycle), ('month', 1))
    if unit == 'day':
        return (base + datetime.timedelta(days=size)).isoformat()
    return _add_months(base, size).isoformat()


def cycle_noun(cycle) -> str:
    """What one period of a cycle is called, for the button that adds one."""
    return {WEEKLY: 'week', MONTHLY: 'month',
            QUARTERLY: 'quarter', ANNUAL: 'year'}.get(cycle_of(cycle), 'month')


# How far a one-off due date can fall behind before it stops being a bill and
# starts being a record nobody has kept up. Past this it is still worth
# listing — the date on file is wrong — but it is not this week's problem, and
# painting it the same red as a payment actually due on Tuesday is what taught
# people to stop reading the colour at all.
LAPSED_AFTER_DAYS = 60


def due_date_level(value, cycle) -> str:
    """'danger' within 7 days of the next due date (or past it), 'warn' within
    14, 'lapsed' once a one-off is more than LAPSED_AFTER_DAYS overdue.

    A recurring date can never be 'lapsed': next_due_date always answers with a
    day that is still ahead, which is the whole point of recording the cycle."""
    date = parse_iso_date(next_due_date(value, cycle))
    if date is None:
        return ''
    days = (date - datetime.date.today()).days
    if days < -LAPSED_AFTER_DAYS:
        return 'lapsed'
    if days <= 7:
        return 'danger'
    if days <= 14:
        return 'warn'
    return ''


# -----------------------------------------------------------------------------
# Attention
#
# due_date_level and updated_age_level each answer for one field of one record.
# Nothing aggregated them, so finding what actually needed doing meant opening
# every category tab and reading the colours. These roll the two into a single
# per-account verdict, which the Needs Attention tab filters on.
# -----------------------------------------------------------------------------

# Ordered least to most urgent, so max() over the pair below picks the winner.
#
# 'lapsed' sits above nothing and below everything. It is not a milder version
# of 'warn' — it says the date on file was never rolled forward, which is a
# broken record rather than an approaching payment — but the rank has to place
# it somewhere, and placing it last is what stops a years-old due date from
# outranking a balance that has genuinely gone stale this month.
_LEVEL_RANK = {'': 0, 'lapsed': 1, 'warn': 2, 'danger': 3}


def is_favorite(account: dict) -> bool:
    """Whether an account is pinned.

    A predicate rather than a bare truth test on the key, because what is
    stored has been through JSON and, for an imported record, through a CSV
    cell: True, 1 and '1' all have to read the same, and the string '0' must
    not read as pinned merely by being a non-empty string."""
    value = account.get(FAVORITE_KEY)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    return bool(value)


def account_category(account: dict) -> str:
    """The category an account dict belongs to, however it reached us.

    Views flatten a normalized '_category' onto the row; raw service records
    carry the stored 'category', which may still be a pre-rename slug."""
    return normalize_category(
        account.get('_category') or account.get('category') or ''
    )


def attention_signals(account: dict) -> tuple[str, str]:
    """The account's (due, stale) levels, both '' for a closed account.

    The two are answered together because every caller needs both and computing
    them costs a date parse each: attention_counts used to call this pair once
    through attention_level and then a second time to say *why*, parsing four
    dates per account on a sweep of the whole vault."""
    if account.get('closed'):
        return '', ''
    return (
        due_date_level(account.get('due_date'), account.get(DUE_CYCLE_KEY)),
        updated_age_level(account.get('last_updated'), account_category(account)),
    )


def attention_level(account: dict) -> str:
    """How urgently an account needs looking at: 'danger', 'warn', 'lapsed', ''.

    The worse of its two signals — a due date coming up (or gone by) and a
    last-updated stamp going stale. Closed accounts never qualify: they are
    settled by definition, and a closed card with an old due date is history
    rather than a task."""
    return max(attention_signals(account),
               key=lambda level: _LEVEL_RANK.get(level, 0))


def needs_attention(account: dict) -> bool:
    return attention_level(account) != ''


def attention_counts(accounts) -> dict:
    """Counts of what needs attention and why, for the tab's tooltip.

    'due', 'stale' and 'lapsed' overlap — one account can be both due and
    stale — so they do not sum to 'total'. That is deliberate: the tooltip
    explains what is in the list, and an account with two problems is still one
    row. 'lapsed' is counted apart from 'due' rather than folded into it,
    because the two ask for different things: one wants a payment, the other
    wants the record corrected."""
    counts = {'total': 0, 'due': 0, 'stale': 0, 'lapsed': 0, 'urgent': 0}
    for account in accounts:
        due, stale = attention_signals(account)
        level = max((due, stale), key=lambda l: _LEVEL_RANK.get(l, 0))
        if not level:
            continue
        counts['total'] += 1
        if level == 'danger':
            counts['urgent'] += 1
        if due == 'lapsed':
            counts['lapsed'] += 1
        elif due:
            counts['due'] += 1
        if stale:
            counts['stale'] += 1
    return counts


def add_one_month(value) -> str:
    """Adds a calendar month to a YYYY-MM-DD string (or today if blank), clamping
    overflow so Jan 31 + 1 month lands on Feb 28/29 rather than spilling into March.

    The clamping rule itself lives in _add_months, which next_due_date and
    advance_cycle also step by — three copies of "which day does the target
    month actually have" is three places for the answer to drift."""
    return _add_months(parse_iso_date(value) or datetime.date.today(), 1).isoformat()


def _days_in_month(year: int, month: int) -> int:
    next_month = datetime.date(year + (month == 12), (month % 12) + 1, 1)
    return (next_month - datetime.timedelta(days=1)).day

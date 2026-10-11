# -----------------------------------------------------------------------------
# core/categories.py
# Per-category field schemas and the value formatting helpers built on them.
#
# Each category maps to:
#   title        — label shown in the left panel
#   item_label   — singular noun used in headings and empty states
#   name_field   — field used for the list title
#   value_noun   — what current_balance is called here (default 'Balance')
#   stale_after  — default (warn, danger) day thresholds for the last-updated
#                  stamp. Absent means the category does not go stale unless the
#                  user turns it on (see "Warning settings" below).
#   fields       — fields rendered in the detail view and edit form
#
# Field flags:
#   sensitive    — masked by default with a show/hide toggle
#   copy         — gets a copy button. On a sensitive field it copies the real
#                  value while it is still masked
#   link         — rendered as a clickable link
#   hide_in_form — shown in the detail view but not editable directly
#   default      — value a *new* record starts on. Select fields only, and never
#                  applied to an existing one
# -----------------------------------------------------------------------------

import datetime
import math
import re

# -----------------------------------------------------------------------------
# Field schemas
# -----------------------------------------------------------------------------

# Whether the user has pinned this account. Stored in extra_data rather than a
# plaintext column, which would reveal which accounts the owner reaches for
# most. No category declares it as a field; it reaches a record only through
# services.accounts.set_favorite. Absent reads as not pinned.
FAVORITE_KEY = 'favorite'

# Due-date recurrence, stored in extra_data beside the date it governs. Absent
# reads as ONE_OFF, so records written before cycles existed behave as before.
DUE_CYCLE_KEY = 'due_cycle'
ONE_OFF = 'One-off'
WEEKLY = 'Weekly'
MONTHLY = 'Monthly'
QUARTERLY = 'Quarterly'
ANNUAL = 'Annually'

# The options every category with a due date offers, shortest period first. The
# stored value is this string.
DUE_CYCLES = [ONE_OFF, WEEKLY, MONTHLY, QUARTERLY, ANNUAL]

# How far one period of each cycle reaches, as (unit, size). Months rather than
# day counts for the last three, so a cycle lands on the same day of the month.
_CYCLE_STEPS = {
    WEEKLY:    ('day', 7),
    MONTHLY:   ('month', 1),
    QUARTERLY: ('month', 3),
    ANNUAL:    ('month', 12),
}

# Stored spellings that mean one of the cycles above, lowercased. The canonical
# names are included; the rest are spellings a user or an import might carry in.
# Read on the way in, never written.
_CYCLE_ALIASES = {
    **{name.lower(): name for name in _CYCLE_STEPS},
    'annual': ANNUAL, 'yearly': ANNUAL, 'year': ANNUAL,
    'quarter': QUARTERLY, 'every 3 months': QUARTERLY,
    'month': MONTHLY, 'every month': MONTHLY,
    'week': WEEKLY, 'every week': WEEKLY,
}

# How often an investment account's contribution_amount goes in, and how many
# times a year that is. The stored value is the option string. Read exactly, with
# no aliases: the edit form shows anything else as blank, and the dashboard must
# not count a period the form would show as unset.
CONTRIBUTION_PERIOD_KEY = 'contribution_period'
CONTRIBUTIONS_PER_YEAR = {
    'Weekly':        52,
    'Every 2 Weeks': 26,
    'Monthly':       12,
    'Yearly':        1,
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
        # A fixed payment against a known schedule moves the way it said it
        # would, so it is not news monthly.
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
        # A quarter is the honest interval to re-read a portfolio on.
        'stale_after': (60, 90),
        'fields': [
            {'key': 'name',                'label': 'Account Name',        'type': 'text',   'required': True},
            {'key': 'institution',         'label': 'Institution',         'type': 'text'},
            {'key': 'account_number',      'label': 'Account Number',      'type': 'text',   'sensitive': True, 'copy': True},
            {'key': 'account_type',        'label': 'Account Type',        'type': 'select',
             'options': ['401k', 'IRA', 'Roth IRA', 'HSA', 'Brokerage', 'Other']},
            {'key': 'current_balance',     'label': 'Current Balance',     'type': 'number', 'money': True},
            {'key': 'contribution_amount', 'label': 'Contribution Amount', 'type': 'number', 'money': True},
            # No default: a period nobody picked would be counted on the
            # dashboard as if someone had.
            {'key': CONTRIBUTION_PERIOD_KEY, 'label': 'Contribution Period', 'type': 'select',
             'options': list(CONTRIBUTIONS_PER_YEAR)},
            {'key': 'username',           'label': 'Username',            'type': 'text',   'copy': True},
            {'key': 'password',            'label': 'Password',            'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',             'label': 'Website',             'type': 'url',    'link': True, 'copy': True},
            {'key': 'last_updated',        'label': 'Last Updated',        'type': 'date',   'hide_in_form': True},
            {'key': 'notes',               'label': 'Notes',               'type': 'textarea'},
        ],
    },

    # What a home or a car is worth, so net worth counts the asset a mortgage
    # or an auto loan is against. Without it, Loans subtracted the debt and
    # nothing added the house back.
    'Property': {
        'title': 'Property',
        'item_label': 'Property',
        'name_field': 'name',
        'value_noun': 'Estimated Value',
        # Nobody revalues a house monthly; once a year keeps it honest.
        'stale_after': (180, 365),
        'fields': [
            {'key': 'name',            'label': 'Name',            'type': 'text',     'required': True},
            {'key': 'property_type',   'label': 'Property Type',   'type': 'select',
             'options': ['Home', 'Vehicle', 'Land', 'Other']},
            {'key': 'current_balance', 'label': 'Estimated Value', 'type': 'number',   'money': True},
            {'key': 'username',        'label': 'Username',        'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',        'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',         'type': 'url',      'link': True, 'copy': True},
            {'key': 'last_updated',    'label': 'Last Updated',    'type': 'date',     'hide_in_form': True},
            {'key': 'notes',           'label': 'Notes',           'type': 'textarea'},
        ],
    },

    # current_balance holds the yearly figure — the history it drives is a record
    # of the pay rate over time, not of income received.
    'Income': {
        'title': 'Income',
        'item_label': 'Income Source',
        'name_field': 'name',
        'value_noun': 'Annual Pay',
        # Annual pay changes about once a year.
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

    # No stale_after here, on Secure Notes, or on GENERIC_CONFIG: a password or a
    # note does not drift while nobody is looking.
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

    Secrets are left out, not merely hidden from the results: a search that
    matches a secret is an oracle for it — type a password into the box and the
    one account still listed is the account that uses it. Free text (notes) is
    left out for the same reason, because notes are where people keep PINs,
    recovery codes and security answers.

    Only the category's own schema is searched. extra_data keeps whatever key
    space a record was written with, so a card moved to another category can
    still carry its old card_number with nothing marking it sensitive."""
    return [
        field for field in get_config(category)['fields']
        if not field.get('sensitive') and field['type'] not in ('password', 'textarea')
    ]


def stale_after(category: str | None) -> tuple[int, int] | None:
    """The (warn, danger) day thresholds for a category's last-updated stamp,
    or None where staleness is switched off for it.

    Off by default for a category declaring no interval: staleness claims the
    record has probably drifted, which only a value that moves on its own can."""
    entry = category_warnings(category)
    if not entry['stale']:
        return None
    return entry['stale_warn'], entry['stale_danger']


def value_noun(category: str | None) -> str:
    """What this category calls the amount current_balance tracks, so Income's
    history reads 'Annual Pay' rather than 'Balance'."""
    return get_config(category).get('value_noun', DEFAULT_VALUE_NOUN)


# -----------------------------------------------------------------------------
# Value formatting
# -----------------------------------------------------------------------------

def format_money(value) -> str:
    """Formats a number as USD. Returns the input as text if it isn't numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '' if value is None else str(value)
    return f"-${abs(number):,.2f}" if number < 0 else f"${number:,.2f}"


def format_money_compact(value) -> str:
    """Money abbreviated to fit a chart axis: $1.2k, $3.4M, $950."""
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

    The one definition of how a whole figure is spelled, shared by the detail
    view, history_io.canonical_amount and accounts._sync_current_balance, so a
    figure that round-trips through an export comes back spelled the same way.
    Infinity and NaN have no whole form and are returned unchanged."""
    if not math.isfinite(number):
        return number
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
    """A stored YYYY-MM-DD date as a date, or None. Accepts a full ISO datetime
    by reading its date half.

    The one place the app decides whether a stored date is a date. The regex
    matters: bare fromisoformat also reads ISO week dates and the compact form,
    so '2024-W01-1' would come back as an unrelated day. Only the spelling the
    app itself writes is admitted."""
    text = str(value).strip()[:10] if value is not None else ''
    if not _ISO_DATE.match(text):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def updated_age_level(value, category) -> str:
    """How stale a last-updated stamp is: 'danger', 'warn', or ''.

    category is required: with absent thresholds meaning "never stale", a
    forgotten argument would silently switch the check off."""
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

    'One-off', a blank and an unrecognised spelling all answer None: in each
    case the date on file is the only occurrence there is."""
    return _CYCLE_ALIASES.get(str(value or '').strip().lower())


def _add_months(date: datetime.date, count: int) -> datetime.date:
    """A date `count` calendar months on, clamping a day the target month has
    not got — Jan 31 + 1 lands on Feb 28/29."""
    index = date.month - 1 + count
    year = date.year + index // 12
    month = index % 12 + 1
    return datetime.date(year, month, min(date.day, _days_in_month(year, month)))


def next_due_date(value, cycle) -> str | None:
    """The occurrence of a due date that has not gone by yet.

    For a one-off this is the stored date. For a recurring one the stored date
    is an anchor, and the answer is the first occurrence on or after today.

    Every occurrence is rebuilt from the anchor rather than stepped from the
    last one, because month clamping is one-way: a date on the 31st walked
    through February would come back as the 28th and stay there."""
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
        # Rounded up, so an anchor a whole number of periods behind lands on today.
        periods = -(-(today - date).days // size)
        return (date + datetime.timedelta(days=periods * size)).isoformat()

    # Whole periods between the anchor's month and this one. If that occurrence
    # is still earlier in the month than today, the next one is always ahead.
    elapsed = (today.year - date.year) * 12 + (today.month - date.month)
    periods = elapsed // size
    candidate = _add_months(date, periods * size)
    if candidate < today:
        candidate = _add_months(date, (periods + 1) * size)
    return candidate.isoformat()


def advance_cycle(value, cycle) -> str:
    """One period past the due date showing, for the "next one" button.

    A one-off advances by a month. From a blank field it means one period from
    today."""
    base = parse_iso_date(next_due_date(value, cycle)) or datetime.date.today()
    unit, size = _CYCLE_STEPS.get(cycle_of(cycle), ('month', 1))
    if unit == 'day':
        return (base + datetime.timedelta(days=size)).isoformat()
    return _add_months(base, size).isoformat()


def cycle_noun(cycle) -> str:
    """What one period of a cycle is called, for the button that adds one."""
    return {WEEKLY: 'week', MONTHLY: 'month',
            QUARTERLY: 'quarter', ANNUAL: 'year'}.get(cycle_of(cycle), 'month')


# -----------------------------------------------------------------------------
# Warning settings
#
# When due dates and last-updated stamps turn orange ('warn') and red
# ('danger'), and which categories get either check. The constants and each
# category's stale_after are the defaults; the user's overrides arrive through
# set_warning_settings at unlock and are dropped at lock.
#
# Held here in memory because these levels are computed for every row on every
# render, and this module stays free of the database.
#
# Shape, as stored and as set:
#   {'due_warn': 14, 'due_danger': 7, 'lapsed_after': 60,
#    'categories': {name: {'due': bool, 'stale': bool,
#                          'stale_warn': int, 'stale_danger': int}}}
# A category missing from 'categories' runs on its defaults.
# -----------------------------------------------------------------------------

# Days before the next due date that it turns orange, and red.
DUE_WARN_DAYS = 14
DUE_DANGER_DAYS = 7

# How far a one-off due date can fall behind before it stops being a bill and
# becomes a record nobody rolled forward. Still listed, but not painted the same
# red as a payment due on Tuesday.
LAPSED_AFTER_DAYS = 60

# What a category with no stale_after starts on when the user switches
# staleness on for it.
DEFAULT_STALE_AFTER = (21, 28)

# Every day count the user can set has to fall inside this.
WARNING_DAYS_MIN = 1
WARNING_DAYS_MAX = 730

_warning_settings: dict | None = None


def warning_defaults(category: str | None) -> dict:
    """A category's built-in warning entry.

    'due' defaults on everywhere, including categories with no due-date field:
    a record moved out of Credit Cards keeps its due_date in extra_data."""
    thresholds = get_config(category).get('stale_after')
    warn, danger = thresholds or DEFAULT_STALE_AFTER
    return {'due': True, 'stale': bool(thresholds),
            'stale_warn': warn, 'stale_danger': danger}


def category_warnings(category: str | None) -> dict:
    """The warning entry in force for a category: the user's, else the default."""
    key = normalize_category(category or '')
    if _warning_settings is not None:
        entry = _warning_settings['categories'].get(key)
        if entry is not None:
            return entry
    return warning_defaults(key)


def due_thresholds() -> tuple[int, int]:
    """(warn, danger): days before a due date that it turns orange, and red."""
    if _warning_settings is None:
        return DUE_WARN_DAYS, DUE_DANGER_DAYS
    return _warning_settings['due_warn'], _warning_settings['due_danger']


def lapsed_after_days() -> int:
    if _warning_settings is None:
        return LAPSED_AFTER_DAYS
    return _warning_settings['lapsed_after']


def warning_settings(categories) -> dict:
    """The settings in force, in the stored shape, with an entry for every one
    of `categories` — what the Warnings dialog opens on."""
    warn, danger = due_thresholds()
    return {
        'due_warn': warn, 'due_danger': danger,
        'lapsed_after': lapsed_after_days(),
        'categories': {normalize_category(name): dict(category_warnings(name))
                       for name in categories},
    }


def default_warning_settings(categories) -> dict:
    """The built-in settings, in the same shape, for the dialog's Reset."""
    return {
        'due_warn': DUE_WARN_DAYS, 'due_danger': DUE_DANGER_DAYS,
        'lapsed_after': LAPSED_AFTER_DAYS,
        'categories': {normalize_category(name): warning_defaults(name)
                       for name in categories},
    }


def _days(value, label: str) -> int:
    # bool is an int to Python, and True is not a day count anyone typed.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole number of days.")
    if not WARNING_DAYS_MIN <= value <= WARNING_DAYS_MAX:
        raise ValueError(f"{label} must be between {WARNING_DAYS_MIN} and "
                         f"{WARNING_DAYS_MAX} days.")
    return value


def validate_warning_settings(raw) -> dict:
    """A clean copy of raw in the stored shape, or ValueError naming the problem.

    Red has to be stricter than orange, or orange would never show. A category
    entry missing a key takes it from that category's defaults, and unknown
    keys are dropped."""
    if not isinstance(raw, dict):
        raise ValueError("Warning settings are not in the expected format.")

    due_warn = _days(raw.get('due_warn'), 'Due-date orange')
    due_danger = _days(raw.get('due_danger'), 'Due-date red')
    if due_danger >= due_warn:
        raise ValueError("Due dates must turn red closer to the date than they "
                         "turn orange.")
    clean = {'due_warn': due_warn, 'due_danger': due_danger,
             'lapsed_after': _days(raw.get('lapsed_after'), 'Lapsed after'),
             'categories': {}}

    entries = raw.get('categories') or {}
    if not isinstance(entries, dict):
        raise ValueError("Warning settings are not in the expected format.")
    for name, entry in entries.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            raise ValueError("Warning settings are not in the expected format.")
        key = normalize_category(name.strip())
        merged = {**warning_defaults(key), **entry}
        if not isinstance(merged['due'], bool) or not isinstance(merged['stale'], bool):
            raise ValueError(f"{key}: the on/off switches must be on or off.")
        warn = _days(merged['stale_warn'], f"{key} orange")
        danger = _days(merged['stale_danger'], f"{key} red")
        if danger <= warn:
            raise ValueError(f"{key}: red must come later than orange.")
        clean['categories'][key] = {'due': merged['due'], 'stale': merged['stale'],
                                    'stale_warn': warn, 'stale_danger': danger}
    return clean


def set_warning_settings(settings: dict | None):
    """Puts the user's warning settings in force, or the defaults for None.
    Validated here too: a malformed dict would fail on every row rendered."""
    global _warning_settings
    _warning_settings = None if settings is None else validate_warning_settings(settings)


def due_date_level(value, cycle, category) -> str:
    """'danger' within due_danger days of the next due date (or past it), 'warn'
    within due_warn, 'lapsed' once a one-off is more than lapsed_after days
    overdue — '' throughout for a category with due-date warnings off.

    category is required, as for updated_age_level. A recurring date is never
    'lapsed': next_due_date always answers with a day still ahead."""
    if not category_warnings(category)['due']:
        return ''
    date = parse_iso_date(next_due_date(value, cycle))
    if date is None:
        return ''
    days = (date - datetime.date.today()).days
    if days < -lapsed_after_days():
        return 'lapsed'
    warn, danger = due_thresholds()
    if days <= danger:
        return 'danger'
    if days <= warn:
        return 'warn'
    return ''


# -----------------------------------------------------------------------------
# Attention
#
# Rolls due_date_level and updated_age_level into a single per-account verdict,
# which the Needs Attention tab filters on.
# -----------------------------------------------------------------------------

# Ordered least to most urgent, so max() over a pair picks the winner. 'lapsed'
# ranks lowest: it is a broken record rather than an approaching payment, and
# must not outrank a balance that has genuinely gone stale this month.
_LEVEL_RANK = {'': 0, 'lapsed': 1, 'warn': 2, 'danger': 3}


def _rank(level: str) -> int:
    return _LEVEL_RANK.get(level, 0)


def is_favorite(account: dict) -> bool:
    """Whether an account is pinned. True, 1 and '1' all read the same, and the
    string '0' must not read as pinned merely by being non-empty."""
    value = account.get(FAVORITE_KEY)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'on')
    return bool(value)


def account_category(account: dict) -> str:
    """The category an account dict belongs to, however it reached us. Views
    flatten a normalized '_category' onto the row; raw service records carry
    the stored 'category', which may still be a pre-rename slug."""
    return normalize_category(
        account.get('_category') or account.get('category') or ''
    )


def attention_signals(account: dict) -> tuple[str, str]:
    """The account's (due, stale) levels, both '' for a closed account."""
    if account.get('closed'):
        return '', ''
    category = account_category(account)
    return (
        due_date_level(account.get('due_date'), account.get(DUE_CYCLE_KEY), category),
        updated_age_level(account.get('last_updated'), category),
    )


def attention_level(account: dict) -> str:
    """How urgently an account needs looking at: 'danger', 'warn', 'lapsed', ''.
    The worse of its two signals. Closed accounts never qualify."""
    return max(attention_signals(account), key=_rank)


def needs_attention(account: dict) -> bool:
    return attention_level(account) != ''


def attention_counts(accounts) -> dict:
    """Counts of what needs attention and why, for the tab's tooltip.

    'due', 'stale' and 'lapsed' overlap — one account can be both due and stale
    — so they do not sum to 'total'."""
    counts = {'total': 0, 'due': 0, 'stale': 0, 'lapsed': 0, 'urgent': 0}
    for account in accounts:
        due, stale = attention_signals(account)
        level = max((due, stale), key=_rank)
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


def _days_in_month(year: int, month: int) -> int:
    next_month = datetime.date(year + (month == 12), (month % 12) + 1, 1)
    return (next_month - datetime.timedelta(days=1)).day

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
#   last4        — on a sensitive field, the masked value still shows its last
#                  four characters ('•••• 4821'), so two cards can be told apart
#   no_search    — kept out of search without being masked: a security question
#                  is not a secret, but it sits beside one
#   hide_in_form — shown in the detail view but not editable directly
#   default      — value a *new* record starts on. Select fields only, and never
#                  applied to an existing one
#   shown_when   — (key, values): the field applies only while the field named
#                  by key is itself shown and holds one of values. Hidden in the
#                  form and the detail view otherwise, and cleared on save
#   hidden_when  — (key, values): the reverse. The field applies unless the
#                  field named by key holds one of values, so a value nobody
#                  listed (blank, or an import's own spelling) keeps it
#
# Select options may be reordered or added to, never renamed or removed: the
# edit form shows a stored value it has no option for as blank, and saving the
# account then writes that blank over it.
# -----------------------------------------------------------------------------

import datetime
import math
import re
from decimal import Decimal

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

# Whether a retirement account's money is taxed when withdrawn (Traditional),
# not at all (Roth), or some of each (Both). No default, for the reason the
# contribution period has none: a treatment nobody picked would be counted as
# if someone had.
TAX_TREATMENT_KEY = 'tax_treatment'
TRADITIONAL = 'Traditional'
ROTH = 'Roth'
BOTH = 'Both'
TAX_TREATMENTS = [TRADITIONAL, ROTH, BOTH]

# The account types a tax treatment is asked for. The rest decide it
# themselves: a Roth IRA is Roth, an HSA an HSA, and a brokerage account taxable.
TAX_TREATMENT_TYPES = ('401k', '403b', '457b', 'IRA', 'SEP IRA', 'SIMPLE IRA')

# On a Both account, each of these totals is the sum of its (Traditional, Roth)
# parts, entered in dollars the way a statement or pay stub shows them. The
# total is what the rest of the app reads; only the retirement planner reads
# the parts.
SPLIT_FIELDS = {
    'current_balance':     ('traditional_balance', 'roth_balance'),
    'contribution_amount': ('traditional_contribution', 'roth_contribution'),
}

_BOTH_ONLY = (TAX_TREATMENT_KEY, (BOTH,))

# Checkbox fields. A ticked one is stored as 'Yes', as Auto Pay is, so an
# export carries it as text and its re-import reads it back; unticked, it is
# blank. is_checked is the one test of whether a value is ticked.
CHECKED = 'Yes'

# Ticked, an account is left out of every figure on the dashboard (balances,
# net worth, trends, the investing rate), or out of the retirement planner:
# a child's 529, or money looked after for someone else. Each is independent
# of the other, and neither hides the account anywhere else.
EXCLUDE_DASHBOARD_KEY = 'exclude_dashboard'
EXCLUDE_RETIREMENT_KEY = 'exclude_retirement'
_EXCLUDE_DASHBOARD = {'key': EXCLUDE_DASHBOARD_KEY, 'label': 'Exclude from Dashboard',
                      'type': 'checkbox', 'text': 'Leave out of every dashboard figure'}
_EXCLUDE_RETIREMENT = {'key': EXCLUDE_RETIREMENT_KEY, 'label': 'Exclude from Retirement',
                       'type': 'checkbox', 'text': "Not for retirement, like a child's 529"}


# What an Income record is. Blank reads as Pay, so records written before the
# field existed keep counting as they did. Social Security and a pension are
# kept as a monthly benefit rather than annual pay, belong to one of the
# household, and count as income on the dashboard only while they are being
# received. The retirement planner reads them for Your details.
INCOME_TYPE_KEY = 'income_type'
PAY = 'Pay'
SOCIAL_SECURITY = 'Social Security'
PENSION = 'Pension'
INCOME_TYPES = [PAY, SOCIAL_SECURITY, PENSION, 'Other']
BENEFIT_TYPES = (SOCIAL_SECURITY, PENSION)

MONTHLY_BENEFIT_KEY = 'monthly_benefit'
RECEIVING_KEY = 'receiving_now'
# Whose benefit it is, or whose Investments account. Blank reads as yours.
WHOSE_KEY = 'whose'
YOURS = 'You'
PARTNERS = 'Partner'

_BENEFIT_ONLY = (INCOME_TYPE_KEY, BENEFIT_TYPES)


def is_checked(value) -> bool:
    """Whether a checkbox field's stored value is ticked. True counts as well,
    for a record written by hand or by another tool."""
    return value is True or (isinstance(value, str)
                             and value.strip().lower() == CHECKED.lower())


CATEGORY_CONFIG = {
    'Credit Cards': {
        'title': 'Credit Cards',
        'item_label': 'Credit Card',
        'name_field': 'name',
        # A statement a month, so three weeks quiet is worth a nudge.
        'stale_after': (21, 28),
        'fields': [
            {'key': 'name',            'label': 'Account Name',    'type': 'text',     'required': True},
            {'key': 'card_number',     'label': 'Card Number',     'type': 'text',     'sensitive': True, 'copy': True, 'last4': True},
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
            {**_EXCLUDE_DASHBOARD},
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
            {'key': 'account_number',  'label': 'Account Number',  'type': 'text',     'sensitive': True, 'copy': True, 'last4': True},
            {'key': 'routing_number',  'label': 'Routing Number',  'type': 'text',     'sensitive': True, 'copy': True, 'last4': True},
            {'key': 'account_type',    'label': 'Account Type',    'type': 'select',
             'options': ['Checking', 'Savings', 'Money Market', 'Other']},
            {'key': 'current_balance', 'label': 'Current Balance', 'type': 'number',   'money': True},
            {'key': 'username',        'label': 'Username',        'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',        'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',         'type': 'url',      'link': True, 'copy': True},
            {**_EXCLUDE_DASHBOARD},
            {**_EXCLUDE_RETIREMENT},
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
            {'key': 'account_number',  'label': 'Account Number', 'type': 'text',     'sensitive': True, 'copy': True, 'last4': True},
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
            {**_EXCLUDE_DASHBOARD},
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
            {'key': 'account_number',      'label': 'Account Number',      'type': 'text',   'sensitive': True, 'copy': True, 'last4': True},
            {'key': 'account_type',        'label': 'Account Type',        'type': 'select',
             'options': ['401k', '403b', '457b', 'IRA', 'Roth IRA', 'SEP IRA',
                         'SIMPLE IRA', 'HSA', 'Brokerage', 'Other']},
            {'key': TAX_TREATMENT_KEY,     'label': 'Tax Treatment',       'type': 'select',
             'options': TAX_TREATMENTS,
             'shown_when': ('account_type', TAX_TREATMENT_TYPES)},
            # Ordered so the form's two-column grid pairs each total with
            # what sits beside it and each Traditional part with its Roth one.
            {'key': 'current_balance',     'label': 'Current Balance',     'type': 'number', 'money': True},
            {'key': 'traditional_balance', 'label': 'Traditional Balance', 'type': 'number', 'money': True,
             'shown_when': _BOTH_ONLY},
            {'key': 'roth_balance',        'label': 'Roth Balance',        'type': 'number', 'money': True,
             'shown_when': _BOTH_ONLY},
            {'key': 'contribution_amount', 'label': 'Contribution Amount', 'type': 'number', 'money': True},
            # No default: a period nobody picked would be counted on the
            # dashboard as if someone had.
            {'key': CONTRIBUTION_PERIOD_KEY, 'label': 'Contribution Period', 'type': 'select',
             'options': list(CONTRIBUTIONS_PER_YEAR)},
            {'key': 'traditional_contribution', 'label': 'Traditional Contribution',
             'type': 'number', 'money': True, 'shown_when': _BOTH_ONLY},
            {'key': 'roth_contribution',   'label': 'Roth Contribution',   'type': 'number', 'money': True,
             'shown_when': _BOTH_ONLY},
            # Whose account it is, so the retirement planner stops its
            # contributions when that person retires. Blank reads as yours.
            {'key': WHOSE_KEY,             'label': 'Whose',               'type': 'select',
             'options': [YOURS, PARTNERS], 'default': YOURS},
            {'key': 'username',           'label': 'Username',            'type': 'text',   'copy': True},
            {'key': 'password',            'label': 'Password',            'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',             'label': 'Website',             'type': 'url',    'link': True, 'copy': True},
            {**_EXCLUDE_DASHBOARD},
            {**_EXCLUDE_RETIREMENT},
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
            {**_EXCLUDE_DASHBOARD},
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
            {'key': INCOME_TYPE_KEY,   'label': 'Income Type',  'type': 'select',
             'options': INCOME_TYPES, 'default': PAY},
            {'key': 'current_balance', 'label': 'Annual Pay',   'type': 'number',   'money': True,
             'hidden_when': _BENEFIT_ONLY},
            # A Social Security benefit not yet received is the one at 67 from
            # the statement; once received, what is paid.
            {'key': MONTHLY_BENEFIT_KEY, 'label': 'Monthly Benefit', 'type': 'number', 'money': True,
             'shown_when': _BENEFIT_ONLY},
            {'key': WHOSE_KEY,         'label': 'Whose',        'type': 'select',
             'options': [YOURS, PARTNERS], 'default': YOURS, 'shown_when': _BENEFIT_ONLY},
            {'key': RECEIVING_KEY,     'label': 'Receiving Now', 'type': 'checkbox',
             'text': 'Paid now, so the dashboard counts it', 'shown_when': _BENEFIT_ONLY},
            {'key': 'username',        'label': 'Username',     'type': 'text',     'copy': True},
            {'key': 'password',        'label': 'Password',     'type': 'password', 'sensitive': True, 'copy': True},
            {'key': 'website',         'label': 'Website',      'type': 'url',      'link': True, 'copy': True},
            {**_EXCLUDE_DASHBOARD},
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
            {'key': 'account_number',  'label': 'Account Number', 'type': 'text',   'sensitive': True, 'copy': True, 'last4': True},
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
            {'key': 'security_question', 'label': 'Security Question', 'type': 'text',     'no_search': True},
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

# Every checkbox field any schema declares, for an import to read as yes or no.
CHECKBOX_KEYS = frozenset(
    field['key'] for config in [*CATEGORY_CONFIG.values(), GENERIC_CONFIG]
    for field in config['fields'] if field['type'] == 'checkbox'
)


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


def has_balance(category: str | None, account: dict) -> bool:
    """Whether this record keeps a balance: its category has the field and it
    applies to the record, which an Income record of Social Security's does
    not. Quick Update, the list row's figure and Balance History go by this."""
    field = schema_field(category, 'current_balance')
    return field is not None and field_shown(category, field, account)


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
        # A checkbox is left out too: searching "yes" would list every ticked one.
        if not field.get('sensitive') and not field.get('no_search')
        and field['type'] not in ('password', 'textarea', 'checkbox')
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


def schema_field(category: str | None, key: str) -> dict | None:
    """The category's definition of one field, or None if it has no such field.
    A pre-rename slug ('investments') finds the current category's."""
    fields = get_config(normalize_category(category))['fields']
    return next((f for f in fields if f['key'] == key), None)


def field_shown(category: str | None, field: dict, values: dict) -> bool:
    """Whether a field applies to a record holding `values`.

    A field without shown_when always does. One with it applies while the field
    it names is itself shown and holds one of the listed values, so changing a
    401(k) to Brokerage hides its tax treatment, and with it the two amounts
    that only a Both account has. One with hidden_when applies unless the field
    it names holds one of its values, so Social Security hides Annual Pay."""
    hidden = field.get('hidden_when')
    if hidden and values.get(hidden[0]) in hidden[1]:
        return False
    rule = field.get('shown_when')
    if not rule:
        return True
    key, allowed = rule
    controller = schema_field(category, key)
    if controller is not None and not field_shown(category, controller, values):
        return False
    return values.get(key) in allowed


def active_splits(category: str | None, values: dict) -> dict:
    """{total key: (Traditional key, Roth key)} for each SPLIT_FIELDS total
    that a record holding `values` works out from its parts: the category has
    the total and both parts, and the parts are shown."""
    result = {}
    for total, parts in SPLIT_FIELDS.items():
        if schema_field(category, total) is None:
            continue
        fields = [schema_field(category, key) for key in parts]
        if all(field is not None and field_shown(category, field, values)
               for field in fields):
            result[total] = parts
    return result


def add_amounts(*amounts) -> float:
    """The sum of some amounts, without the binary rounding that makes
    1000.10 + 2000.20 come to 3000.3000000000002. Whole sums come back whole,
    as trim_number spells them."""
    total = sum((Decimal(str(amount)) for amount in amounts), Decimal(0))
    return trim_number(float(total))


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


_MONTH_ABBR = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
               'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def format_date(value) -> str | None:
    """YYYY-MM-DD → 'Oct 24, 2026', which no reader can take two ways. Returns
    None if the value isn't a date."""
    date = parse_iso_date(value)
    if date is None:
        return None
    return f"{_MONTH_ABBR[date.month - 1]} {date.day}, {date.year}"


def format_short_date(value) -> str | None:
    """'Nov 16' for a date this year, 'Nov 16, 2027' otherwise: the list row's
    spelling, where the year is nearly always this one."""
    date = parse_iso_date(value)
    if date is None:
        return None
    if date.year == datetime.date.today().year:
        return f"{_MONTH_ABBR[date.month - 1]} {date.day}"
    return format_date(value)


def format_month(value) -> str:
    """YYYY-MM → "May '26". The month by name, so an axis label cannot be read
    as a day."""
    parts = str(value).split('-')
    try:
        month = int(parts[1])
    except (IndexError, ValueError):
        return str(value)
    if not 1 <= month <= 12:
        return str(value)
    return f"{_MONTH_ABBR[month - 1]} '{parts[0][2:]}"


def format_age(value) -> str | None:
    """How long ago a stored date was, in the largest whole unit: 'today',
    'yesterday', '5 days ago', '3 weeks ago', '4 months ago', '2 years ago'.
    None if the value isn't a date."""
    date = parse_iso_date(value)
    if date is None:
        return None
    days = (datetime.date.today() - date).days
    if days <= 0:
        # A date ahead of today is a clock that moved; say today rather than
        # 'in 3 days' for something that already happened.
        return 'today'
    if days == 1:
        return 'yesterday'
    for size, unit in ((365, 'year'), (30, 'month'), (7, 'week')):
        # Two of a unit before switching to it: '10 days ago' reads better
        # than '1 week ago' for a figure 10 days old.
        if days >= 2 * size or (unit == 'year' and days >= size):
            count = days // size
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return f"{days} days ago"


# The shortest value a masked field shows the end of. Below it, four
# characters would be most of the secret.
_LAST4_MIN = 8


def last_four(value) -> str | None:
    """The last four letters or digits of a card or account number, or None
    when the value is too short for them to stay a hint. Spaces and dashes
    are skipped, so '4111 1111 1111 1234' ends in '1234'."""
    characters = [c for c in str(value or '') if c.isalnum()]
    if len(characters) < _LAST4_MIN:
        return None
    return ''.join(characters[-4:])


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

    if field.get('type') == 'checkbox':
        return CHECKED if is_checked(value) else 'No'

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

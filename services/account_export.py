# -----------------------------------------------------------------------------
# services/account_export.py
# One account rendered as a plaintext CSV, at a scope the user picks.
#
# Three scopes, and the difference between them is not cosmetic. A history file
# holds dates and figures, which is worth little to anyone who finds it. The
# other two hold the account's password. Nothing here decides whether that is
# acceptable — the dialog asks, and asks in those terms — but everything here is
# arranged so the scope is the only thing that governs what reaches the file,
# rather than a caller assembling the parts and hoping it left the right ones
# out.
#
# The history section is rendered by services/history_io.py, the module that
# also reads it back, so an exported history still imports. This module only
# composes; it does not know the format of what it is composing.
# -----------------------------------------------------------------------------

import csv
import datetime
import io
import re

from categories import DEFAULT_VALUE_NOUN, account_category, get_config
from encryption import is_unreadable
from services import history_io
from services.export_io import INTERNAL_KEYS, defuse

SCOPE_ALL = 'all'
SCOPE_ACCOUNT = 'account'
SCOPE_HISTORY = 'history'

# Fields never written from the schema or the record's own keys.
#
# The floor — id, date_added, _category, _match — is export_io.INTERNAL_KEYS,
# shared with the whole-vault CSV export so that a new piece of bookkeeping
# cannot be excluded from one file and leak from the other.
#
# 'category' and 'closed' are added on top because they *are* written, just not
# from this loop: account_rows adds them by name, at the top and bottom of the file
# respectively, so listing them here is what stops a second copy appearing in
# the middle. Nothing in CATEGORY_CONFIG declares either as a field today, so
# the guard in account_rows is currently unreachable — it is kept because it is
# the enforcement point, and a schema gaining a 'date_added' field should not
# be what decides whether one gets exported.
_INTERNAL = INTERNAL_KEYS | {'category', 'closed'}

# Anything a filesystem would reject or read as a path. Applied to the account
# name before it goes in a filename — this is a suggestion the save dialog shows,
# not a path the app writes to blind, but a name holding a slash should not be
# able to propose a directory.
_UNSAFE_NAME = re.compile(r'[^\w \-.]+')


def scopes(noun: str = DEFAULT_VALUE_NOUN) -> list[dict]:
    """The three choices, worded for this category.

    Each carries `credentials`, which is the whole basis of the warning the
    dialog shows: two of these files can be read for a password and one cannot,
    and that is the distinction worth putting in front of someone before they
    write one to disk."""
    history = f'{noun} history'
    return [
        {'key': SCOPE_ALL, 'label': 'Everything', 'credentials': True,
         'detail': f'Account details and {history.lower()} in one file'},
        {'key': SCOPE_ACCOUNT, 'label': 'Account details only', 'credentials': True,
         'detail': 'Name, username, password, website and this category’s fields'},
        {'key': SCOPE_HISTORY, 'label': f'{history} only', 'credentials': False,
         'detail': 'Dates and figures, with nothing identifying the account'},
    ]


def includes_credentials(scope: str) -> bool:
    return scope in (SCOPE_ALL, SCOPE_ACCOUNT)


def _value(value) -> str:
    """One stored value as CSV text. Booleans read as words rather than as the
    0 and 1 they are stored as — this file is for a person to read."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'Yes' if value else 'No'
    return str(value)


def account_rows(account: dict) -> list[tuple[str, str]]:
    """The account as (label, value) pairs, in the order the detail view shows.

    Labelled from the category's own schema, so a credit card's field reads
    'Card Number' rather than 'card_number'. Anything the account carries that
    the schema does not name is written after them under its raw key: an
    imported record keeps whatever key space it was written with, and a scope
    that says 'everything' has to mean it."""
    category = account_category(account)
    fields = get_config(category)['fields']

    rows = [('Category', category or '')]
    named = set()
    for field in fields:
        key = field['key']
        named.add(key)
        if key in _INTERNAL:
            continue
        rows.append((field.get('label') or key, _value(account.get(key))))

    for key, value in account.items():
        if key in named or key in _INTERNAL or key.startswith('_'):
            continue
        rows.append((key, _value(value)))

    rows.append(('Closed', 'Yes' if account.get('closed') else 'No'))
    return rows


def _write(writer, rows):
    for row in rows:
        writer.writerow([defuse(cell) for cell in row])


def render(account: dict, entries=None, scope: str = SCOPE_ALL,
           noun: str = DEFAULT_VALUE_NOUN) -> dict:
    """The CSV text for one account at one scope.

    Returns {text, fields, entries, unreadable, unreadable_fields} — the counts
    are what the toast reports, so what was written is stated rather than
    assumed. 'fields' counts the account's own fields; 'unreadable' counts
    history entries left out, and 'unreadable_fields' account fields written as
    their placeholder.

    A single-section file carries no section heading, which is what keeps a
    history-only export exactly the document history_io.prepare expects to read
    back. Only the combined file needs headings, and there they earn their
    place by saying where one table stops and the next starts."""
    account = account or {}
    sections = []
    fields = 0
    unreadable_fields = 0
    history = {'text': '', 'written': 0, 'unreadable': 0}

    if scope in (SCOPE_ALL, SCOPE_ACCOUNT):
        rows = account_rows(account)
        # The account's own fields, not the rows written. 'Category' and
        # 'Closed' are added by name rather than being fields of the record, so
        # counting rows reported two more fields than the account has.
        fields = max(len(rows) - 2, 0)
        # Counted for the same reason history entries are: a field that came
        # back '⚠ unreadable' is in the file as that placeholder, and the toast
        # should say so rather than let the user believe the export is whole.
        unreadable_fields = sum(1 for _, value in rows if is_unreadable(value))
        buffer = io.StringIO()
        _write(csv.writer(buffer, lineterminator='\r\n'), [('Field', 'Value'), *rows])
        sections.append(('Account Details', buffer.getvalue()))

    if scope in (SCOPE_ALL, SCOPE_HISTORY):
        history = history_io.export_csv(entries, noun)
        sections.append((f'{noun} History', history['text']))

    if len(sections) == 1:
        text = sections[0][1]
    else:
        writer_buffer = io.StringIO()
        for index, (title, body) in enumerate(sections):
            if index:
                writer_buffer.write('\r\n')
            csv.writer(writer_buffer, lineterminator='\r\n').writerow([defuse(title)])
            writer_buffer.write(body)
        text = writer_buffer.getvalue()

    return {'text': text, 'fields': fields,
            'entries': history['written'],
            # 'unreadable' stays the history count the toast already reports;
            # the account-field count is separate so a caller can word the two
            # differently — a missing figure and a missing password are not the
            # same news.
            'unreadable': history['unreadable'],
            'unreadable_fields': unreadable_fields}


def default_name(account_name, scope: str = SCOPE_ALL,
                 noun: str = DEFAULT_VALUE_NOUN) -> str:
    """A filename saying which account, and which of the three files this is."""
    stem = _UNSAFE_NAME.sub('_', str(account_name or '')).strip('_. ') or 'account'
    if scope == SCOPE_ACCOUNT:
        suffix = 'details'
    elif scope == SCOPE_HISTORY:
        suffix = f"{noun.lower().replace(' ', '-')}-history"
    else:
        suffix = 'export'
    return f'{stem[:60]}-{suffix}-{datetime.date.today()}.csv'

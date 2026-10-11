# -----------------------------------------------------------------------------
# services/account_import.py
# A table of accounts read into records ready for the vault.
#
# Reads this app's own CSV export, another password manager's, or a
# spreadsheet. Nothing here touches the database: it works out which column is
# which and returns the rows worth keeping plus a reason for every one it
# dropped. The caller shows both and only then writes — the same contract
# services/history_io holds.
#
# Unlike history, where a date looks like a date, an account is a dozen columns
# of free text, and nothing about 'jsmith' says whether it is a username or a
# security answer. So the header row is required, and a column it cannot name
# is dropped and reported rather than guessed at — a wrong guess here files a
# password under 'notes'.
#
# A row with no category is filed under the category the user picks in the
# dialog.
# -----------------------------------------------------------------------------

import csv
import re
from collections import Counter

from core.categories import (
    CATEGORY_CONFIG, FAVORITE_KEY, GENERIC_CONFIG, normalize_category,
)
from core.encryption import is_unreadable
from services import export_io
from services.accounts import MAX_LENGTHS, is_reserved_category
from services.export_io import INTERNAL_KEYS, undefuse

# Accounts accepted from a single import — well past any real password
# manager's export, and low enough that the wrong file cannot flood a vault.
MAX_ROWS = 2000

# An account is a few hundred bytes of text, so anything past this is the
# wrong file.
MAX_FILE_BYTES = 5 * 1024 * 1024

_DELIMITERS = ('\t', ',', ';', '|')

# The column every row must have.
NAME_KEY = 'name'

# Reason codes for a dropped row, and how they are worded to the user.
REASONS = {
    'no_name':   'no account name',
    'duplicate': 'already in this vault',
    'repeat':    'repeated in the file',
    'empty':     'nothing in the row',
    'limit':     f'past the {MAX_ROWS:,}-row limit',
    'too_long':  'a value too long to store',
    'reserved':  'a category name the app reserves',
}

# The two fields read as a yes/no rather than as text.
_BOOLEAN_KEYS = frozenset({'closed', FAVORITE_KEY})

# Fields whose cell is kept exactly as written. Every other cell is trimmed,
# which rescues a hand-typed "Chase, jsmith" — but a password with a leading or
# trailing space is a different password, and trimming it would import one that
# does not log in.
_VERBATIM_KEYS = frozenset({'password'})

# Fields no column may ever be read into. On top of the keys no export writes:
# 'last_updated', which every category declares. An imported record was last
# known good the moment it arrived; reading the file's stamp would land an old
# export pre-aged in Needs Attention.
_NEVER_IMPORTED = INTERNAL_KEYS | {'last_updated'}

_TRUE_WORDS = frozenset({'1', 'true', 'yes', 'y', 'on', 'closed', 'favourite',
                         'favorite', 'starred', 'pinned'})
_FALSE_WORDS = frozenset({'0', 'false', 'no', 'n', 'off', 'open', ''})

# Header spellings other tools write, mapped onto this app's field keys. Read
# on the way in, never written.
_HEADER_ALIASES = {
    'url': 'website', 'uri': 'website', 'web site': 'website',
    'login uri': 'website', 'site': 'website', 'link': 'website',
    'title': 'name', 'account': 'name', 'account name': 'name',
    'item name': 'name', 'label': 'name',
    'login username': 'username', 'user': 'username', 'user name': 'username',
    'login': 'username', 'email': 'username', 'e mail': 'username',
    'login password': 'password', 'pass': 'password', 'pwd': 'password',
    'note': 'notes', 'notes comments': 'notes', 'comments': 'notes',
    'extra': 'notes', 'memo': 'notes',
    'folder': 'category', 'group': 'category', 'type': 'category',
    'grouping': 'category', 'collection': 'category',
    'archived': 'closed', 'inactive': 'closed',
    'starred': FAVORITE_KEY, 'favourite': FAVORITE_KEY, 'pinned': FAVORITE_KEY,
}

# Non-alphanumerics collapse to single spaces before a header is matched, so
# 'Card Number', 'card_number' and 'CARD-NUMBER' are one spelling.
_HEADER_NOISE = re.compile(r'[^a-z0-9]+')


class AccountImportError(Exception):
    """Raised when a chosen file cannot be read at all."""


# -----------------------------------------------------------------------------
# Header vocabulary
#
# Built from the category schemas, so a new field is importable the moment it
# exists. Both a field's key and its label are accepted: a file is as likely to
# come from this app's export (keys) as from a person reading the screen.
# -----------------------------------------------------------------------------

def _normalize_header(text) -> str:
    return _HEADER_NOISE.sub(' ', str(text or '').strip().lower()).strip()


def _build_vocabulary() -> dict:
    """{normalized header: field key} for every field any category declares."""
    vocabulary = {}
    schemas = list(CATEGORY_CONFIG.values()) + [GENERIC_CONFIG]
    for config in schemas:
        for field in config['fields']:
            key = field['key']
            if key in _NEVER_IMPORTED:
                continue
            vocabulary.setdefault(_normalize_header(field['label']), key)
            vocabulary[_normalize_header(key)] = key

    # The account columns that are not in any schema's field list.
    for key in ('category', 'closed', FAVORITE_KEY):
        vocabulary[_normalize_header(key)] = key

    # Aliases last, and never over a real field name.
    for alias, key in _HEADER_ALIASES.items():
        if key in _NEVER_IMPORTED:
            continue
        vocabulary.setdefault(_normalize_header(alias), key)
    return vocabulary


HEADER_VOCABULARY = _build_vocabulary()


def field_key(header) -> str | None:
    """The field a column heading names, or None if this module cannot say."""
    return HEADER_VOCABULARY.get(_normalize_header(header))


# -----------------------------------------------------------------------------
# Value reading
# -----------------------------------------------------------------------------

def read_boolean(value) -> bool | None:
    """A yes/no cell as a bool, or None when it says neither — so an
    unrecognised 'Closed' cell leaves the account open rather than guessing."""
    text = str(value or '').strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def _cell(raw: str, key: str) -> str:
    """One cell's value as stored: un-defused, and trimmed unless it is a
    password. A password of nothing but spaces reads as empty."""
    if key in _VERBATIM_KEYS:
        value = undefuse(raw)
        return value if value.strip() else ''
    return undefuse(raw.strip()).strip()


# -----------------------------------------------------------------------------
# Table shape
# -----------------------------------------------------------------------------

def _read_rows(text: str, delimiter: str) -> list[tuple[int, str, list[str]]]:
    """(line number, original line, fields) for every line that holds something.

    Fields are as the CSV reader returned them, untrimmed — see _cell.

    Whole-document parsing, unlike history_io: an account's notes routinely hold
    newlines, and this app's own export writes them as quoted multi-line
    values. The line number comes from the reader, so a reported row is one the
    user can count to."""
    rows = []
    reader = csv.reader(text.splitlines(True), delimiter=delimiter)
    try:
        for fields in reader:
            trimmed = [f.strip() for f in fields]
            if not any(trimmed):
                continue
            rows.append((reader.line_num, delimiter.join(trimmed)[:200], fields))
    except csv.Error:
        # A malformed quote leaves this layout unusable; another delimiter may
        # still read the file.
        return []
    return rows


def _map_headers(labels: list[str]) -> dict:
    """{column index: field key} for the headings this module can name.

    A field claimed by two columns keeps the first; the second is reported as
    ignored, since only the user knows which of 'Name' and 'Account Name' is
    really the name."""
    mapping = {}
    taken = set()
    for index, label in enumerate(labels):
        key = field_key(label)
        if key is None or key in taken:
            continue
        taken.add(key)
        mapping[index] = key
    return mapping


def _plan(text: str) -> dict:
    """The delimiter that names the most columns.

    Scored on mapped headings rather than row count, so a separator that is
    really part of the data loses. A plan with no name column is still
    returned, and the caller says the first row has to be headings."""
    best = {'rows': [], 'labels': [], 'mapping': {}, 'delimiter': ''}
    for delimiter in _DELIMITERS:
        rows = _read_rows(text, delimiter)
        if not rows:
            continue
        mapping = _map_headers(rows[0][2])
        if len(mapping) <= len(best['mapping']):
            continue
        best = {'rows': rows[1:], 'labels': [label.strip() for label in rows[0][2]],
                'mapping': mapping, 'delimiter': delimiter}
    return best


# -----------------------------------------------------------------------------
# Preparation
# -----------------------------------------------------------------------------

def _existing_keys(existing) -> set:
    """(category, name) of what the vault already holds, lowercased. The pair,
    not the name alone: 'Chase' under Banking and under Credit Cards are two
    accounts."""
    keys = set()
    for account in existing or []:
        name = str(account.get('name') or '').strip().lower()
        if not name:
            continue
        keys.add((normalize_category(account.get('category') or '').lower(), name))
    return keys


def _row_values(fields: list[str], mapping: dict) -> dict:
    """One row's cells under the field keys their columns were named with."""
    values = {}
    for index, key in mapping.items():
        if index >= len(fields):
            continue
        values[key] = _cell(fields[index], key)
    return values


def _too_long(row: dict) -> bool:
    return any(len(str(row.get(key) or '')) > limit
               for key, limit in MAX_LENGTHS.items())


def prepare(text: str, existing=None, default_category: str = 'Websites') -> dict:
    """Reads a table of accounts into records ready for the vault.

    Returns {rows, skipped, reasons, mapped, ignored, categories, has_header,
    unreadable}, where rows is a list of flat account dicts in file order —
    services.accounts.create_accounts takes them as they are.

    A row matching an account in `existing` by category and name is dropped, so
    importing the same file twice adds nothing; rows repeating each other in
    the file are dropped the same way. A cell holding the "⚠ unreadable"
    placeholder (an export from a vault with damaged fields) is left blank and
    counted in `unreadable`."""
    text = str(text or '')
    # Also enforced here so it covers pasted text, which never goes through
    # read_file.
    if len(text.encode('utf-8', 'ignore')) > MAX_FILE_BYTES:
        raise AccountImportError(
            'That is too much text to be an account list — paste a smaller range.')

    plan = _plan(text)
    mapping = plan['mapping']
    result = {
        'rows': [], 'skipped': [], 'reasons': Counter(),
        'mapped': {plan['labels'][i]: key for i, key in sorted(mapping.items())
                   if i < len(plan['labels'])},
        'ignored': [label for index, label in enumerate(plan['labels'])
                    if index not in mapping and label],
        'categories': Counter(),
        'has_header': NAME_KEY in mapping.values(),
        'unreadable': 0,
    }
    if not result['has_header']:
        return result

    # `stored` never grows, so it still tells "already in the vault" apart from
    # "repeated in the file" after `seen` has absorbed the rows read so far.
    stored = _existing_keys(existing)
    seen = set(stored)
    default_category = str(default_category or 'Websites').strip() or 'Websites'

    def drop(number: int, line: str, reason: str):
        result['reasons'][reason] += 1
        # The summary counts them all; the list only shows what a bad row looks
        # like.
        if len(result['skipped']) < 12:
            result['skipped'].append(
                {'line': number, 'text': line[:120], 'reason': reason})

    for number, line, fields in plan['rows']:
        values = _row_values(fields, mapping)
        unreadable = [key for key, value in values.items() if is_unreadable(value)]
        for key in unreadable:
            values[key] = ''

        name = values.pop(NAME_KEY, '')
        if not name:
            drop(number, line, 'empty' if not any(values.values()) else 'no_name')
            continue

        category = normalize_category(values.pop('category', '').strip()) or default_category
        if is_reserved_category(category):
            drop(number, line, 'reserved')
            continue

        row = {'name': name, 'category': category}
        for field, value in values.items():
            if field in _BOOLEAN_KEYS:
                # An unreadable flag leaves the account open and unpinned.
                if read_boolean(value):
                    row[field] = True
                continue
            if value != '':
                row[field] = value
        if _too_long(row):
            drop(number, line, 'too_long')
            continue

        key = (category.lower(), name.lower())
        if key in seen:
            drop(number, line, 'duplicate' if key in stored else 'repeat')
            continue
        seen.add(key)

        if len(result['rows']) >= MAX_ROWS:
            drop(number, line, 'limit')
            continue

        result['unreadable'] += len(unreadable)
        result['categories'][category] += 1
        result['rows'].append(row)

    return result


# -----------------------------------------------------------------------------
# Describing a parse
# -----------------------------------------------------------------------------

def summarise(result: dict) -> str:
    """One line describing what an import would do, for the preview."""
    if not result['has_header']:
        return ('No column here is named as an account name. The first row has '
                'to head the columns — Name, Username, Password, and so on.')
    rows = result['rows']
    if not rows:
        return 'Nothing to import yet.'
    categories = result['categories']
    spread = (f"{len(categories)} categories" if len(categories) > 1
              else next(iter(categories), ''))
    text = f"{len(rows):,} account{'s' if len(rows) != 1 else ''}, {spread}."
    unreadable = result.get('unreadable', 0)
    if unreadable:
        text += (f" {unreadable:,} value{'s' if unreadable != 1 else ''} marked "
                 f"unreadable will be left blank.")
    return text


def columns_summary(result: dict) -> str:
    """Which headings were read as what, and which were left out — named, so a
    column read as the wrong field is visible before Import is pressed."""
    parts = []
    mapped = result['mapped']
    if mapped:
        pairs = ', '.join(f"'{header}' as {key.replace('_', ' ')}"
                          for header, key in mapped.items())
        parts.append(f'Reading {pairs}.')
    ignored = result['ignored']
    if ignored:
        listed = ', '.join(f"'{label}'" for label in ignored[:6])
        more = f" and {len(ignored) - 6} more" if len(ignored) > 6 else ''
        parts.append(f"Ignoring {listed}{more} — no field of this app matches.")
    return ' '.join(parts)


def skipped_summary(result: dict) -> str:
    """The dropped rows as counts by reason, or '' when nothing was dropped."""
    total = sum(result['reasons'].values())
    if not total:
        return ''
    parts = [f'{count:,} {REASONS.get(reason, reason)}'
             for reason, count in result['reasons'].most_common()]
    return f"{total:,} row{'s' if total != 1 else ''} skipped — " + ', '.join(parts) + '.'


def read_file(path) -> str:
    """The text of a chosen account file. See export_io.read_text."""
    try:
        return export_io.read_text(path, MAX_FILE_BYTES, 'an account list')
    except export_io.PlaintextReadError as exc:
        raise AccountImportError(str(exc)) from exc

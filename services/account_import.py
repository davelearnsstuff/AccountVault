# -----------------------------------------------------------------------------
# services/account_import.py
# A table of accounts read into records ready for the vault.
#
# The half of the door that was missing. The app could write its accounts out
# three ways — the whole-vault CSV, the per-account file, the balance history —
# and had no way at all to take any of them back, or to take a spreadsheet, or
# an export from whatever the user was keeping their passwords in before. The
# only route in was typing.
#
# Nothing here touches the database. It reads text, works out which column is
# which, and returns the rows worth keeping plus a reason for every one it
# dropped. The caller shows both and only then writes, so a misread column is
# visible while declining is still free — the same contract services/history_io
# holds, for the same reason, and this module deliberately mirrors its shape so
# that the two dialogs built on them can behave alike.
#
# Where this one differs is what it does with a column it cannot place. History
# is two columns and the parser can find them by looking at the values: a date
# looks like a date. An account is a dozen columns of free text, and no amount
# of looking at 'jsmith' says whether it is a username or a security answer.
# So the header row is required, and a column it cannot name is dropped and
# reported rather than guessed at or swept into extra_data — a guess here does
# not produce a visibly wrong chart, it files a password under 'notes'.
#
# One value is completed rather than read: a row with no category is filed
# under the category the user picks in the dialog, which is stated up front
# rather than inferred.
# -----------------------------------------------------------------------------

import csv
import re
from collections import Counter

from categories import (
    CATEGORY_CONFIG, FAVORITE_KEY, GENERIC_CONFIG, normalize_category,
)
from services import export_io
from services.export_io import INTERNAL_KEYS

# Accounts accepted from a single import. Well past any real password manager's
# export, and low enough that the wrong file picked by accident cannot fill a
# vault with rows that then have to be deleted one at a time.
MAX_ROWS = 2000

# Refuses a file larger than this rather than reading it. An account is a few
# hundred bytes of text, so anything past this is the wrong file.
MAX_FILE_BYTES = 5 * 1024 * 1024

_DELIMITERS = ('\t', ',', ';', '|')

# The column every row must have. Without a name there is nothing to call the
# account in the list, and services.accounts._require_name would reject it at
# the write anyway — better to say so in the preview, per row, than to fail the
# whole import on row 400.
NAME_KEY = 'name'

# Reason codes for a dropped row, and how they are worded to the user.
REASONS = {
    'no_name':   'no account name',
    'duplicate': 'already in this vault',
    'repeat':    'repeated in the file',
    'empty':     'nothing in the row',
    'limit':     f'past the {MAX_ROWS:,}-row limit',
}

# The two fields read as a yes/no rather than as text. Everything else the
# vocabulary below recognises is stored as the string in the cell.
_BOOLEAN_KEYS = frozenset({'closed', FAVORITE_KEY})

# Fields no column may ever be read into, whatever it is headed.
#
# The floor is export_io.INTERNAL_KEYS — the same keys no export writes a
# column for — so a key that stops being exportable stops being importable in
# the same edit rather than in a second one somebody has to remember.
#
# 'last_updated' is added on top, and it is the one that needs explaining: it
# is declared as a field by every category, so the vocabulary below would
# otherwise pick it up. The stamp says when the record was last known good, and
# it drives the staleness half of Needs Attention. An imported record was last
# known good the moment it arrived, so reading the file's date would let a
# three-year-old export land pre-aged — every account of it in Needs Attention
# on the day it was imported, for a reason the user cannot act on. The insert
# stamps today instead, which is the claim that is actually true.
_NEVER_IMPORTED = INTERNAL_KEYS | {'last_updated'}

_TRUE_WORDS = frozenset({'1', 'true', 'yes', 'y', 'on', 'closed', 'favourite',
                         'favorite', 'starred', 'pinned'})
_FALSE_WORDS = frozenset({'0', 'false', 'no', 'n', 'off', 'open', ''})

# Header spellings other tools write, mapped onto this app's field keys. Read
# on the way in and never written on the way out, exactly like
# categories.CATEGORY_ALIASES.
#
# The set is deliberately small and covers what the common exports actually
# head their columns: 'url'/'uri' for a website, 'title' for a name, 'folder'
# or 'group' for what this app calls a category, 'note' for notes. Anything
# past that is a column this module declines to name — see the header note.
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
# Built once from the category schemas rather than written out again, so a
# field added to a category is importable the moment it exists. Both spellings
# of every field are accepted — its stored key and the label the app shows it
# under — because a file is as likely to have come out of this app's own export
# (keys) as out of a person reading the screen (labels).
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
            # The key wins over a label that normalizes to the same string,
            # which is the common case ('name' / 'Name') and not a conflict.
            vocabulary.setdefault(_normalize_header(field['label']), key)
            vocabulary[_normalize_header(key)] = key

    # The account columns that are not in any schema's field list.
    for key in ('category', 'closed', FAVORITE_KEY):
        vocabulary[_normalize_header(key)] = key

    # Aliases last, and only where they do not displace a real field name. A
    # category that ever declares a field called 'type' or 'email' should keep
    # it rather than have the alias table quietly redirect the column.
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

def undefuse(value) -> str:
    """Undoes the leading apostrophe services/export_io.defuse adds.

    That apostrophe is the documented "this cell is text" marker, added so a
    password beginning with '=' is shown by a spreadsheet rather than executed.
    A spreadsheet consumes it on display but a file read as text still carries
    it, so without this a vault's own export re-imported every such password
    one character longer than it really is — and the account would then not log
    in, with nothing on screen to say why.

    Exactly one apostrophe, and only in front of a character that needed it.
    A password that genuinely starts with an apostrophe is left alone."""
    text = '' if value is None else str(value)
    if text[:1] == "'" and text[1:2] in ('=', '+', '-', '@', '\t', '\r'):
        return text[1:]
    return text


def read_boolean(value) -> bool | None:
    """A yes/no cell as a bool, or None when it says neither.

    None matters: a 'Closed' column holding something this does not recognise
    must leave the account open rather than guess, and a caller that cannot
    tell "false" from "unreadable" would have no way to make that choice."""
    text = str(value or '').strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


# -----------------------------------------------------------------------------
# Table shape
# -----------------------------------------------------------------------------

def _read_rows(text: str, delimiter: str) -> list[tuple[int, str, list[str]]]:
    """(line number, original line, fields) for every line that holds something.

    Whole-document parsing rather than line by line, unlike history_io: an
    account's notes field routinely holds newlines, and a quoted value spanning
    several lines is exactly what this app's own export writes for one. The
    line number is taken from the reader afterwards so a reported row is still
    one the user can count to.
    """
    rows = []
    reader = csv.reader(text.splitlines(True), delimiter=delimiter)
    try:
        for fields in reader:
            fields = [f.strip() for f in fields]
            if not any(fields):
                continue
            rows.append((reader.line_num, delimiter.join(fields)[:200], fields))
    except csv.Error:
        # A malformed quote leaves this layout unusable; another delimiter may
        # still read the file, and _plan scores them all.
        return []
    return rows


def _map_headers(labels: list[str]) -> dict:
    """{column index: field key} for the headings this module can name.

    A field claimed by two columns keeps the first. The second is reported as
    ignored rather than silently overwriting, because a file with both 'Name'
    and 'Account Name' means one of them is not the name and the user is the
    only one who knows which."""
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

    Scored on mapped headings rather than on row count, which is what makes a
    separator that is really part of the data lose: splitting a file of
    'Chase Visa, jsmith, hunter2' on the comma yields three named columns, and
    splitting it on the semicolon yields one column named nothing.

    A plan with no name column is still returned. It is the honest answer to a
    file whose first row is data rather than headings, and the caller says so
    in those words instead of importing a header as an account."""
    best = {'rows': [], 'labels': [], 'mapping': {}, 'delimiter': ''}
    for delimiter in _DELIMITERS:
        rows = _read_rows(text, delimiter)
        if not rows:
            continue
        mapping = _map_headers(rows[0][2])
        if len(mapping) <= len(best['mapping']):
            continue
        best = {'rows': rows[1:], 'labels': rows[0][2], 'mapping': mapping,
                'delimiter': delimiter}
    return best


# -----------------------------------------------------------------------------
# Preparation
# -----------------------------------------------------------------------------

def _existing_keys(existing) -> set:
    """(category, name) of what the vault already holds, lowercased.

    The pair, not the name alone: 'Chase' filed under Banking and 'Chase' filed
    under Credit Cards are two accounts, and most people have both."""
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
        values[key] = undefuse(fields[index]).strip()
    return values


def prepare(text: str, existing=None, default_category: str = 'Websites') -> dict:
    """Reads a table of accounts into records ready for the vault.

    Returns {rows, skipped, reasons, mapped, ignored, categories, has_header},
    where rows is a list of flat account dicts in file order —
    services.accounts.create_accounts takes them as they are.

    `existing` is what the vault already holds: a row matching one by category
    and name is dropped, which makes importing the same file twice a no-op
    rather than a doubled vault. Rows repeating each other inside the file are
    dropped the same way, on the grounds that a file listing one account twice
    still describes one account.
    """
    text = str(text or '')
    # The same ceiling read_file applies, enforced here too so it covers pasted
    # text — which never goes through read_file, and which _plan then parses
    # four times over while it works out the layout.
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
    }
    if not result['has_header']:
        return result

    # Two sets rather than one. `stored` never grows, so it still answers
    # "was this already in the vault" after `seen` has absorbed the rows read
    # so far — which is what tells a file imported twice apart from a file
    # that lists an account twice.
    stored = _existing_keys(existing)
    seen = set(stored)
    default_category = str(default_category or 'Websites').strip() or 'Websites'

    def drop(number: int, line: str, reason: str):
        result['reasons'][reason] += 1
        # Only the first few are kept: the summary counts them all, and the
        # list is there to show what a bad row looks like, not to reproduce
        # the file.
        if len(result['skipped']) < 12:
            result['skipped'].append(
                {'line': number, 'text': line[:120], 'reason': reason})

    for number, line, fields in plan['rows']:
        values = _row_values(fields, mapping)

        name = values.pop(NAME_KEY, '')
        if not name:
            # A row of separators with a stray cell in it is not worth
            # reporting as a failed account; one with real content is.
            drop(number, line, 'empty' if not any(values.values()) else 'no_name')
            continue

        category = normalize_category(values.pop('category', '').strip()) or default_category
        key = (category.lower(), name.lower())
        if key in seen:
            # Told apart on purpose. 'Already in this vault' is the file being
            # imported twice and means nothing was lost; 'repeated in the file'
            # is the file itself listing an account twice, which the user may
            # want to look at.
            drop(number, line, 'duplicate' if key in stored else 'repeat')
            continue
        seen.add(key)

        if len(result['rows']) >= MAX_ROWS:
            drop(number, line, 'limit')
            continue

        row = {'name': name, 'category': category}
        for field, value in values.items():
            if field in _BOOLEAN_KEYS:
                flag = read_boolean(value)
                # An unreadable flag leaves the account open and unpinned,
                # which is what a row that never carried the column gets.
                if flag:
                    row[field] = True
                continue
            if value != '':
                row[field] = value

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
    return f"{len(rows):,} account{'s' if len(rows) != 1 else ''}, {spread}."


def columns_summary(result: dict) -> str:
    """Which headings were read as what, and which were left out.

    Named rather than counted. This module decides where every value in the
    file lands, and a column read as the wrong field has to be visible before
    the Import button is pressed rather than found later in an account."""
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

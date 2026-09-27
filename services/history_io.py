# -----------------------------------------------------------------------------
# services/history_io.py
# The text form of one account's balance history, in both directions.
#
# Nothing here touches the database. Reading, it takes text the user got from
# somewhere else — a bank's CSV export, a spreadsheet column, a table copied off
# a statement — works out which column is the date and which is the money, and
# returns the rows worth keeping plus a reason for every one it dropped. The
# caller shows both and only then writes, so a misread column is visible while
# declining is still free.
#
# Writing, it renders the entries back out as a CSV holding dates and figures
# and nothing else. Both directions live here so the round trip is one file's
# problem: the export writes the format the import is surest of, and a test
# holds the two to it.
#
# The parsing is deliberately forgiving about format and strict about meaning:
# it will read '01/05/24', 'Jan 5, 2024' and '2024-01-05 00:00:00' as the same
# day, but a row it is unsure of is reported, not invented — history drives the
# account's current balance, so a fabricated entry does not just sit there
# looking wrong, it changes what the account says it holds.
#
# One date is completed rather than read, and only because it is unambiguous
# about what it means and silent only about precision: 'YYYY-MM' is filed on
# the 1st of that month. A bare year, 'Jan 2024' and '01/2024' are all still
# refused — the first cannot name a month at all, and the other two would have
# to be read as a month before the same rule could apply to them.
# -----------------------------------------------------------------------------

import csv
import datetime
import io
import re
from collections import Counter

from categories import DEFAULT_VALUE_NOUN, parse_money, trim_number
from services import export_io
from services.export_io import defuse

# Rows accepted from a single import. Well past any real statement history, and
# low enough that a file pasted in by accident cannot bloat the account's
# history table into something that has to be deleted one entry at a time.
MAX_ENTRIES = 2000

# Refuses a file larger than this rather than reading it. History is a few tens
# of bytes per row, so anything past this is the wrong file.
MAX_FILE_BYTES = 5 * 1024 * 1024

_DELIMITERS = ('\t', ',', ';', '|')

_MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

# Header names that identify each column, checked as substrings of the
# lowercased header so 'Statement Date' and 'Ending Balance' both land.
#
# Order is priority, not preference for the leftmost column: a bank export
# headed Date, Description, Amount, Balance holds the day's transaction under
# 'Amount' and the running total under 'Balance', and it is the running total
# this app tracks. Matching 'balance' across every column before 'amount' is
# tried anywhere is what picks the right one.
_DATE_HEADERS = ('date', 'as of', 'asof', 'as-of', 'posted', 'period', 'when', 'day')
_AMOUNT_HEADERS = ('balance', 'value', 'worth', 'equity', 'principal',
                   'total', 'amount', 'usd', '$')

# 2024-01-05, 2024/01/05 — year first, so the order is not in question.
_ISO = re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$')
# 2024-01 — a month with no day in it, filed on the 1st. Same separators as the
# full form above, and a four-digit year is required, so '01-05' is still a
# day-precision date missing its year rather than May of year 1.
_ISO_MONTH = re.compile(r'^(\d{4})[-/.](\d{1,2})$')
# 01/05/2024, 1-5-24 — order is ambiguous; see _from_numeric.
_NUMERIC = re.compile(r'^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})$')
# Jan 5, 2024 / January 5th 2024 / Jan-05-2024
_MONTH_FIRST = re.compile(
    r'^([A-Za-z]{3,9})\.?[-\s]+(\d{1,2})(?:st|nd|rd|th)?,?[-\s]+(\d{2,4})$', re.I)
# 5 Jan 2024 / 5th January, 2024
_DAY_FIRST = re.compile(
    r'^(\d{1,2})(?:st|nd|rd|th)?[-\s]+([A-Za-z]{3,9})\.?,?[-\s]+(\d{2,4})$', re.I)
# A trailing time, as Excel and most exports write it: '2024-01-05 00:00:00'.
_TIME_SUFFIX = re.compile(r'^(.+?)[T\s]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AaPp]\.?[Mm]\.?)?$')
# An amount whose only comma is followed by exactly two digits — see parse_amount.
_DECIMAL_COMMA = re.compile(r'-?\d+,\d{2}')
# Currency symbols and spacing, stripped before the test above. Deliberately not
# the comma: that is the character being judged.
_MONEY_STRIP_FOR_COMMA = re.compile(r'[$£€¥₹\s]')
# The money at the end of a line that arrived as one undelimited string.
# Anchored to the end, because the date is whatever precedes it, and required to
# start at a separator: without that, the leading '-' of a minus sign lets the
# match begin inside the date, and '2024-02-05 1340.50' reads as a balance of
# minus five and a date of 2024-02.
_TRAILING_AMOUNT = re.compile(
    r'(?:^|[\s,;|])(\(?\s*-?\s*[$£€¥₹]?\s*-?\d[\d,]*(?:\.\d+)?\s*\)?-?)\s*$')

# Reason codes for a dropped row, and how they are worded to the user.
REASONS = {
    'no_date':   'no readable date',
    'no_amount': 'no readable amount',
    'future':    'dated in the future',
    'duplicate': 'already recorded',
    'limit':     f'past the {MAX_ENTRIES:,}-row limit',
}


class HistoryImportError(Exception):
    """Raised when a chosen file cannot be read at all."""


# -----------------------------------------------------------------------------
# Field parsing
# -----------------------------------------------------------------------------

def _expand_year(value: int) -> int:
    """Two-digit years, pivoted the way POSIX does: 69-99 are last century.

    A balance dated 1969 is unlikely and one dated 2069 is impossible, so the
    pivot only ever has to be defensible for the recent past."""
    if value >= 100:
        return value
    return 2000 + value if value <= 68 else 1900 + value


def _build(year: int, month: int, day: int) -> str | None:
    try:
        return datetime.date(_expand_year(year), month, day).isoformat()
    except ValueError:
        return None


def _from_numeric(first: int, second: int, year: int) -> str | None:
    """Reads a slash-or-dash date whose field order the format does not state.

    Month first, because that is what this app displays (format_date renders
    MM-DD-YYYY) and what a US export writes. The exception is the one case
    where month-first is provably wrong — a first field past 12 — which is
    read day-first rather than thrown away."""
    if first > 12 and second <= 12:
        return _build(year, second, first)
    return _build(year, first, second)


def parse_date(value) -> str | None:
    """A date in any of the accepted spellings as YYYY-MM-DD, or None.

    'YYYY-MM' is the one input that comes back carrying a day it did not
    supply: a month-precision figure is filed on the 1st. That is a stated
    convention rather than a guess at what the file meant — monthly history is
    a real thing to keep, and it has to land on some day to be a history entry
    at all. Every other partial date is still refused."""
    text = str(value or '').strip().strip('"\'')
    if not text:
        return None

    stamped = _TIME_SUFFIX.match(text)
    if stamped:
        text = stamped.group(1).strip()

    match = _ISO.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _build(year, month, day)

    match = _ISO_MONTH.match(text)
    if match:
        year, month = (int(g) for g in match.groups())
        return _build(year, month, 1)

    match = _NUMERIC.match(text)
    if match:
        first, second, year = (int(g) for g in match.groups())
        return _from_numeric(first, second, year)

    match = _MONTH_FIRST.match(text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        return _build(int(match.group(3)), month, int(match.group(2))) if month else None

    match = _DAY_FIRST.match(text)
    if match:
        month = _MONTHS.get(match.group(2)[:3].lower())
        return _build(int(match.group(3)), month, int(match.group(1))) if month else None

    return None


def parse_amount(value) -> float | None:
    """An amount as a number, or None. Understands the two ways a statement
    writes a negative that parse_money does not: parentheses and a trailing
    minus."""
    text = str(value or '').strip().strip('"\'')
    if not text:
        return None

    negative = False
    if text.startswith('(') and text.endswith(')'):
        negative, text = True, text[1:-1].strip()
    if text.endswith('-'):
        negative, text = True, text[:-1].strip()

    # '1200,00' is a decimal comma, not a thousands separator: no amount
    # written the US way ever groups two digits. Without this parse_money
    # strips the comma and the figure silently arrives a hundred times too big.
    #
    # Tested against the figure with its currency symbol and spacing removed,
    # not against the raw text. Matching the raw text meant '1200,00' was
    # caught but '€1200,00' — which is how the statements that write a decimal
    # comma actually spell it — was not, and went through as 120000.
    bare = _MONEY_STRIP_FOR_COMMA.sub('', text)
    if _DECIMAL_COMMA.fullmatch(bare):
        text = bare.replace(',', '.')

    number = parse_money(text)
    if number is None:
        return None
    return -abs(number) if negative else number


def canonical_amount(number: float) -> str:
    """The stored form of an amount. Whole numbers stay whole, matching what
    _sync_current_balance writes back onto the account — both go through
    categories.trim_number, which is the one definition of that spelling."""
    return str(trim_number(number))


# -----------------------------------------------------------------------------
# Table shape
#
# The layout is not asked for, it is worked out — and the obvious way to do that
# is wrong. Sniffing the most common separator picks the comma out of
# 'Jan 5, 2024  $1,200.00', where every comma sits inside a value rather than
# between two, and shreds the row. So every plausible layout is built instead,
# scored on how many rows come out of it as a date and an amount, and the best
# one wins. A separator that is really part of the data scores badly by
# construction.
# -----------------------------------------------------------------------------

def _read_rows(text: str, delimiter: str) -> list[tuple[int, str, list[str]]]:
    """(line number, original line, fields) for every line that holds something.

    Parsed a line at a time rather than handing the whole document to
    csv.reader, so a reported line number is the one the user can count to in
    their file. The cost is that a quoted field spanning two lines is not
    understood, which no history export produces."""
    rows = []
    for number, line in enumerate(str(text or '').splitlines(), start=1):
        if not line.strip():
            continue
        fields = [f.strip() for f in next(csv.reader([line], delimiter=delimiter), [])]
        if not any(fields):
            # A row of nothing but separators: blank, whatever it looks like,
            # and not worth reporting as a row that failed to parse.
            continue
        rows.append((number, line.strip(), fields))
    return rows


def _raw_rows(text: str) -> list[tuple[int, str, list[str]]]:
    """Every non-blank line, unsplit — the input to the no-separator layout."""
    return [(number, line.strip(), [line.strip()])
            for number, line in enumerate(str(text or '').splitlines(), start=1)
            if line.strip()]


def _noun_words(noun: str) -> tuple[str, ...]:
    """The words of a category's value noun worth matching a header against —
    'Annual Pay' should recognise a column headed 'Pay'."""
    return tuple(word for word in re.split(r'\W+', noun.lower()) if len(word) >= 3)


def _is_header(fields: list[str]) -> bool:
    """Whether a first row is naming the columns rather than holding data.

    A row with a date in it is data, always. Without one it used to be taken as
    a header unconditionally — which silently swallowed a real row whose date
    this module could not read, dropping it with no entry in `skipped` and no
    reason shown, in a module whose whole contract is that a row it is unsure
    of is reported rather than invented.

    So a dateless row is a header only if it also looks like one: either it
    carries no readable amount (nothing of value to lose) or one of its cells
    names a column. 'Opening Balance, 1200.00' is data with an unreadable
    date; 'Statement Date, Balance' is a header."""
    if any(parse_date(field) for field in fields):
        return False
    if not any(parse_amount(field) is not None for field in fields):
        return True
    return _match_header(fields, _DATE_HEADERS + _AMOUNT_HEADERS) is not None


def _match_header(labels: list[str], keywords) -> int | None:
    """The column a keyword names. Keywords are tried in their own order first,
    across every column, so priority beats position."""
    for keyword in keywords:
        for index, label in enumerate(labels):
            if keyword in label.lower():
                return index
    return None


def _detect_columns(rows, width: int) -> tuple[int | None, int | None]:
    """Picks the date and amount columns from the values themselves.

    The amount is taken from the last column holding money rather than the
    first, for the same reason 'balance' outranks 'amount' above: a bank export
    runs Date, Description, Debit, Credit, Balance, and the running balance at
    the end is the figure this app tracks."""
    dates = [0] * width
    amounts = [0] * width
    for _, _, fields in rows:
        for index in range(min(width, len(fields))):
            if parse_date(fields[index]):
                dates[index] += 1
            elif parse_amount(fields[index]) is not None:
                # elif: a date must never also count as money, or a column of
                # them competes for the amount slot.
                amounts[index] += 1

    date_column = max(range(width), key=lambda i: (dates[i], -i))
    if not dates[date_column]:
        return None, None

    candidates = [i for i in range(width) if i != date_column and amounts[i]]
    amount_column = max(candidates, key=lambda i: (amounts[i], i)) if candidates else None
    return date_column, amount_column


def _column_label(labels: list[str], index: int | None) -> str:
    if index is None:
        return ''
    if index < len(labels) and labels[index]:
        return labels[index]
    return f'Column {index + 1}'


def _extract_pair(line: str) -> tuple[str, str] | None:
    """Splits a row that has no usable separator into date and amount.

    For 'Jan 5, 2024  $1,200.00' pasted out of a PDF or a web table. The amount
    is the number the line ends with; the date is everything in front of it."""
    match = _TRAILING_AMOUNT.search(line)
    if not match:
        return None
    head = line[:match.start()].strip().strip(',;|').strip()
    return (head, match.group(1).strip()) if head else None


# -----------------------------------------------------------------------------
# Layout plans
# -----------------------------------------------------------------------------

def _pair(plan: dict, line: str, fields: list[str]) -> tuple[str, str] | None:
    """The (date text, amount text) one row yields under this plan."""
    if plan['extract']:
        return _extract_pair(line)
    date_column, amount_column = plan['date'], plan['amount']
    if date_column is None or amount_column is None:
        return None
    return (fields[date_column] if date_column < len(fields) else '',
            fields[amount_column] if amount_column < len(fields) else '')


def _score(plan: dict) -> dict:
    """Counts the rows this plan reads as both a date and an amount.

    Format only — a row dropped later for being a duplicate or dated in the
    future was still read correctly, and must not count against the layout that
    read it."""
    usable = 0
    for _, line, fields in plan['rows']:
        pair = _pair(plan, line, fields)
        if pair and parse_date(pair[0]) and parse_amount(pair[1]) is not None:
            usable += 1
    plan['usable'] = usable
    return plan


def _column_plan(text: str, delimiter: str, noun: str) -> dict:
    rows = _read_rows(text, delimiter)
    labels: list[str] = []
    # Only with a row left behind it: a lone row that failed to parse as a date
    # is a row this layout cannot use, not proof that it is a header.
    if len(rows) > 1 and _is_header(rows[0][2]):
        labels = rows[0][2]
        rows = rows[1:]

    width = max((len(fields) for _, _, fields in rows), default=0)
    date_column = amount_column = None
    if width > 1:
        if labels:
            date_column = _match_header(labels, _DATE_HEADERS)
            amount_column = _match_header(labels, _noun_words(noun) + _AMOUNT_HEADERS)
        # Header names are a hint, not the answer: a file can head its columns
        # anything at all, so whichever one they failed to identify is worked
        # out from the values underneath them.
        if date_column is None or amount_column is None:
            detected_date, detected_amount = _detect_columns(rows, width)
            date_column = detected_date if date_column is None else date_column
            amount_column = detected_amount if amount_column is None else amount_column

    return _score({'rows': rows, 'labels': labels, 'date': date_column,
                   'amount': amount_column, 'extract': False})


def _extract_plan(text: str) -> dict:
    return _score({'rows': _raw_rows(text), 'labels': [], 'date': None,
                   'amount': None, 'extract': True})


def _plan(text: str, noun: str) -> dict:
    """The layout that reads the most rows. Ties go to a real separator, which
    names its columns and so can tell the user what it decided."""
    best = None
    for delimiter in _DELIMITERS:
        if delimiter not in text:
            continue
        plan = _column_plan(text, delimiter, noun)
        if best is None or plan['usable'] > best['usable']:
            best = plan

    extract = _extract_plan(text)
    if best is None or extract['usable'] > best['usable'] or not best['usable']:
        # The last clause is the nothing-worked case. Reporting per line rather
        # than per cell names what the user is looking at, instead of a column
        # that was never really there.
        best = extract
    return best


# -----------------------------------------------------------------------------
# Preparation
# -----------------------------------------------------------------------------

def _existing_keys(existing) -> set[tuple[str, str]]:
    """(date, amount) of what the account already holds, for spotting a file
    that has been imported once already."""
    keys = set()
    for entry in existing or []:
        number = parse_amount(entry.get('balance'))
        if number is None:
            # An entry that cannot be read is not something a new row can be
            # judged a duplicate of, so it does not block one.
            continue
        keys.add((str(entry.get('recorded_at'))[:10], canonical_amount(number)))
    return keys


def prepare(text: str, existing=None, noun: str = DEFAULT_VALUE_NOUN,
            today: str | None = None) -> dict:
    """Reads a pasted or loaded table into entries ready for the account.

    Returns {entries, skipped, reasons, date_column, amount_column}, where
    entries is a list of (amount, YYYY-MM-DD) in ascending date order — the
    order they should be inserted in, so that same-day rows keep the file's own
    ordering once the ids break the tie.

    `existing` is the account's current history: rows matching one exactly are
    dropped, which is what makes importing the same statement twice a no-op
    instead of a doubled chart."""
    today = today or datetime.date.today().isoformat()
    text = str(text or '')
    # The same ceiling read_file applies, enforced here too so it also covers
    # pasted text — which never goes through read_file, and which _plan then
    # parses five times over while it works out the layout.
    if len(text.encode('utf-8', 'ignore')) > MAX_FILE_BYTES:
        raise HistoryImportError(
            'That is too much text to be balance history — paste a smaller range.')

    plan = _plan(text, noun)
    result = {'entries': [], 'skipped': [], 'reasons': Counter(),
              'date_column': _column_label(plan['labels'], plan['date']),
              'amount_column': _column_label(plan['labels'], plan['amount'])}

    seen = _existing_keys(existing)
    # When the plan found no columns at all, _pair returns None for every row
    # and the reason is the same for every row. Worked out once rather than by
    # re-parsing each whole line as a date inside the loop.
    no_columns = not plan['extract'] and (plan['date'] is None or plan['amount'] is None)
    missing_reason = 'no_date' if plan['date'] is None else 'no_amount'

    def drop(number: int, line: str, reason: str):
        result['reasons'][reason] += 1
        # Only the first few are kept: the summary counts them all, and the list
        # is there to show what a bad row looks like, not to reproduce the file.
        if len(result['skipped']) < 12:
            result['skipped'].append({'line': number, 'text': line[:120], 'reason': reason})

    for number, line, fields in plan['rows']:
        pair = _pair(plan, line, fields)
        if pair is None:
            if no_columns:
                drop(number, line, missing_reason)
            else:
                drop(number, line, 'no_amount' if parse_date(line) else 'no_date')
            continue

        recorded_at = parse_date(pair[0])
        if recorded_at is None:
            drop(number, line, 'no_date')
            continue

        value = parse_amount(pair[1])
        if value is None:
            drop(number, line, 'no_amount')
            continue

        if recorded_at > today:
            # The same rule the add-entry row enforces with its date cap: the
            # latest-dated entry is what the account currently holds, so one
            # dated ahead would hold the account on it until it was deleted.
            drop(number, line, 'future')
            continue

        amount = canonical_amount(value)
        if (recorded_at, amount) in seen:
            drop(number, line, 'duplicate')
            continue
        seen.add((recorded_at, amount))

        if len(result['entries']) >= MAX_ENTRIES:
            drop(number, line, 'limit')
            continue
        result['entries'].append((amount, recorded_at))

    # Ascending, and stable, so rows sharing a date stay in file order.
    result['entries'].sort(key=lambda entry: entry[1])
    return result


def summarise(result: dict) -> str:
    """One line describing what an import would do, for the preview."""
    entries = result['entries']
    if not entries:
        return 'Nothing to import yet.'
    first, last = entries[0][1], entries[-1][1]
    span = f'{first} to {last}' if first != last else first
    return f"{len(entries):,} entr{'ies' if len(entries) != 1 else 'y'}, {span}."


def skipped_summary(result: dict) -> str:
    """The dropped rows as counts by reason, or '' when nothing was dropped."""
    total = sum(result['reasons'].values())
    if not total:
        return ''
    parts = [f'{count:,} {REASONS.get(reason, reason)}'
             for reason, count in result['reasons'].most_common()]
    return f"{total:,} row{'s' if total != 1 else ''} skipped — " + ', '.join(parts) + '.'


def read_file(path) -> str:
    """The text of a chosen history file.

    The reading itself — the size cap and the encoding fallback — is
    export_io.read_text, shared with the account importer, which needs exactly
    the same thing and used to carry a second copy of it. The error type stays
    this module's, because every caller here catches it by name."""
    try:
        return export_io.read_text(path, MAX_FILE_BYTES, 'balance history')
    except export_io.PlaintextReadError as exc:
        raise HistoryImportError(str(exc)) from exc


# -----------------------------------------------------------------------------
# Export
#
# The history and nothing else. This file leaves the vault as plaintext, so what
# it may carry is decided here rather than by whatever the caller happened to
# pass: a date and a figure per row, and no field of the account they belong to
# — not its name, not its username, and certainly not its password. The account
# reaches the file only through services/account_export.py, which composes this
# section with an account one when the user asks for both.
# -----------------------------------------------------------------------------

def export_csv(entries, noun: str = DEFAULT_VALUE_NOUN) -> dict:
    """Renders history entries as CSV text. Returns {text, written, unreadable}.

    Dates are written ISO, not in the MM-DD-YYYY the app displays: this is the
    one spelling parse_date can never read two ways, so the file reads back into
    the same days it was written from no matter whose machine opens it.

    An entry that will not decrypt is counted and left out rather than written
    as its placeholder. The caller reports the count — a figure that came back
    '⚠ unreadable' is not a figure, and a spreadsheet holding it would be one
    the numbers no longer add up in."""
    rows, unreadable = [], 0
    for entry in entries or []:
        number = parse_amount(entry.get('balance'))
        if number is None:
            unreadable += 1
            continue
        rows.append((str(entry.get('recorded_at'))[:10], canonical_amount(number)))
    # Oldest first: a spreadsheet plots a series in the order it reads it, and
    # it is the order prepare() hands entries back in, so a file that goes out
    # and comes back does not reverse itself on the way.
    rows.sort(key=lambda row: row[0])

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator='\r\n')
    # Every cell goes through defuse, the header included: the noun comes from a
    # category the user can name, so it is no more trusted than a value.
    writer.writerow([defuse('Date'), defuse(noun)])
    for recorded_at, amount in rows:
        writer.writerow([defuse(recorded_at), defuse(amount)])

    return {'text': buffer.getvalue(), 'written': len(rows), 'unreadable': unreadable}

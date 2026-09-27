# -----------------------------------------------------------------------------
# tests/test_history_io.py
# One account's balance history in and out of a text file.
#
# Three parts. The parser is where the risk lives: it is handed a file the app
# did not write, in a layout nobody stated, and every guess it makes about
# which column is the money ends up as a figure the account reports as its
# balance. The batch write has to be all-or-nothing — a half-finished import is
# the one state there is no way back out of short of deleting entries by hand.
# The export has one job beyond writing the rows, which is to carry the history
# and nothing else: it leaves the vault as plaintext.
# -----------------------------------------------------------------------------

import csv
import io
from datetime import date, timedelta

import pytest

import database
import encryption
from conftest import make_account, snapshot_rows
from services import accounts as accounts_service
from services import history_io

TODAY = '2026-08-18'


def prepare(text, existing=None, noun='Balance'):
    return history_io.prepare(text, existing, noun, today=TODAY)


def entries(text, existing=None, noun='Balance'):
    return prepare(text, existing, noun)['entries']


# -- layouts ------------------------------------------------------------------

def test_a_plain_two_column_paste(vault):
    assert entries('2024-01-05,1200.00\n2024-02-05,1340.50') == [
        ('1200', '2024-01-05'), ('1340.5', '2024-02-05'),
    ]


def test_a_tab_separated_paste_with_currency_and_separators(vault):
    """What copying two spreadsheet columns actually puts on the clipboard."""
    assert entries('Date\tBalance\n1/5/24\t$1,200.00\n2/5/24\t$1,340.50') == [
        ('1200', '2024-01-05'), ('1340.5', '2024-02-05'),
    ]


def test_the_running_balance_wins_over_the_transaction_amount(vault):
    """A bank export carries both. This app tracks what the account holds, not
    what moved that day, and 'Amount' is the wrong one of the two."""
    result = prepare(
        'Date,Description,Amount,Balance\n'
        '01/05/2024,Deposit,500.00,1200.00\n'
        '02/05/2024,ATM,-40.00,1160.00'
    )

    assert result['amount_column'] == 'Balance'
    assert result['entries'] == [('1200', '2024-01-05'), ('1160', '2024-02-05')]


def test_unnamed_columns_are_found_from_their_values(vault):
    """No header, and a description column in between."""
    result = prepare('01/05/2024,groceries,1200.00\n02/05/2024,rent,1160.00')

    assert result['entries'] == [('1200', '2024-01-05'), ('1160', '2024-02-05')]
    # Named for the user, who has no other way to check the guess was right.
    assert (result['date_column'], result['amount_column']) == ('Column 1', 'Column 3')


def test_a_date_containing_a_comma_does_not_split_the_row(vault):
    """The reason the layout is scored rather than sniffed: every comma in
    'Jan 5, 2024  $1,200.00' is inside a value, so the most common separator is
    the one that destroys the row."""
    assert entries('Jan 5, 2024  $1,200.00\nFeb 5, 2024  $1,340.50') == [
        ('1200', '2024-01-05'), ('1340.5', '2024-02-05'),
    ]


def test_a_line_with_no_separator_at_all(vault):
    assert entries('2024-01-05 1200.00\n2024-02-05 1340.50') == [
        ('1200', '2024-01-05'), ('1340.5', '2024-02-05'),
    ]


def test_the_category_noun_names_its_own_column(vault):
    result = prepare('When,Annual Pay\n2024-01-05,90000\n2025-01-05,95000',
                     noun='Annual Pay')

    assert result['amount_column'] == 'Annual Pay'
    assert result['entries'] == [('90000', '2024-01-05'), ('95000', '2025-01-05')]


def test_entries_come_back_oldest_first(vault):
    """The order they are inserted in. Same-date rows break their tie on the id,
    so the file's own order has to survive to the insert."""
    assert entries('2024-03-01,300\n2024-01-01,100\n2024-02-01,200') == [
        ('100', '2024-01-01'), ('200', '2024-02-01'), ('300', '2024-03-01'),
    ]


# -- dates --------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('2024-01-05', '2024-01-05'),
    ('2024/01/05', '2024-01-05'),
    ('01/05/2024', '2024-01-05'),     # month first, as this app displays dates
    ('1-5-2024',   '2024-01-05'),
    ('01/05/24',   '2024-01-05'),
    ('25/12/2023', '2023-12-25'),     # month-first is impossible, so day-first
    ('Jan 5, 2024', '2024-01-05'),
    ('January 5th 2024', '2024-01-05'),
    ('5 Jan 2024', '2024-01-05'),
    ('2024-01-05 00:00:00', '2024-01-05'),
    ('2024-01-05T09:30', '2024-01-05'),
    ('2024-01', '2024-01-01'),        # a month with no day is filed on the 1st
    ('2024/01', '2024-01-01'),
    ('2024-1',  '2024-01-01'),
])
def test_date_spellings(text, expected):
    assert history_io.parse_date(text) == expected


@pytest.mark.parametrize('text', [
    '', 'sometime', '2024-13-05', '2024-02-30', '2024-13', '2024-00',
    '01/2024', 'Jan 2024', '2024', '01-05',
])
def test_dates_that_are_refused_rather_than_guessed_at(text):
    """A date that does not exist, or one there is no reading of at all.

    '2024' names no month, and '01/2024' and 'Jan 2024' would each have to be
    read as a month before the YYYY-MM rule below could date them — which is a
    guess at the format, not the precision. '01-05' stays a day-precision date
    missing its year rather than becoming May of year 1."""
    assert history_io.parse_date(text) is None


def test_a_month_is_dated_to_the_first_of_it(vault):
    """Monthly history is a real thing to keep, and an entry has to land on
    some day to be an entry at all."""
    assert entries('2024-01,1200\n2024-02,1340.50\n2024-03,1410') == [
        ('1200', '2024-01-01'), ('1340.5', '2024-02-01'), ('1410', '2024-03-01'),
    ]


def test_months_and_full_dates_can_share_a_file(vault):
    assert entries('2024-01,1200\n2024-02-15,1300') == [
        ('1200', '2024-01-01'), ('1300', '2024-02-15'),
    ]


def test_a_month_that_is_already_recorded_is_not_imported_twice(vault):
    """The 1st is where the rule files it, so re-importing the same monthly
    file matches the entry the first import left behind."""
    existing = [{'balance': '1200', 'recorded_at': '2024-01-01'}]

    result = prepare('2024-01,1200', existing)

    assert result['entries'] == []
    assert result['reasons']['duplicate'] == 1


def test_a_month_still_in_the_future_is_left_out(vault):
    """The current month dates to the 1st, which has been and gone, so it
    imports; next month has not."""
    result = prepare('2026-08,1500\n2026-09,9999')

    assert result['entries'] == [('1500', '2026-08-01')]
    assert result['reasons']['future'] == 1


def test_a_monthly_file_does_not_lose_its_first_row_to_the_header_check(vault):
    """A first row is a header when it holds no date. Before YYYY-MM was read
    as one, an unheaded monthly file's opening row looked exactly like that."""
    assert len(entries('2024-01,1200\n2024-02,1300')) == 2


# -- amounts ------------------------------------------------------------------

@pytest.mark.parametrize('text, expected', [
    ('1200', 1200), ('1,200.50', 1200.5), ('$1,200', 1200),
    ('(1,200.00)', -1200),       # accounting parentheses
    ('1200.50-', -1200.5),       # trailing minus
    ('-1200', -1200),
    ('1200,00', 1200),           # decimal comma, not a thousands group
])
def test_amount_spellings(text, expected):
    assert history_io.parse_amount(text) == expected


def test_a_decimal_comma_is_not_multiplied_by_a_hundred(vault):
    """parse_money strips commas, so '1200,00' would otherwise arrive as
    120,000 — the kind of wrong that looks plausible in a chart."""
    assert entries('Datum;Saldo\n2024-01-05;1200,00') == [('1200', '2024-01-05')]


# -- rows that are dropped ----------------------------------------------------

def test_rows_that_cannot_be_read_are_reported_not_invented(vault):
    result = prepare('Date,Balance\n2024-01-05,1200\nnot a row at all\n2024-03-01,')

    assert result['entries'] == [('1200', '2024-01-05')]
    assert result['reasons']['no_date'] == 1
    assert result['reasons']['no_amount'] == 1
    assert {row['line'] for row in result['skipped']} == {3, 4}


def test_a_row_of_nothing_but_separators_is_blank_not_broken(vault):
    result = prepare('2024-01-05,1200\n,,\n2024-02-05,1300')

    assert len(result['entries']) == 2
    assert not result['reasons']


def test_future_dated_rows_are_left_out(vault):
    """Same rule the add-entry row enforces with its date cap: the latest entry
    is what the account holds, so one dated ahead would pin it there."""
    result = prepare(f'2024-01-05,1200\n{TODAY},1300\n2099-01-01,999999')

    assert result['entries'] == [('1200', '2024-01-05'), ('1300', TODAY)]
    assert result['reasons']['future'] == 1


def test_a_file_already_imported_once_adds_nothing_the_second_time(vault):
    existing = [{'balance': '1200', 'recorded_at': '2024-01-05'},
                {'balance': '1340.5', 'recorded_at': '2024-02-05'}]

    result = prepare('2024-01-05,1200.00\n2024-02-05,1340.50', existing)

    assert result['entries'] == []
    assert result['reasons']['duplicate'] == 2


def test_a_row_repeated_inside_the_file_only_lands_once(vault):
    result = prepare('2024-01-05,1200\n2024-01-05,1200')

    assert result['entries'] == [('1200', '2024-01-05')]
    assert result['reasons']['duplicate'] == 1


def test_the_same_date_at_a_different_figure_is_not_a_duplicate(vault):
    """A correction, not a re-import."""
    existing = [{'balance': '1200', 'recorded_at': '2024-01-05'}]

    assert entries('2024-01-05,1250', existing) == [('1250', '2024-01-05')]


def test_an_unreadable_existing_entry_does_not_block_a_new_one(vault):
    """A history row this key cannot open is not something a new row can be
    judged a duplicate of."""
    existing = [{'balance': '⚠ unreadable', 'recorded_at': '2024-01-05'}]

    assert entries('2024-01-05,1200', existing) == [('1200', '2024-01-05')]


def test_the_row_limit_caps_what_one_import_can_add(vault):
    rows = '\n'.join(f'2024-01-05,{n}' for n in range(history_io.MAX_ENTRIES + 5))

    result = prepare(rows)

    assert len(result['entries']) == history_io.MAX_ENTRIES
    assert result['reasons']['limit'] == 5


def test_nothing_usable_reports_every_line(vault):
    result = prepare('hello\nthere')

    assert result['entries'] == []
    assert sum(result['reasons'].values()) == 2


# -- the batch write ----------------------------------------------------------

def stored_history(account_id):
    return [(entry['balance'], entry['recorded_at'])
            for entry in accounts_service.list_balance_history(account_id)]


def test_an_import_files_every_entry_and_moves_the_balance(vault):
    """The newest row says what the account holds now, exactly as a typed entry
    would — the import is not a separate kind of history."""
    account = make_account(current_balance=100)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    today = date.today().isoformat()

    added = accounts_service.add_balance_entries(
        account['id'], [('900', '2020-01-01'), ('1200', yesterday), ('1500', today)],
    )

    assert added == 3
    assert accounts_service.get_account(account['id'])['current_balance'] == 1500
    assert ('900', '2020-01-01') in stored_history(account['id'])


def test_an_older_import_leaves_the_current_balance_alone(vault):
    """Backfilling a decade of statements says what the account *was*."""
    account = make_account(current_balance=100)

    accounts_service.add_balance_entries(
        account['id'], [('900', '2020-01-01'), ('950', '2020-02-01')])

    assert accounts_service.get_account(account['id'])['current_balance'] == 100
    assert len(stored_history(account['id'])) == 3  # two imported, one from creation


def test_one_bad_row_leaves_the_account_exactly_as_it_was(vault):
    """All or nothing. A partial import is the state there is no way back out
    of short of deleting entries one at a time."""
    account = make_account(current_balance=100)
    before = snapshot_rows()

    with pytest.raises(ValueError):
        accounts_service.add_balance_entries(
            account['id'], [('900', '2020-01-01'), ('not a number', '2020-02-01')])

    assert snapshot_rows() == before


def test_an_unknown_account_writes_nothing(vault):
    before = snapshot_rows()

    with pytest.raises(accounts_service.AccountNotFound):
        accounts_service.add_balance_entries(9999, [('900', '2020-01-01')])

    assert snapshot_rows() == before


def test_imported_entries_are_encrypted_at_rest(vault):
    account = make_account()

    accounts_service.add_balance_entries(account['id'], [('900', '2020-01-01')])

    with database.connect() as db:
        stored = [row['balance'] for row in db.execute(
            "SELECT balance FROM balance_history WHERE account_id = ?", (account['id'],))]
    assert all(encryption.looks_encrypted(value) for value in stored)
    assert '900' not in stored


def test_an_empty_import_is_a_no_op(vault):
    account = make_account(current_balance=100)
    before = snapshot_rows()

    assert accounts_service.add_balance_entries(account['id'], []) == 0
    assert snapshot_rows() == before


def test_parse_then_write_is_the_whole_round_trip(vault):
    """What the dialog does: read the paste, hand the entries straight over."""
    account = make_account(current_balance=100)
    text = 'Date,Balance\n01/05/2020,1200.00\n02/05/2020,"1,340.50"'

    prepared = prepare(text, accounts_service.list_balance_history(account['id']))
    accounts_service.add_balance_entries(account['id'], prepared['entries'])

    # Newest first, so the two backfilled rows sit behind the entry the account
    # filed for its own opening balance today.
    assert stored_history(account['id'])[-2:] == [
        ('1340.5', '2020-02-05'), ('1200', '2020-01-05'),
    ]


# -- export -------------------------------------------------------------------

def exported(entries, noun='Balance'):
    return history_io.export_csv(entries, noun)


def rows_of(text):
    """The CSV back as a list of field lists, header included."""
    return [row for row in csv.reader(io.StringIO(text)) if row]


def test_an_export_holds_dates_and_figures_and_nothing_else(vault):
    """The whole point of the feature: this file leaves the vault as plaintext,
    so it carries the history and no field of the account it belongs to."""
    account = make_account(name='Bank', username='dave', password='s3cret',
                           account_number='12345', current_balance=1200)
    accounts_service.add_balance_entries(account['id'], [('900', '2020-01-01')])

    text = exported(accounts_service.list_balance_history(account['id']))['text']

    assert rows_of(text)[0] == ['Date', 'Balance']
    assert all(len(row) == 2 for row in rows_of(text))
    for secret in ('dave', 's3cret', '12345', 'Bank', 'Banking'):
        assert secret not in text


def test_an_export_is_written_oldest_first(vault):
    """list_balance_history hands them over newest first; a spreadsheet plots a
    series in the order it reads it."""
    entries = [{'balance': '1300', 'recorded_at': '2024-03-01'},
               {'balance': '1200', 'recorded_at': '2024-02-01'},
               {'balance': '1100', 'recorded_at': '2024-01-01'}]

    assert rows_of(exported(entries)['text'])[1:] == [
        ['2024-01-01', '1100'], ['2024-02-01', '1200'], ['2024-03-01', '1300'],
    ]


def test_the_column_is_named_for_the_category(vault):
    entries = [{'balance': '90000', 'recorded_at': '2024-01-01'}]

    assert rows_of(exported(entries, 'Annual Pay')['text'])[0] == ['Date', 'Annual Pay']


def test_a_timestamped_entry_exports_as_its_date(vault):
    """Entries written by a save carry a full stamp; the file wants the day."""
    entries = [{'balance': '1200', 'recorded_at': '2026-08-18T18:27:05'}]

    assert rows_of(exported(entries)['text'])[1] == ['2026-08-18', '1200']


def test_an_unreadable_entry_is_counted_and_left_out(vault):
    """Writing the '⚠ unreadable' placeholder would produce a spreadsheet that
    looks complete and does not add up."""
    entries = [{'balance': '1200', 'recorded_at': '2024-01-01'},
               {'balance': '⚠ unreadable', 'recorded_at': '2024-02-01'}]

    result = exported(entries)

    assert (result['written'], result['unreadable']) == (1, 1)
    assert 'unreadable' not in result['text']


def test_an_empty_history_writes_a_header_and_reports_nothing_written(vault):
    result = exported([])

    assert result['written'] == 0
    assert rows_of(result['text']) == [['Date', 'Balance']]


def test_a_negative_balance_cannot_be_read_as_a_formula(vault):
    """A leading minus is what a spreadsheet evaluates, and every liability
    balance in the vault starts with one."""
    entries = [{'balance': '-1200.5', 'recorded_at': '2024-01-01'}]

    text = exported(entries)['text']

    assert "'-1200.5" in text
    # And it still reads back as the number it was, apostrophe and all.
    assert history_io.parse_amount("'-1200.5") == -1200.5


def test_a_category_named_to_look_like_a_formula_is_defused_too(vault):
    """The noun comes from a category the user names, so the header is no more
    trusted than a value."""
    text = exported([], '=cmd|calc')['text']

    assert rows_of(text)[0] == ['Date', "'=cmd|calc"]


# -- the round trip -----------------------------------------------------------

def test_an_export_reads_back_into_the_entries_it_came_from(vault):
    """Both directions live in one module so this holds: what goes out comes
    back as the same days and the same figures."""
    original = [('900', '2020-01-01'), ('-1200.5', '2021-06-15'),
                ('1340.5', '2024-02-29'), ('90000', '2024-12-31')]

    text = exported([{'balance': b, 'recorded_at': d} for b, d in original])['text']
    reimported = history_io.prepare(text, today=TODAY)

    assert reimported['entries'] == original
    assert not reimported['reasons']


def test_an_export_reimported_into_the_same_account_adds_nothing(vault):
    """The file is the account's own history, so every row is already recorded.
    Round-tripping one back in must not double the chart."""
    account = make_account(current_balance=1200)
    accounts_service.add_balance_entries(
        account['id'], [('900', '2020-01-01'), ('1000', '2020-02-01')])
    entries = accounts_service.list_balance_history(account['id'])

    text = exported(entries)['text']
    # The real today, not the pinned one: the account filed an entry dated now
    # when it was created, and against a frozen TODAY that row reads as future.
    reimported = history_io.prepare(text, entries)

    assert reimported['entries'] == []
    assert reimported['reasons']['duplicate'] == len(entries)

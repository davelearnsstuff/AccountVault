# -----------------------------------------------------------------------------
# tests/test_account_import.py
# Reading a table of accounts back into the vault.
#
# The parsing half is pure text work and is tested without a vault. The writing
# half gets one, because create_accounts is all-or-nothing and that is a
# property about a transaction rather than about a list.
#
# The round trip is the test that matters most: this app's own CSV export has
# to import back into the same accounts it was written from, or the pair is two
# formats that merely resemble each other.
# -----------------------------------------------------------------------------

import pytest

from conftest import make_account
from services import account_import, csv_export
from services import accounts as accounts_service


# -- header mapping -----------------------------------------------------------

def test_a_file_with_no_name_column_is_refused_rather_than_guessed_at():
    """Without a header there is nothing to say which column is the password
    and which is the security answer, and a guess there files a secret under
    the wrong field rather than producing a visibly wrong chart."""
    result = account_import.prepare('Chase Visa,jsmith,hunter2\nAlly,bob,pw\n')

    assert result['has_header'] is False
    assert result['rows'] == []
    assert 'head the columns' in account_import.summarise(result)


@pytest.mark.parametrize('header, key', [
    ('Card Number', 'card_number'),
    ('card_number', 'card_number'),
    ('CARD-NUMBER', 'card_number'),
    ('Login URI', 'website'),
    ('url', 'website'),
    ('Title', 'name'),
    ('Folder', 'category'),
    ('Notes', 'notes'),
])
def test_a_heading_is_matched_by_its_key_its_label_or_a_known_alias(header, key):
    assert account_import.field_key(header) == key


@pytest.mark.parametrize('header', ['last_updated', 'Last Updated', 'id', 'date_added'])
def test_bookkeeping_columns_are_never_read_into_a_field(header):
    """last_updated drives the staleness half of Needs Attention. An imported
    record was last known good now, so reading the file's date would land a
    three-year-old export entirely inside Needs Attention on day one."""
    assert account_import.field_key(header) is None


def test_an_unnameable_column_is_dropped_and_named():
    result = account_import.prepare('name,mystery\nChase,something\n')

    assert result['rows'] == [{'name': 'Chase', 'category': 'Websites'}]
    assert "'mystery'" in account_import.columns_summary(result)


def test_a_field_claimed_twice_keeps_the_first_column():
    """Two columns both readable as the name means one of them is not the name,
    and only the user knows which — so the second is reported, not merged."""
    result = account_import.prepare('name,title\nReal Name,Other\n')

    assert result['rows'][0]['name'] == 'Real Name'
    assert result['ignored'] == ['title']


@pytest.mark.parametrize('delimiter', [',', '\t', ';', '|'])
def test_the_delimiter_that_names_the_most_columns_wins(delimiter):
    text = (delimiter.join(['name', 'username', 'password']) + '\n'
            + delimiter.join(['Chase Visa', 'jsmith', 'hunter2']) + '\n')
    result = account_import.prepare(text)

    assert result['rows'] == [{'name': 'Chase Visa', 'category': 'Websites',
                               'username': 'jsmith', 'password': 'hunter2'}]


def test_a_comma_inside_a_value_does_not_become_a_column():
    """'Smith, John' is one cell. Scoring layouts on how many columns they can
    name is what makes the separator that is really part of the data lose."""
    result = account_import.prepare('name|notes\nChase|Smith, John called\n')

    assert result['rows'][0]['notes'] == 'Smith, John called'


def test_a_quoted_value_may_span_lines():
    """An account's notes routinely hold newlines, and this app's own export
    writes them as a quoted field — so the whole document is parsed at once
    rather than a line at a time as the history importer does it."""
    result = account_import.prepare('name,notes\nChase,"first line\nsecond line"\n')

    assert result['rows'][0]['notes'] == 'first line\nsecond line'


# -- values -------------------------------------------------------------------

def test_the_formula_guard_apostrophe_is_taken_back_off():
    """export_io.defuse writes '=hunter2 so a spreadsheet shows the password
    rather than evaluating it. Read back as text the apostrophe is still there,
    and without this the account arrives with a password one character longer
    than the real one and simply does not log in."""
    result = account_import.prepare("name,password\nChase,'=hunter2\n")

    assert result['rows'][0]['password'] == '=hunter2'


def test_an_apostrophe_that_was_always_part_of_the_value_is_left_alone():
    result = account_import.prepare("name,password\nChase,'quoted\n")

    assert result['rows'][0]['password'] == "'quoted"


@pytest.mark.parametrize('cell, expected', [
    ('1', True), ('true', True), ('Yes', True), ('closed', True),
    ('0', False), ('false', False), ('no', False), ('', False),
])
def test_a_yes_no_column_is_read_as_one(cell, expected):
    result = account_import.prepare(f'name,closed\nChase,{cell}\n')

    assert bool(result['rows'][0].get('closed')) is expected


def test_an_unreadable_flag_leaves_the_account_open():
    """A 'Closed' column holding something this does not recognise must not
    close the account on a guess."""
    result = account_import.prepare('name,closed\nChase,perhaps\n')

    assert 'closed' not in result['rows'][0]


def test_a_row_with_no_category_takes_the_one_the_dialog_offers():
    result = account_import.prepare('name,category\nA,Banking\nB,\n',
                                    default_category='Secure Notes')

    assert [row['category'] for row in result['rows']] == ['Banking', 'Secure Notes']


def test_a_pre_rename_category_slug_still_lands_in_its_category():
    result = account_import.prepare('name,category\nChase,creditcards\n')

    assert result['rows'][0]['category'] == 'Credit Cards'


# -- rows that are dropped ----------------------------------------------------

def test_a_row_with_no_name_is_reported_rather_than_imported():
    result = account_import.prepare('name,username\n,jsmith\n')

    assert result['rows'] == []
    assert result['reasons']['no_name'] == 1
    assert 'no account name' in account_import.skipped_summary(result)


def test_importing_the_same_file_twice_adds_nothing_the_second_time():
    existing = [{'category': 'Banking', 'name': 'Ally Savings'}]
    result = account_import.prepare('name,category\nAlly Savings,Banking\n', existing)

    assert result['rows'] == []
    assert result['reasons']['duplicate'] == 1


def test_the_same_name_under_two_categories_is_two_accounts():
    existing = [{'category': 'Banking', 'name': 'Chase'}]
    result = account_import.prepare('name,category\nChase,Banking\nChase,Credit Cards\n',
                                    existing)

    assert [row['category'] for row in result['rows']] == ['Credit Cards']


def test_a_file_listing_one_account_twice_is_told_apart_from_a_second_import():
    """Different reasons because they mean different things: one says nothing
    was lost, the other says the file itself is worth a look."""
    result = account_import.prepare('name\nChase\nChase\n',
                                    existing=[{'category': 'Websites', 'name': 'Other'}])

    assert result['reasons']['repeat'] == 1
    assert result['reasons']['duplicate'] == 0


def test_the_row_limit_is_enforced_and_reported():
    rows = '\n'.join(f'Account {n}' for n in range(account_import.MAX_ROWS + 5))
    result = account_import.prepare('name\n' + rows + '\n')

    assert len(result['rows']) == account_import.MAX_ROWS
    assert result['reasons']['limit'] == 5


def test_too_much_text_is_refused_rather_than_parsed_five_times():
    with pytest.raises(account_import.AccountImportError):
        account_import.prepare('name\n' + 'x' * (account_import.MAX_FILE_BYTES + 1))


# -- writing ------------------------------------------------------------------

def test_imported_rows_are_stored_exactly_as_typed_ones_are(vault):
    rows = account_import.prepare(
        'name,category,username,password,current_balance\n'
        'Ally,Banking,jsmith,hunter2-long-enough,5000\n'
    )['rows']

    assert accounts_service.create_accounts(rows) == 1

    stored = accounts_service.list_accounts()[0]
    assert stored['name'] == 'Ally'
    assert stored['category'] == 'Banking'
    assert stored['password'] == 'hunter2-long-enough'
    # The category-specific field went into the encrypted blob, and the insert
    # filed the opening balance in history the same way create_account does.
    assert stored['current_balance'] == '5000'
    assert len(accounts_service.list_balance_history(stored['id'])) == 1


def test_an_import_lands_whole_or_not_at_all(vault):
    """A row that fails halfway through would otherwise leave the user unable
    to tell which of their accounts arrived without checking the file against
    the vault by hand — and re-running it would then double everything that
    did land."""
    rows = [{'name': 'Good One', 'category': 'Banking'},
            {'name': '   ', 'category': 'Banking'},
            {'name': 'Never Reached', 'category': 'Banking'}]

    with pytest.raises(ValueError):
        accounts_service.create_accounts(rows)

    assert accounts_service.list_accounts() == []


def test_an_empty_import_writes_nothing_and_says_so(vault):
    assert accounts_service.create_accounts([]) == 0
    assert accounts_service.list_accounts() == []


def test_the_vaults_own_export_imports_back_into_the_same_accounts(vault):
    """The pair is a round trip or it is two formats that merely resemble each
    other. Passwords with a leading '=' are in here on purpose: those are the
    ones the export escapes on the way out."""
    make_account(name='Chase Visa', category='Credit Cards', username='jsmith',
                 password='=starts-with-equals', website='https://chase.com',
                 card_number='4111111111111111')
    make_account(name='Ally Savings', category='Banking', username='bob',
                 password='another-long-password', notes='a note, with a comma')
    original = accounts_service.list_accounts()

    document = csv_export.export_csv(original)

    # Into a second vault's worth of rows: prepare is told the vault is empty,
    # so nothing is dropped as a duplicate of what it came from.
    rows = account_import.prepare(document, existing=[])['rows']
    assert len(rows) == 2

    by_name = {row['name']: row for row in rows}
    for account in original:
        restored = by_name[account['name']]
        assert restored['category'] == account['category']
        assert restored['password'] == account['password']
        assert restored.get('username') == account['username']
        if account.get('card_number'):
            assert restored['card_number'] == account['card_number']
        if account.get('notes'):
            assert restored['notes'] == account['notes']

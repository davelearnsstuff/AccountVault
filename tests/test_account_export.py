# -----------------------------------------------------------------------------
# tests/test_account_export.py
# One account written out as a plaintext CSV, at each of the three scopes.
#
# The scope is the whole security boundary of this feature. Two of these files
# carry the account's password and one carries dates and figures, and the only
# thing standing between them is which branch render() takes — so the tests
# that matter most here are the ones asserting what is *absent* from a file.
# -----------------------------------------------------------------------------

import csv
import io

import pytest

from conftest import make_account
from services import account_export
from services import accounts as accounts_service
from services import history_io

ALL = account_export.SCOPE_ALL
ACCOUNT = account_export.SCOPE_ACCOUNT
HISTORY = account_export.SCOPE_HISTORY

SECRETS = ('dave', 's3cret!', '4111111111111111', 'Chase Sapphire', 'chase.com')


@pytest.fixture
def card(vault):
    """A fully populated account, so a leak has something to leak."""
    account = make_account(
        name='Chase Sapphire', category='Credit Cards', username='dave',
        password='s3cret!', website='chase.com', card_number='4111111111111111',
        credit_limit=15000, current_balance=-1240.55, notes='the travel card',
    )
    accounts_service.add_balance_entries(
        account['id'], [('-900', '2020-01-01'), ('-1500.25', '2021-06-15')])
    return accounts_service.get_account(account['id'])


def render(account, scope, entries=None, noun='Balance'):
    if entries is None:
        entries = accounts_service.list_balance_history(account['id'])
    return account_export.render(account, entries, scope, noun)


def rows_of(text):
    return [row for row in csv.reader(io.StringIO(text)) if row]


# -- what each scope carries --------------------------------------------------

def test_history_only_carries_nothing_identifying_the_account(card):
    """The point of the scope. This file is the one that can be handed to a
    spreadsheet without handing over the account with it."""
    text = render(card, HISTORY)['text']

    for secret in SECRETS:
        assert secret not in text
    assert rows_of(text)[0] == ['Date', 'Balance']


def test_account_only_carries_the_fields_and_no_history(card):
    result = render(card, ACCOUNT)

    text = result['text']
    assert 's3cret!' in text and 'dave' in text
    assert '2020-01-01' not in text
    assert result['entries'] == 0
    assert rows_of(text)[0] == ['Field', 'Value']


def test_everything_carries_both_under_their_own_headings(card):
    result = render(card, ALL)

    rows = rows_of(result['text'])
    assert rows[0] == ['Account Details']
    assert ['Balance History'] in rows
    assert result['fields'] > 0 and result['entries'] == 3
    assert 's3cret!' in result['text'] and '2020-01-01' in result['text']


def test_fields_are_labelled_from_the_category_schema(card):
    """'Card Number', not 'card_number' — the file is for a person to read."""
    labels = [row[0] for row in rows_of(render(card, ACCOUNT)['text'])]

    assert 'Card Number' in labels
    assert 'Credit Limit' in labels
    assert 'card_number' not in labels


def test_the_category_and_closed_state_are_written_by_name(card):
    values = dict(row for row in rows_of(render(card, ACCOUNT)['text']) if len(row) == 2)

    assert values['Category'] == 'Credit Cards'
    assert values['Closed'] == 'No'


def test_a_key_outside_the_schema_is_still_written(vault):
    """An imported record keeps whatever key space it was written with, and a
    scope that says 'everything' has to mean it."""
    account = make_account(name='Odd', category='Websites', mystery_field='keep me')

    labels = dict(row for row in rows_of(render(account, ACCOUNT)['text']) if len(row) == 2)

    assert labels.get('mystery_field') == 'keep me'


def test_date_added_stays_out_of_every_scope(card):
    """It is recorded but never shown anywhere in the app; an export is not the
    place to introduce a field the UI has decided not to have."""
    for scope in (ALL, ACCOUNT, HISTORY):
        assert 'date_added' not in render(card, scope)['text']


# -- formula neutralising -----------------------------------------------------

def test_a_field_that_looks_like_a_formula_is_defused(vault):
    """Account fields are free text, so this is where a crafted value would
    ride into a spreadsheet."""
    account = make_account(name='Odd', category='Websites', notes='=cmd|/c calc')

    assert "'=cmd|/c calc" in render(account, ACCOUNT)['text']


def test_a_negative_balance_in_the_details_is_defused_too(card):
    assert "'-1240.55" in render(card, ACCOUNT)['text']


# -- the combined file still gives its history back ---------------------------

def test_the_history_inside_a_combined_file_still_imports(card):
    """The account rows cannot be read as history and are reported as skipped,
    but the history section survives being carried alongside them."""
    text = render(card, ALL)['text']

    back = history_io.prepare(text)

    assert len(back['entries']) == 3
    assert back['reasons']['duplicate'] == 0


def test_a_history_only_file_is_exactly_what_the_importer_expects(card):
    """No section heading on a single-section file, so the round trip stays
    clean — nothing is skipped on the way back in."""
    text = render(card, HISTORY)['text']

    back = history_io.prepare(text)

    assert len(back['entries']) == 3
    assert not back['reasons']


# -- scopes and naming --------------------------------------------------------

def test_only_the_history_scope_is_free_of_credentials():
    assert not account_export.includes_credentials(HISTORY)
    assert account_export.includes_credentials(ALL)
    assert account_export.includes_credentials(ACCOUNT)


def test_the_scope_list_is_worded_for_the_category():
    keys = [scope['key'] for scope in account_export.scopes('Annual Pay')]
    labels = [scope['label'] for scope in account_export.scopes('Annual Pay')]

    assert keys == [ALL, ACCOUNT, HISTORY]
    assert 'Annual Pay history only' in labels
    # The flag the dialog's warning is driven by has to match the scope itself.
    for scope in account_export.scopes():
        assert scope['credentials'] == account_export.includes_credentials(scope['key'])


@pytest.mark.parametrize('scope, expected', [
    (ALL, 'Chase Sapphire-export-'),
    (ACCOUNT, 'Chase Sapphire-details-'),
    (HISTORY, 'Chase Sapphire-balance-history-'),
])
def test_the_filename_says_which_of_the_three_files_this_is(scope, expected):
    assert account_export.default_name('Chase Sapphire', scope).startswith(expected)


@pytest.mark.parametrize('account_name, expected', [
    ('a/b', 'a_b'),          # a separator must not propose a directory
    ('..', 'account'),       # nor may the name walk up out of one
    ('', 'account'),
    (None, 'account'),
])
def test_the_filename_cannot_be_steered_by_the_account_name(account_name, expected):
    assert account_export.default_name(account_name, ALL).startswith(expected + '-')


def test_an_account_with_no_history_still_exports_its_details(vault):
    account = make_account(name='Site', category='Websites', username='dave')

    result = render(account, ACCOUNT, entries=[])

    assert result['fields'] > 0
    assert 'dave' in result['text']

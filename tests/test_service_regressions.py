# -----------------------------------------------------------------------------
# tests/test_service_regressions.py
# Cross-cutting regressions in services/ — the faults that spanned more than one
# module, and so have no single topic file to live in.
#
# Each test names the behaviour that was wrong, not the function that changed,
# so a later refactor that reintroduces the fault fails here regardless of where
# the code has moved to.
# -----------------------------------------------------------------------------

import datetime
import json

import pytest

from core import database
from core import encryption
from conftest import corrupt, corrupt_column, make_account, raw_column
from services import account_export, accounts as accounts_service
from services import csv_export as backup_service
from services import dashboard as dashboard_service
from services import history_io


def today() -> str:
    return datetime.date.today().isoformat()


# -- CSV export ---------------------------------------------------------------

def test_csv_export_omits_date_added_and_the_row_id(vault):
    """account_export excludes date_added on the grounds that the UI never shows
    it; the vault CSV used to contradict that, and shipped the row id too."""
    make_account(current_balance=100)

    text = backup_service.export_csv(accounts_service.list_accounts())
    header = text.splitlines()[0].split(',')

    assert 'date_added' not in header
    assert 'id' not in header
    assert 'name' in header and 'password' in header


def test_every_csv_write_path_neutralizes_formulas(vault):
    """The mitigation has to hold on all three writers, headers included."""
    account = make_account(name='=cmd|calc', notes='@SUM(A1)', current_balance=5)

    vault_csv = backup_service.export_csv(accounts_service.list_accounts())
    assert "'=cmd|calc" in vault_csv and "'@SUM(A1)" in vault_csv

    single = account_export.render(
        accounts_service.get_account(account['id']), [], account_export.SCOPE_ACCOUNT)
    assert "'=cmd|calc" in single['text']

    history = history_io.export_csv([{'balance': '-5', 'recorded_at': '2024-01-01'}],
                                    noun='=Balance')
    assert "'=Balance" in history['text'] and "'-5" in history['text']


def test_a_defused_history_export_still_imports(vault):
    """The leading apostrophe must not break the round trip it protects."""
    written = history_io.export_csv([
        {'balance': '-1200.5', 'recorded_at': '2024-01-05'},
        {'balance': '300', 'recorded_at': '2024-02-05'},
    ])
    read_back = history_io.prepare(written['text'])

    assert read_back['entries'] == [('-1200.5', '2024-01-05'), ('300', '2024-02-05')]


def test_render_counts_the_accounts_fields_not_the_rows_written(vault):
    """'Category' and 'Closed' are added by name and were being counted as
    fields of the record."""
    account = make_account()
    rows = account_export.account_rows(accounts_service.get_account(account['id']))
    result = account_export.render(
        accounts_service.get_account(account['id']), [], account_export.SCOPE_ACCOUNT)

    assert result['fields'] == len(rows) - 2


def test_render_counts_fields_that_would_not_decrypt(vault):
    account = make_account(password='hunter2hunter2')
    corrupt_column(account['id'], 'password')

    result = account_export.render(
        accounts_service.get_account(account['id']), [], account_export.SCOPE_ACCOUNT)

    assert result['unreadable_fields'] == 1


# -- balance history ----------------------------------------------------------

def test_deleting_the_last_entry_clears_the_accounts_figure(vault):
    """'Delete the entry and the account drops to the one before it' was false
    when there was no entry before it — the account kept the deleted figure."""
    account = make_account(current_balance=100)
    entries = accounts_service.list_balance_history(account['id'])
    assert len(entries) == 1

    accounts_service.delete_balance_entry(account['id'], entries[0]['id'])

    assert not accounts_service.get_account(account['id']).get('current_balance')


def test_deleting_an_entry_that_is_not_there_is_an_error(vault):
    account = make_account(current_balance=100)

    with pytest.raises(accounts_service.AccountNotFound):
        accounts_service.delete_balance_entry(account['id'], 999999)


def test_future_only_history_does_not_clear_the_figure(vault):
    """The companion to the two above: entries exist, none of them current."""
    account = make_account(current_balance=100)
    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    accounts_service.add_balance_entry(account['id'], '999', future)
    entries = accounts_service.list_balance_history(account['id'])
    original = next(e for e in entries if e['recorded_at'][:10] == today())

    accounts_service.delete_balance_entry(account['id'], original['id'])

    assert accounts_service.get_account(account['id'])['current_balance'] == 100


def test_an_edit_that_does_not_move_the_balance_files_no_history_row(vault):
    """Every save used to force a snapshot, so editing a note claimed the
    balance had been re-read and grew a column of identical rows."""
    account = make_account(current_balance=100)
    before = len(accounts_service.list_balance_history(account['id']))

    edit = dict(accounts_service.get_account(account['id']))
    edit['notes'] = 'changed something unrelated'
    accounts_service.update_account(account['id'], edit)

    assert len(accounts_service.list_balance_history(account['id'])) == before


def test_an_edit_that_moves_the_balance_still_files_one(vault):
    account = make_account(current_balance=100)
    before = len(accounts_service.list_balance_history(account['id']))

    edit = dict(accounts_service.get_account(account['id']))
    edit['current_balance'] = 250
    accounts_service.update_account(account['id'], edit)

    assert len(accounts_service.list_balance_history(account['id'])) == before + 1
    assert accounts_service.get_account(account['id'])['current_balance'] == 250


def test_a_backdated_save_is_always_filed(vault):
    """Even when the figure matches the one already on file: the date is the
    point of a backdated entry."""
    account = make_account(current_balance=100)
    before = len(accounts_service.list_balance_history(account['id']))

    edit = dict(accounts_service.get_account(account['id']))
    edit['current_balance'] = 100
    accounts_service.update_account(account['id'], edit, balance_date='2021-06-01')

    entries = accounts_service.list_balance_history(account['id'])
    assert len(entries) == before + 1
    assert any(e['recorded_at'] == '2021-06-01' for e in entries)


@pytest.mark.parametrize('bad', ['06/01/2021', 'yesterday', '2021-13-45', ''])
def test_an_unparseable_balance_date_is_refused(vault, bad):
    """It went straight into balance_history.recorded_at, which the whole app
    orders and slices on."""
    account = make_account(current_balance=100)
    edit = dict(accounts_service.get_account(account['id']))

    with pytest.raises(ValueError):
        accounts_service.update_account(account['id'], edit, balance_date=bad)


def test_bulk_update_refuses_a_column_it_does_not_own(vault):
    """The column name is interpolated into the statement, so it is checked
    rather than resting on today's callers passing a literal."""
    account = make_account()

    with pytest.raises(ValueError):
        accounts_service._bulk_update([account['id']], 'password', 'x')


# -- dashboard ----------------------------------------------------------------

def _set_category(account_id: int, category: str):
    """Writes a raw category string, including a pre-rename slug."""
    with database.connect() as db:
        db.execute("UPDATE accounts SET category = ? WHERE id = ?", (category, account_id))
    accounts_service.invalidate_cache()


def test_a_legacy_slug_credit_card_counts_as_a_liability(vault):
    """'creditcards' matched neither LIABILITY_CATEGORIES nor the excluded set,
    so a legacy card was *added* to net worth instead of subtracted."""
    account = make_account(category='Credit Cards', current_balance=500)
    _set_category(account['id'], 'creditcards')

    summary = dashboard_service.monthly_summary()

    assert summary['net_worth'][-1] == -500.0


def test_a_legacy_slug_secure_note_is_still_excluded(vault):
    account = make_account(category='Secure Notes', current_balance=500)
    _set_category(account['id'], 'securenotes')

    assert dashboard_service.monthly_summary() == {
        'months': [], 'categories': {}, 'net_worth': [], 'accounts': {}}


def test_websites_is_excluded_from_net_worth(vault):
    """It has no balance field, so a current_balance on one is a stray from a
    move or an import — it used to be counted as an asset."""
    make_account(category='Websites', current_balance=500)

    assert dashboard_service.monthly_summary()['net_worth'] == []


def test_an_unreadable_history_row_does_not_open_the_chart_early(vault):
    """The month range was built from all entries, including ones that then
    failed to decrypt, leaving a run of empty leading months."""
    account = make_account(current_balance=100)
    accounts_service.add_balance_entry(account['id'], '50', '2020-01-15')

    with database.connect() as db:
        row = db.execute(
            "SELECT id, balance FROM balance_history WHERE recorded_at LIKE '2020-01%'"
        ).fetchone()
        db.execute("UPDATE balance_history SET balance = ? WHERE id = ?",
                   (corrupt(row['balance']), row['id']))

    summary = dashboard_service.monthly_summary()

    assert summary['months'][0] == today()[:7]


def test_a_malformed_recorded_at_does_not_take_the_dashboard_down(vault):
    account = make_account(current_balance=100)
    with database.connect() as db:
        db.execute("UPDATE balance_history SET recorded_at = 'whenever' WHERE account_id = ?",
                   (account['id'],))

    assert dashboard_service.monthly_summary()['net_worth'] == []


# -- history import parsing ---------------------------------------------------

def test_a_decimal_comma_survives_a_currency_symbol(vault):
    """'1200,00' was handled; '€1200,00' — how the statements that write a
    decimal comma actually spell it — arrived a hundred times too big."""
    assert history_io.parse_amount('1200,00') == 1200.0
    assert history_io.parse_amount('€1200,00') == 1200.0
    assert history_io.parse_amount('$1,200.00') == 1200.0
    assert history_io.parse_amount('1,200') == 1200.0


def test_a_data_row_with_an_unreadable_date_is_reported_not_swallowed(vault):
    """It was silently consumed as a header row, with no entry in skipped.

    'Q1 close' names no column and holds no readable date, but the row does
    carry an amount — so it is data this module could not read, not a header.
    A first row that *does* name a column ('Opening Balance') is still treated
    as one; see the test below."""
    result = history_io.prepare(
        'Q1 close,1200.00\n2024-01-05,1300.00\n2024-02-05,1400.00\n')

    assert len(result['entries']) == 2
    assert result['reasons']['no_date'] == 1
    assert any(row['line'] == 1 for row in result['skipped'])


def test_a_real_header_row_is_still_consumed(vault):
    result = history_io.prepare(
        'Statement Date,Ending Balance\n2024-01-05,1300.00\n2024-02-05,1400.00\n')

    assert len(result['entries']) == 2
    assert not result['skipped']
    assert result['amount_column'] == 'Ending Balance'


def test_a_pasted_wall_of_text_is_refused_rather_than_parsed_five_times(vault):
    with pytest.raises(history_io.HistoryImportError):
        history_io.prepare('x' * (history_io.MAX_FILE_BYTES + 1))


# -- category helpers ---------------------------------------------------------

def test_a_split_scheme_cannot_smuggle_javascript_past_the_url_check(vault):
    from core.urls import normalize_url

    assert normalize_url('javascript:alert(1)') == ''
    assert normalize_url('java\tscript:alert(1)') == ''
    assert normalize_url('java\nscript:alert(1)') == ''
    assert normalize_url('JaVaScRiPt:alert(1)') == ''
    assert normalize_url('%6aavascript:alert(1)') == ''


def test_ordinary_urls_are_left_alone(vault):
    from core.urls import normalize_url

    assert normalize_url('https://example.com') == 'https://example.com'
    assert normalize_url('example.com') == 'https://example.com'
    assert normalize_url('example.com:8080/path') == 'https://example.com:8080/path'
    assert normalize_url('mailto:a@b.com') == 'mailto:a@b.com'


def test_the_due_cycle_schemas_use_the_declared_key(vault):
    """The constant existed to stop the literal being written twice, and the
    schemas wrote it anyway."""
    from core.categories import CATEGORY_CONFIG, DUE_CYCLE_KEY

    for name in ('Credit Cards', 'Loans', 'Utilities'):
        keys = [f['key'] for f in CATEGORY_CONFIG[name]['fields']]
        assert DUE_CYCLE_KEY in keys


def test_one_spelling_of_a_whole_number_everywhere(vault):
    from core.categories import trim_number

    assert history_io.canonical_amount(5.0) == '5'
    assert history_io.canonical_amount(5.25) == '5.25'
    assert trim_number(5.0) == 5

    account = make_account(current_balance=100)
    accounts_service.add_balance_entry(account['id'], '250.0', today())
    assert accounts_service.get_account(account['id'])['current_balance'] == 250


# -- view bookkeeping must not reach the database -----------------------------
#
# The views flatten their own keys onto a row — '_category' is the normalized
# category it was rendered under, '_match' is which field a search hit. Quick
# Update saves a copy of the row on screen, so editing a *search result* wrote
# the match dict into extra_data. split_flat named the keys to drop one at a
# time and '_match' was not among them.

def test_saving_a_row_carrying_view_bookkeeping_does_not_store_it(vault):
    account = make_account(category='Banking', name='Chequing', current_balance=100)

    # Exactly what MainWindow._quick_update builds: the flattened row plus the
    # dialog's changes, with no filtering of its own.
    row = dict(accounts_service.get_account(account['id']))
    row['_category'] = 'Banking'
    row['_match'] = {'key': 'username', 'label': 'Username', 'type': 'text'}
    row['current_balance'] = 250

    accounts_service.update_account(account['id'], row)

    stored = json.loads(encryption.decrypt(raw_column(account['id'], 'extra_data'),
                                          accounts_service.EXTRA_AAD))
    assert '_match' not in stored
    assert '_category' not in stored
    assert stored['current_balance'] == 250

    # And nothing underscored comes back out on a read either.
    fresh = accounts_service.get_account(account['id'])
    assert not [key for key in fresh if key.startswith('_')]


def test_split_flat_drops_every_underscored_key(vault):
    """Asserted on the prefix rather than on the two keys in use today: the
    point of the fix is that a view inventing a new one cannot reach the
    database without anyone remembering to add it here."""
    columns, extra = accounts_service.split_flat({
        'name': 'Keep', 'category': 'Banking', 'current_balance': 5,
        '_category': 'Banking', '_match': {'key': 'name'}, '_anything_new': 1,
        'id': 9, 'date_added': '2020-01-01',
    })

    assert columns == {'name': 'Keep', 'category': 'Banking'}
    assert extra == {'current_balance': 5}


def test_a_record_already_carrying_bookkeeping_sheds_it_on_the_next_save(vault):
    """No migration ships for vaults polluted before the fix, because none is
    needed: extra_data is rewritten wholesale from the flat dict on every save,
    and split_flat now drops the key on the way through."""
    account = make_account(category='Banking', name='Polluted')
    polluted = dict(accounts_service.get_account(account['id']))
    polluted['_match'] = {'key': 'name'}

    # Write it the way the old code would have, straight past split_flat.
    with database.connect() as db:
        db.execute("UPDATE accounts SET extra_data = ? WHERE id = ?",
                   (accounts_service._encrypt_extra({'_match': {'key': 'name'},
                                                     'current_balance': 7}),
                    account['id']))
    accounts_service.invalidate_cache()
    assert '_match' in json.loads(
        encryption.decrypt(raw_column(account['id'], 'extra_data'),
                           accounts_service.EXTRA_AAD))

    accounts_service.update_account(
        account['id'], dict(accounts_service.get_account(account['id'])))

    after = json.loads(encryption.decrypt(raw_column(account['id'], 'extra_data'),
                                          accounts_service.EXTRA_AAD))
    assert '_match' not in after
    assert after['current_balance'] == 7   # the real field survived the cleanup

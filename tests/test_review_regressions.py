# -----------------------------------------------------------------------------
# tests/test_review_regressions.py
# One test per bug found in the full-codebase review, each reproducing the
# failure it was reported for. Companion to test_service_regressions.py, which
# covers the same ground for the service layer alone — these reach into ui/ too,
# because that is where several of the findings lived.
#
# A handful are deliberately controls: they assert behaviour the fix had to
# leave alone (a future date still refused on an ordinary entry, the excepthook
# still printing when it has somewhere to print to). They pass either way, and
# that is the point.
# -----------------------------------------------------------------------------

import datetime
import sys

import pytest
from PySide6.QtTest import QTest

import database
from conftest import _destroy, corrupt, make_account
from services import accounts as accounts_service



@pytest.fixture
def window(vault, qt_app):
    """A MainWindow against the throwaway vault, already unlocked — the same
    fixture test_ui.py defines."""
    from ui.main_window import MainWindow

    win = MainWindow()
    win._enter_vault('test')
    yield win
    _destroy(win, qt_app)


def type_into(widget, text: str):
    """Types into a form input the way a user does.

    A plain setText() does not mark the form dirty on purpose: every input is
    wired to textEdited, so populating the form from a stored record cannot make
    it look edited. Only real keystrokes count, so the tests below send them."""
    widget.clear()
    QTest.keyClicks(widget, text)


def days_ago(n: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


def days_ahead(n: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=n)).isoformat()


# -- 1. _record_balance compared against the last-inserted row -----------------

def test_a_save_after_a_backdated_entry_files_no_duplicate(vault):
    account = make_account(category='Banking', name='Bank', current_balance=100)
    flat = accounts_service.get_account(account['id'])

    # A backdated statement: older date, but the newest row id.
    accounts_service.update_account(
        account['id'], dict(flat, current_balance=50), balance_date=days_ago(60))
    before = accounts_service.list_balance_history(account['id'])
    assert len(before) == 2
    assert accounts_service.get_account(account['id'])['current_balance'] == 100

    # An ordinary save touching only the notes. The balance has not moved.
    current = accounts_service.get_account(account['id'])
    accounts_service.update_account(account['id'], dict(current, notes='just a note'))

    after = accounts_service.list_balance_history(account['id'])
    assert len(after) == len(before), 'a phantom history row was filed'


# -- 2. extending the selection destroyed unsaved edits -----------------------

def test_extending_the_selection_prompts_instead_of_discarding(window, qt_app, monkeypatch):
    from ui.account_list import ALL

    make_account(name='First Account')
    make_account(name='Second Account')
    window.load_category(ALL)
    window._start_edit(window.accounts[0])
    type_into(window.form._inputs['password'], 'typed but never saved')
    assert window.form.is_dirty()

    prompted = []
    monkeypatch.setattr(window, '_show_dirty_prompt', lambda: prompted.append(True))

    window.account_list.blockSignals(True)
    for index in range(window.account_list.count()):
        window.account_list.item(index).setSelected(True)
    window.account_list.blockSignals(False)
    window._on_selection_changed()

    assert prompted == [True]
    assert window.form is not None
    assert window.form._inputs['password'].text() == 'typed but never saved'
    assert window._pending_navigation[0] == 'bulk'


# -- 3. Ctrl+E discarded unsaved edits ----------------------------------------

def test_ctrl_e_on_a_dirty_form_prompts_instead_of_discarding(window, qt_app, monkeypatch):
    from ui.account_list import ALL

    account = make_account(name='Bank', username='olduser')
    window.load_category(ALL)
    window._select_account(account['id'], 'Banking')
    window._start_edit(window.accounts[0])
    type_into(window.form._inputs['username'], 'typed but never saved')
    assert window.form.is_dirty()

    prompted = []
    monkeypatch.setattr(window, '_show_dirty_prompt', lambda: prompted.append(True))

    window._edit_selected()          # Ctrl+E

    assert prompted == [True]
    assert window.form is not None
    assert window.form._inputs['username'].text() == 'typed but never saved'
    assert window._pending_navigation[0] == 'edit'


# -- 4. editing a future-dated entry rewrote its date -------------------------

def test_editing_a_future_dated_entry_keeps_its_date(qt_app):
    from ui.dialogs import BalanceEntryDialog

    future = days_ahead(200)
    dialog = BalanceEntryDialog(None, '900', future)
    dialog._amount.setText('950')      # correcting a typo in the amount only

    balance, recorded_at = dialog.values()

    assert balance == '950'
    assert recorded_at == future, 'the date was silently clamped to today'


def test_an_ordinary_entry_still_cannot_be_dated_into_the_future(qt_app):
    from PySide6.QtCore import QDate
    from ui.dialogs import BalanceEntryDialog

    dialog = BalanceEntryDialog(None, '900', days_ago(30))
    dialog._date.setDate(QDate.currentDate().addDays(30))

    assert dialog.values()[1] == datetime.date.today().isoformat()


# -- 5. Date Due sorted on the anchor, not the displayed date -----------------

def test_due_date_sort_follows_the_date_the_row_displays(window, qt_app):
    from categories import DUE_CYCLE_KEY, MONTHLY, next_due_date
    from ui.account_list import ALL

    window.current_category = ALL
    window.sort_key = 'due_date'
    window.sort_descending = False

    # The anchor is months back; what the row displays is the next occurrence of
    # its day of the month. The one-off is placed in the gap between the two —
    # the window where the stored value and the displayed one disagree, which is
    # the whole of the bug. Derived from the occurrence rather than hardcoded so
    # the gap exists whatever today's date is.
    anchor = days_ago(150)
    occurrence = next_due_date(anchor, MONTHLY)
    one_off = (datetime.date.fromisoformat(occurrence)
               - datetime.timedelta(days=1)).isoformat()
    assert anchor < one_off < occurrence, 'the test data misses the gap'

    window.accounts = [
        {'id': 1, 'name': 'Monthly Card', '_category': 'Credit Cards',
         'due_date': anchor, DUE_CYCLE_KEY: MONTHLY},
        {'id': 2, 'name': 'One-off Bill', '_category': 'Bills',
         'due_date': one_off},
    ]

    order = [a['name'] for a in window._sorted_accounts()]
    shown = [next_due_date(a.get('due_date'), a.get(DUE_CYCLE_KEY))
             for a in window._sorted_accounts()]

    assert shown == sorted(shown), 'the visible due dates are out of order'
    assert order == ['One-off Bill', 'Monthly Card']


# -- 7. list_accounts(category) skipped normalize_category --------------------

def test_a_pre_rename_slug_is_found_by_a_filtered_listing(vault):
    account = make_account(category='creditcards', name='Old Card')
    accounts_service.invalidate_cache()

    found = accounts_service.list_accounts('Credit Cards')

    assert [a['id'] for a in found] == [account['id']]


# -- 8. a search left the tab counts stale ------------------------------------

def test_a_search_keeps_the_tab_counts_current(window, qt_app):
    from ui.account_list import ALL

    account = make_account(name='Alpha Bank', category='Banking')
    make_account(name='Beta Card', category='Credit Cards')
    window.load_category(ALL)
    assert (window.open_count, window.closed_count) == (2, 0)

    window.search_query = 'alpha'
    window._run_search()
    assert [a['name'] for a in window.accounts] == ['Alpha Bank']

    # Close the matched account from inside the results, as a save would.
    stored = accounts_service.get_account(account['id'])
    accounts_service.update_account(account['id'], dict(stored, closed=True))
    window._run_search(keep_selection=True)

    assert (window.open_count, window.closed_count) == (1, 1)
    assert window.category_counts.get('Banking') is None
    assert window.total_count == 2


# -- 9. the excepthook crashed when sys.stderr was None -----------------------

def test_the_excepthook_survives_a_missing_stderr(monkeypatch):
    import main as main_module

    monkeypatch.setattr(sys, 'excepthook', sys.__excepthook__)
    main_module._install_excepthook()
    monkeypatch.setattr(sys, 'stderr', None)      # what pythonw.exe gives us

    try:
        raise ValueError('boom')
    except ValueError:
        sys.excepthook(*sys.exc_info())           # must not raise


def test_the_excepthook_still_prints_a_traceback(monkeypatch):
    import io

    import main as main_module

    monkeypatch.setattr(sys, 'excepthook', sys.__excepthook__)
    main_module._install_excepthook()
    stream = io.StringIO()
    monkeypatch.setattr(sys, 'stderr', stream)

    try:
        raise ValueError('boom')
    except ValueError:
        sys.excepthook(*sys.exc_info())

    assert 'ValueError: boom' in stream.getvalue()


# -- 10. the undecryptable-entry toast fired on every reload ------------------

def test_the_unreadable_entry_notice_is_not_repeated(vault, qt_app):
    from ui.history_table import BalanceHistory

    account = make_account(category='Banking', name='Bank')
    for offset, amount in ((30, '100'), (20, '200'), (10, '300')):
        accounts_service.add_balance_entry(account['id'], amount, days_ago(offset))

    with database.connect() as db:
        row = db.execute(
            "SELECT id, balance FROM balance_history WHERE account_id = ? "
            "ORDER BY id LIMIT 1", (account['id'],)).fetchone()
        db.execute("UPDATE balance_history SET balance = ? WHERE id = ?",
                   (corrupt(row['balance']), row['id']))

    table = BalanceHistory(account['id'], 'dark')
    seen = []
    table.error.connect(seen.append)

    table.reload()
    table.reload()
    table.reload()

    assert len(seen) == 1, f'the notice fired {len(seen)} times'
    assert 'could not be decrypted' in seen[0]


# -- 11. get_settings hardcoded the key list a second time --------------------

def test_a_setting_added_to_the_defaults_is_read_back(vault, monkeypatch):
    import preferences
    from database import write_config

    monkeypatch.setitem(preferences._SETTING_DEFAULTS, 'show_tooltips', 'off')
    write_config('show_tooltips', 'on')

    assert preferences.get_settings()['show_tooltips'] == 'on'


# -- 12. export decrypted the whole vault to take a len() ---------------------

def test_counting_accounts_decrypts_nothing(vault, monkeypatch):
    make_account(name='One', password='p@ssword-long-enough')
    make_account(name='Two', password='p@ssword-long-enough')
    accounts_service.invalidate_cache()

    calls = []
    real = accounts_service.safe_decrypt
    monkeypatch.setattr(accounts_service, 'safe_decrypt',
                        lambda value: calls.append(value) or real(value))

    assert accounts_service.count_accounts() == 2
    assert calls == []


def test_the_csv_export_counts_without_decrypting(window, qt_app, monkeypatch):
    from services import csv_export as bk

    make_account(name='One')
    listed = []
    monkeypatch.setattr(accounts_service, 'list_accounts',
                        lambda *a, **k: listed.append(True) or [])
    monkeypatch.setattr(bk, 'default_csv_name', lambda: 'x.csv')
    # Refuse the file dialog, so only the pre-dialog work runs.
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *a, **k: ('', '')))
    from ui import dialogs
    monkeypatch.setattr(dialogs.CsvWarningDialog, 'exec',
                        lambda self: dialogs.CsvWarningDialog.Accepted)

    window._export_csv()

    assert listed == [], 'the export decrypted the vault just to count it'


# -- 13. every save rendered the account list twice ---------------------------

def test_saving_renders_the_list_once(window, qt_app, monkeypatch):
    from ui.account_list import ALL

    account = make_account(name='Bank')
    window.load_category(ALL)
    window._start_edit(window.accounts[0])
    type_into(window.form._inputs['name'], 'Renamed Bank')

    renders = []
    real = window.render_list
    monkeypatch.setattr(window, 'render_list',
                        lambda: renders.append(True) or real())

    window.form.submit()

    assert len(renders) == 1, f'the list was rebuilt {len(renders)} times'
    assert window.selected_id == account['id']
    assert [a['name'] for a in window.accounts] == ['Renamed Bank']


def test_saving_inside_a_search_still_highlights_the_saved_row(window, qt_app):
    account = make_account(name='Alpha Bank')
    make_account(name='Beta Card')
    window.search_query = 'alpha'
    window._run_search()
    window._start_edit(window.accounts[0])
    type_into(window.form._inputs['username'], 'someone')

    window.form.submit()

    assert window.selected_id == account['id']
    selected = [window.account_list.item(i)
                for i in range(window.account_list.count())
                if window.account_list.item(i).isSelected()]
    assert [i.data(0x0100) for i in selected] == [account['id']]

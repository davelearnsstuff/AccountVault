# -----------------------------------------------------------------------------
# tests/test_ui.py
# The parts of the Qt layer that handle stored fields or secrets directly.
#
# Most of ui/ is deliberately untested, on the grounds that its failures are
# cosmetic. These are the exceptions: the account form silently destroyed
# stored fields on save, and locking the vault left a copied password on the
# clipboard. Both are data/secret handling that happens to live in a widget, so
# they get the same treatment as the services.
#
# Everything here drives widgets directly rather than synthesising input events.
# -----------------------------------------------------------------------------

import pytest

from PySide6.QtWidgets import QLineEdit

from conftest import _destroy, make_account
from services import accounts as accounts_service


@pytest.fixture
def form_class(qt_app):
    from ui.account_form import AccountForm
    return AccountForm


# -- the account form must not destroy what it cannot display -----------------

def test_saving_an_unchanged_form_keeps_fields_outside_the_schema(vault, form_class):
    """The form builds inputs only for the current category's schema, and the
    save path takes the flat dict as the whole truth. A Websites account holding
    card_number — from an import, or from being moved out of Credit Cards — used
    to lose it on a save that changed nothing."""
    account = make_account(
        category='Websites', name='Moved Card', password='p@ssword-long-enough',
        card_number='4111111111111111', cardholder_name='A Person',
        current_balance=900,
    )
    stored = accounts_service.get_account(account['id'])

    form = form_class(dict(stored), 'Websites')
    accounts_service.update_account(account['id'], form.values())

    after = accounts_service.get_account(account['id'])
    assert after['card_number'] == '4111111111111111'
    assert after['cardholder_name'] == 'A Person'
    assert after['current_balance'] == 900


def test_the_form_still_edits_and_clears_its_own_fields(vault, form_class):
    """The companion to the test above: carrying values forward must not make
    an edited field un-editable or a cleared one un-clearable."""
    account = make_account(category='Banking', name='Old Name',
                           username='olduser', notes='keep me')
    form = form_class(dict(accounts_service.get_account(account['id'])), 'Banking')

    form._inputs['name'].setText('New Name')
    form._inputs['username'].setText('')

    values = form.values()
    assert values['name'] == 'New Name'
    assert values['username'] is None

    accounts_service.update_account(account['id'], values)
    after = accounts_service.get_account(account['id'])
    assert after['name'] == 'New Name'
    assert after['username'] is None
    assert after['notes'] == 'keep me'


def test_the_form_never_writes_view_bookkeeping_into_the_record(vault, form_class):
    """'_category' and '_match' are flattened onto rows by the views. Carrying
    the record forward must not route them into extra_data as if they were
    fields of the account."""
    account = make_account(category='Banking')
    stored = dict(accounts_service.get_account(account['id']))
    stored['_category'] = 'Banking'
    stored['_match'] = {'key': 'name'}

    values = form_class(stored, 'Banking').values()

    assert '_category' not in values and '_match' not in values
    assert 'date_added' not in values


def test_switching_category_keeps_what_was_already_typed(vault, form_class):
    """The schemas overlap on name, username, password, website and notes, so
    a switch used to empty five filled-in fields with nothing said."""
    form = form_class(None, None)
    # Whichever category the picker opens on — switching to the one already
    # selected emits nothing and would not exercise a rebuild at all.
    start = form._category_combo.currentData()
    target = next(k for k in ('Banking', 'Credit Cards', 'Loans') if k != start)

    form._inputs['name'].setText('Shared Name')
    form._inputs['username'].setText('shareduser')

    form._category_combo.setCurrentIndex(form._category_combo.findData(target))

    assert form._category == target          # the rebuild really happened
    assert form._inputs['name'].text() == 'Shared Name'
    assert form._inputs['username'].text() == 'shareduser'


# -- masked fields ------------------------------------------------------------

def test_a_revealed_field_in_the_form_re_masks_itself(vault, form_class):
    """The detail pane grew an auto-re-mask timer because a revealed password
    could otherwise sit on screen for the rest of the session. The form had its
    own copy of the button without one, so the same secret revealed one panel
    to the left stayed up indefinitely."""
    from ui.widgets import REVEAL_SECONDS

    form = form_class(None, 'Banking')
    field = form._inputs['password']
    toggle = _find_toggle(form, 'Password')
    timer = _find_timer(toggle)

    assert field.echoMode() == QLineEdit.Password
    assert not timer.isActive()

    toggle.setChecked(True)
    assert field.echoMode() == QLineEdit.Normal
    assert toggle.text() == 'Hide'
    assert toggle.accessibleName() == 'Hide Password'
    assert timer.isActive()
    assert timer.interval() == REVEAL_SECONDS * 1000

    # Firing the timer is what re-masks it; that is the whole point of it.
    timer.timeout.emit()
    assert field.echoMode() == QLineEdit.Password
    assert toggle.text() == 'Show'
    assert not timer.isActive()


def _find_toggle(form, label: str):
    from PySide6.QtWidgets import QPushButton
    return next(b for b in form.findChildren(QPushButton)
                if b.accessibleName() in (f'Show {label}', f'Hide {label}'))


def _find_timer(button):
    from PySide6.QtCore import QTimer
    return button.findChild(QTimer)


def test_a_sensitive_field_starts_masked(vault, form_class):
    form = form_class(None, 'Credit Cards')
    assert form._inputs['card_number'].echoMode() == QLineEdit.Password
    assert form._inputs['password'].echoMode() == QLineEdit.Password
    # Not sensitive — must not be masked.
    assert form._inputs['cardholder_name'].echoMode() == QLineEdit.Normal


# -- the password generator ---------------------------------------------------

def test_the_generator_holds_nothing_until_it_is_opened(qt_app):
    """Constructing a form built one of these per password field, each of which
    generated a real password into a visible-echo QLineEdit whether or not the
    user ever opened it."""
    from ui.widgets import GeneratorPanel

    panel = GeneratorPanel()
    panel.hide()
    assert panel._preview.text() == ''

    panel.show()
    generated = panel._preview.text()
    assert len(generated) == 20

    panel.hide()
    assert panel._preview.text() == ''


def test_a_generated_password_is_delivered_before_the_panel_clears(vault, form_class):
    from ui.widgets import GeneratorPanel

    form = form_class(None, 'Banking')
    # Shown, because the panel generates on its showEvent and Qt does not
    # deliver one to a child whose parent is still hidden.
    form.show()
    field = form._inputs['password']
    panel = form.findChildren(GeneratorPanel)[0]

    panel.show()
    generated = panel._preview.text()
    panel._use()

    assert field.text() == generated
    assert len(generated) == 20
    assert form.is_dirty()


def test_the_generator_uses_a_cryptographic_source():
    """Not a behavioural test — a guard against 'random' quietly replacing
    'secrets' in a module whose whole job is producing secrets."""
    import ui.widgets as widgets_module

    source = open(widgets_module.__file__, encoding='utf-8').read()
    assert 'import secrets' in source
    assert 'import random' not in source


# -- URL handling -------------------------------------------------------------

def test_a_refused_url_is_rendered_as_plain_text_not_a_link(vault, qt_app):
    """normalize_url returns '' for a script-ish scheme; the label must then
    carry no href at all rather than an empty one."""
    from ui.detail_view import DetailView

    view = DetailView('dark')
    label = view._link_value('javascript:alert(1)')

    assert '<a ' not in label.text()
    assert label.openExternalLinks() is False


def test_an_ordinary_url_becomes_a_link_that_qt_delivers(vault, qt_app):
    from ui.detail_view import DetailView

    view = DetailView('dark')
    label = view._link_value('example.com')

    assert 'href="https://example.com"' in label.text()
    # Never handed to the OS without passing through normalize_url first.
    assert label.openExternalLinks() is False


def test_a_url_with_markup_in_it_is_escaped(vault, qt_app):
    """The value is user data going into rich text."""
    from ui.detail_view import DetailView

    view = DetailView('dark')
    label = view._link_value('example.com/<img src=x>')

    assert '<img' not in label.text()
    assert '&lt;img' in label.text()


# -- shared helpers -----------------------------------------------------------

def test_the_attention_dict_carries_every_key_it_is_read_for(qt_app):
    """MainWindow's initialiser spelled the shape out by hand and left 'lapsed'
    off the list, which _attention_hint reads."""
    from categories import attention_counts

    counts = attention_counts([])
    for key in ('total', 'due', 'stale', 'lapsed', 'urgent'):
        assert counts[key] == 0


def test_compact_money_matches_the_axis_it_replaced(qt_app):
    from categories import format_money_compact

    assert format_money_compact(950) == '$950'
    assert format_money_compact(1200) == '$1.2k'
    assert format_money_compact(2_400_000) == '$2.4M'
    assert format_money_compact(-1500) == '$-1.5k'
    assert format_money_compact(None) == ''


def test_side_totals_reconciles_with_net_worth(vault):
    """The dashboard used to carry its own copy of this loop, including its own
    liability test."""
    from services import dashboard as dashboard_service

    summary = {
        'months': ['2024-01'],
        'categories': {'Banking': [1000.0], 'Credit Cards': [400.0],
                       'Investments': [-50.0]},
        'net_worth': [550.0],
    }
    assets, liabilities = dashboard_service.side_totals(summary)

    assert assets == 950.0            # negatives counted, unlike composition()
    assert liabilities == 400.0
    assert assets - liabilities == summary['net_worth'][0]


def test_side_totals_reads_a_pre_rename_slug_as_a_liability(vault):
    from services import dashboard as dashboard_service

    assets, liabilities = dashboard_service.side_totals(
        {'categories': {'creditcards': [500.0]}})

    assert (assets, liabilities) == (0.0, 500.0)


def test_to_qdate_reports_an_unreadable_date_as_the_given_default(qt_app):
    from PySide6.QtCore import QDate
    from ui.widgets import NO_DATE, to_qdate

    assert to_qdate('2024-03-05') == QDate(2024, 3, 5)
    assert to_qdate(None, default=NO_DATE) == NO_DATE
    assert to_qdate('not a date', default=NO_DATE) == NO_DATE
    assert to_qdate(None) == QDate.currentDate()


def test_looking_at_an_empty_date_fields_calendar_does_not_fill_it(qt_app):
    """The calendar of a field on the NO_DATE sentinel opens on the current
    month, but the field stays empty until a day is actually chosen — the
    reason the page is moved and the selection is left alone."""
    from PySide6.QtCore import QDate
    from PySide6.QtGui import QShowEvent
    from PySide6.QtWidgets import QApplication
    from ui.widgets import NO_DATE, date_edit

    field = date_edit(NO_DATE, minimum=NO_DATE, no_date_text=' ')
    calendar = field.calendarWidget()
    calendar.setCurrentPage(NO_DATE.year(), NO_DATE.month())  # where the popup leaves it

    QApplication.sendEvent(calendar, QShowEvent())

    today = QDate.currentDate()
    assert (calendar.yearShown(), calendar.monthShown()) == (today.year(), today.month())
    assert field.date() == NO_DATE


def test_an_empty_date_field_reaches_today_by_button_or_by_step(qt_app):
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import QPushButton
    from ui.widgets import NO_DATE, date_edit

    stepped = date_edit(NO_DATE, minimum=NO_DATE, no_date_text=' ')
    stepped.stepBy(1)
    assert stepped.date() == QDate.currentDate()

    clicked = date_edit(NO_DATE, minimum=NO_DATE, no_date_text=' ')
    clicked.calendarWidget().findChild(QPushButton).click()
    assert clicked.date() == QDate.currentDate()

    # A field holding a date still steps its highlighted section, untouched.
    stepped.setDate(QDate(2024, 3, 5))
    stepped.stepBy(1)
    assert stepped.date() == QDate(2024, 4, 5)


def test_a_date_field_that_cannot_reach_today_offers_no_today_button(qt_app):
    """The as-of field bounds itself at today; one bounded earlier would have a
    button that could only offer a date the field clamps away."""
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import QPushButton
    from ui.widgets import date_edit

    past = QDate.currentDate().addYears(-1)
    field = date_edit(past, maximum=past)

    assert field.calendarWidget().findChild(QPushButton) is None


# -- the lock must not leave a secret on the clipboard ------------------------

@pytest.fixture
def window(vault, qt_app):
    """A MainWindow against the throwaway vault, already unlocked.

    Built after the `vault` fixture has pointed database.DB_PATH at tmp_path and
    installed the session key, so this window is looking at the test vault and
    never at the real one.
    """
    from ui.main_window import MainWindow

    window = MainWindow()
    window._enter_vault('test')
    yield window
    _destroy(window, qt_app)


def test_locking_the_vault_clears_a_copied_secret(window, qt_app):
    """lock_vault hid the clipboard chip on the strength of a comment claiming
    it had cleared the clipboard, which it never did — so the countdown and the
    click-to-clear both vanished while the password stayed on the clipboard."""
    from PySide6.QtGui import QGuiApplication

    window.copy_to_clipboard('hunter2-the-real-one', 'Password')
    assert QGuiApplication.clipboard().text() == 'hunter2-the-real-one'
    assert window._clipboard_timer.isActive()

    window.lock_vault()

    assert QGuiApplication.clipboard().text() == ''
    assert window._copied_value is None
    assert not window._clipboard_timer.isActive()


def test_locking_leaves_someone_elses_clipboard_alone(window, qt_app):
    """The 'only wipe what we put there' rule still holds on the lock path: a
    value copied from another app after ours is not ours to clear."""
    from PySide6.QtGui import QGuiApplication

    window.copy_to_clipboard('hunter2-the-real-one', 'Password')
    QGuiApplication.clipboard().setText('something the user copied since')

    window.lock_vault()

    assert QGuiApplication.clipboard().text() == 'something the user copied since'
    assert window._copied_value is None


def test_locking_drops_the_decrypted_accounts(window, qt_app):
    from ui.account_list import ALL

    make_account(name='Secret Bank', password='p@ssword-long-enough')
    window.load_category(ALL)
    assert window.accounts

    window.lock_vault()

    assert window.accounts == []
    assert window.selected_id is None
    assert not accounts_service.cache_is_warm()


# -- one spelling of a stored amount ------------------------------------------
#
# categories.trim_number is the single definition of "whole numbers stay whole",
# shared by the account form, history_io.canonical_amount and
# _sync_current_balance. The three dialog/table paths below wrote str(float)
# instead, so what a vault held depended on which form last touched it.

def test_quick_update_stores_a_whole_figure_whole(vault, qt_app):
    from ui.dialogs import QuickUpdateDialog

    account = accounts_service.get_account(
        make_account(category='Banking', current_balance=100)['id'])
    dialog = QuickUpdateDialog(None, account)
    dialog._balance.setText('1200')

    assert dialog.changes()['current_balance'] == 1200
    assert str(dialog.changes()['current_balance']) == '1200'
    dialog.deleteLater()


def test_quick_update_keeps_a_real_decimal(vault, qt_app):
    """The companion: trimming must not round a figure that has cents."""
    from ui.dialogs import QuickUpdateDialog

    account = accounts_service.get_account(
        make_account(category='Banking', current_balance=100)['id'])
    dialog = QuickUpdateDialog(None, account)
    dialog._balance.setText('$2,501.99')

    assert dialog.changes()['current_balance'] == 2501.99
    dialog.deleteLater()


@pytest.mark.parametrize('typed,expected', [
    ('1200', '1200'), ('1200.00', '1200'), ('$2,501.99', '2501.99'), ('-40', '-40'),
])
def test_editing_a_history_entry_writes_the_canonical_amount(vault, qt_app,
                                                             typed, expected):
    from ui.dialogs import BalanceEntryDialog

    dialog = BalanceEntryDialog(None, '900', '2024-01-05')
    dialog._amount.setText(typed)

    assert dialog.values()[0] == expected
    dialog.deleteLater()


def test_adding_a_history_entry_writes_the_canonical_amount(vault, qt_app):
    """Through the real service, so what lands in balance_history is asserted
    rather than what the widget intended."""
    from ui.history_table import BalanceHistory

    account = make_account(category='Banking', name='Chequing')
    table = BalanceHistory(account['id'], 'dark', 'Balance')
    table._new_amount.setText('1200')
    table._add_entry()

    entries = accounts_service.list_balance_history(account['id'])
    assert [e['balance'] for e in entries] == ['1200']
    # And the account followed it. Not the same *type*: history is text, so
    # canonical_amount is str(trim_number(n)), while the account's own field
    # holds the number trim_number returns — which is what the account form
    # writes too. Both go through trim_number, which is the agreement that
    # matters; 1200.0 in either place is the fault this pins.
    assert accounts_service.get_account(account['id'])['current_balance'] == 1200
    table.deleteLater()


# -- a locked vault is the window's to answer, not a toast --------------------

def test_the_history_table_reports_a_locked_vault_as_a_lock(vault, qt_app):
    """These paths caught VaultLockedError with a bare `except Exception` and
    emitted it as an error, so the pane went on showing the decrypted account
    until the idle tick happened to catch up."""
    import encryption
    from ui.history_table import BalanceHistory

    account = make_account(category='Banking', name='Chequing')
    table = BalanceHistory(account['id'], 'dark', 'Balance')

    seen = {'locked': 0, 'error': []}
    table.locked.connect(lambda: seen.__setitem__('locked', seen['locked'] + 1))
    table.error.connect(seen['error'].append)

    encryption.release_key()
    table.reload()

    assert seen['locked'] == 1
    assert seen['error'] == []
    table.deleteLater()


def test_the_window_locks_itself_when_the_detail_pane_finds_the_vault_locked(
        window, qt_app):
    """The signal is only useful if it is wired: DetailView.locked has to reach
    MainWindow._handle_vault_locked, the same handler _attempt uses."""
    import auth as auth_module

    make_account(name='Chequing', category='Banking')
    window.load_category('all')
    assert window.accounts

    window.detail_view.locked.emit()

    assert not auth_module.is_unlocked()
    assert window.accounts == []


def test_the_detail_pane_still_renders_a_populated_history_table(vault, qt_app):
    """BalanceHistory no longer loads itself in __init__ — the caller does it
    after connecting the signals. If a caller ever forgets, the table renders
    empty and says 'No history yet' over a vault full of entries, which is the
    kind of failure that looks like missing data rather than a bug."""
    from PySide6.QtWidgets import QTableWidget
    from ui.detail_view import DetailView

    account = make_account(category='Banking', name='Chequing')
    accounts_service.add_balance_entry(account['id'], '1200', '2024-01-05')
    accounts_service.add_balance_entry(account['id'], '1300', '2024-02-05')

    view = DetailView('dark')
    view.show_account(accounts_service.get_account(account['id']))

    from PySide6.QtWidgets import QLabel

    tables = view.findChildren(QTableWidget)
    assert tables, 'the history table was never built'
    assert tables[0].rowCount() == 2
    # And the empty state is not also on screen, which is what an unloaded
    # table would have rendered instead.
    assert 'No history yet.' not in [label.text() for label in view.findChildren(QLabel)]
    view.deleteLater()


# -- links and files leaving the vault -----------------------------------------

def test_the_readme_viewer_does_not_dispatch_links_itself(qt_app):
    """setOpenExternalLinks hands every anchor in the file straight to
    QDesktopServices, which made this the one link dispatch in the app that
    urls.normalize_url did not stand in front of."""
    from PySide6.QtWidgets import QTextBrowser
    from ui.dialogs import ReadmeDialog

    dialog = ReadmeDialog(None)
    browser = dialog.findChildren(QTextBrowser)[0]

    assert browser.openLinks() is False
    assert browser.openExternalLinks() is False
    dialog.deleteLater()


@pytest.mark.parametrize('href', [
    'javascript:alert(1)', 'vbscript:x', 'data:text/html,<b>x', 'file:///etc/passwd',
])
def test_the_readme_viewer_refuses_a_scheme_that_executes(qt_app, monkeypatch, href):
    from PySide6.QtCore import QUrl
    from ui import dialogs

    opened = []
    monkeypatch.setattr(dialogs.QDesktopServices, 'openUrl', lambda url: opened.append(url))

    dialogs.ReadmeDialog._follow(QUrl(href))

    assert opened == [], f'{href} was handed to the desktop'


def test_the_readme_viewer_still_opens_an_ordinary_link(qt_app, monkeypatch):
    from PySide6.QtCore import QUrl
    from ui import dialogs

    opened = []
    monkeypatch.setattr(dialogs.QDesktopServices, 'openUrl', lambda url: opened.append(url))

    dialogs.ReadmeDialog._follow(QUrl('https://example.com/docs'))

    assert [url.toString() for url in opened] == ['https://example.com/docs']


def test_an_export_defaults_somewhere_other_than_the_application_directory(qt_app):
    """A bare filename is resolved against the working directory, which for a
    launch through start.bat is the folder the code lives in — so the default
    place to write a CSV of every password was inside the git repository."""
    from pathlib import Path
    from ui.widgets import export_save_path

    suggested = Path(export_save_path('accountvault-export-2026-01-01.csv'))

    assert suggested.name == 'accountvault-export-2026-01-01.csv'
    assert suggested.parent != Path.cwd()
    assert suggested.is_absolute()

# -----------------------------------------------------------------------------
# tests/test_review_fixes.py
# One or more tests per finding of the October 2026 code review, each named for
# the behaviour it holds rather than the bug it was.
# -----------------------------------------------------------------------------

import math
import os
import time

import pytest

from core import auth
from core import database
from core import encryption
from core import preferences
from core import vaultfile
from conftest import MASTER, NEW_MASTER, _destroy, make_account
from services import account_import, backups, export_io
from services import accounts as accounts_service


@pytest.fixture
def window(vault, qt_app):
    from ui.main_window import MainWindow

    win = MainWindow()
    win._enter_vault('test')
    yield win
    if win.form is not None:
        win.form.clear_dirty()
    _destroy(win, qt_app)


def _tamper(**rows):
    """Writes app_config rows the way somebody holding only the file would."""
    for key, value in rows.items():
        database.write_config(key, str(value))


def _configure_backups(folder):
    preferences.save_settings(15, 30, 60, 'dark', 'default', 'compact', 1,
                              'weekly', 12, str(folder))


# -- the backup seal is checked before anything acts on the backup rows ------

def test_a_forged_backup_folder_is_never_offered_for_approval(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    asked, warned = [], []
    monkeypatch.setattr(QMessageBox, 'question',
                        staticmethod(lambda *a, **k: asked.append(a) or QMessageBox.Yes))
    monkeypatch.setattr(QMessageBox, 'warning',
                        staticmethod(lambda *a, **k: warned.append(a)))

    _tamper(auto_backup_enabled=1, backup_location=r'\\attacker\share')
    window.settings = preferences.get_settings()
    window._run_scheduled_backup()

    assert asked == []
    assert len(warned) == 1
    settings = preferences.get_settings()
    assert settings['auto_backup_enabled'] == 0
    assert settings['backup_location'] == preferences.BACKUP_LOCATION_DEFAULT
    assert backups.check_seal() == 'ok'


def test_a_settings_save_does_not_vouch_for_rows_written_behind_its_back(vault, tmp_path):
    _configure_backups(tmp_path / 'mine')
    assert backups.check_seal() == 'ok'

    _tamper(backup_location=r'\\attacker\share')
    preferences.save_settings(15, 30, 60, 'light', 'default', 'compact', 1,
                              'weekly', 12, r'\\attacker\share')

    assert not preferences.backup_config_is_sealed()
    assert backups.check_seal() == 'reset'


def test_a_kdf_upgrade_at_login_keeps_backups_sealed(vault, monkeypatch, tmp_path):
    _configure_backups(tmp_path / 'mine')
    assert backups.check_seal() == 'ok'

    auth.lock_app()
    monkeypatch.setattr(encryption, 'ARGON2_TIME_COST', 2)   # a future release
    assert auth.verify_master_password(MASTER)

    assert backups.check_seal() == 'ok'
    assert preferences.get_settings()['auto_backup_enabled'] == 1


def test_a_password_change_keeps_backups_sealed(vault, tmp_path):
    _configure_backups(tmp_path / 'mine')
    assert auth.change_master_password(MASTER, NEW_MASTER)
    assert backups.check_seal() == 'ok'


def test_a_re_key_does_not_launder_a_stale_seal(vault, tmp_path):
    _configure_backups(tmp_path / 'mine')
    _tamper(backup_location=r'\\attacker\share')

    assert auth.change_master_password(MASTER, NEW_MASTER)

    assert backups.check_seal() == 'reset'


# -- stored values are shown as plain text -------------------------------------

def test_a_revealed_password_that_looks_like_markup_is_shown_exactly(window):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QPushButton

    make_account(name='Tricky', category='Websites', password='xK9<b>mq2')
    window.load_category('Websites')
    shown = [b for b in window.detail_view.findChildren(QPushButton)
             if b.text() == 'Show' and 'Password' in b.accessibleName()]
    shown[0].setChecked(True)

    labels = [label for label in window.detail_view.findChildren(QLabel)
              if label.text() == 'xK9<b>mq2']
    assert labels and all(label.textFormat() == Qt.PlainText for label in labels)


def test_account_names_are_plain_text_in_the_list_and_the_toast(window):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    from ui.account_list import ALL

    make_account(name='<i>Bank</i>', category='Banking')
    window.load_category(ALL)
    rows = [label for label in window.account_list.findChildren(QLabel)
            if label.text() == '<i>Bank</i>']
    assert rows and all(label.textFormat() == Qt.PlainText for label in rows)
    assert window.toast._label.textFormat() == Qt.PlainText


def test_plain_text_escapes_markup_for_message_boxes(qt_app):
    from ui.widgets import plain_text

    html = plain_text('Delete <b>Bank</b>?')
    assert '<b>' not in html and '&lt;b&gt;' in html


# -- nothing is silently truncated ---------------------------------------------

def test_an_over_long_note_is_refused_not_cut_short(vault):
    limit = accounts_service.MAX_LENGTHS['notes']
    make_account(notes='x' * limit)
    with pytest.raises(ValueError, match='Notes is too long'):
        make_account(name='Too much', notes='x' * (limit + 1))


def test_an_over_long_password_is_refused(vault):
    limit = accounts_service.MAX_LENGTHS['password']
    with pytest.raises(ValueError, match='Password is too long'):
        make_account(password='p' * (limit + 1))


def test_the_form_caps_inputs_at_the_stored_limit(vault, qt_app):
    from ui.account_form import AccountForm

    form = AccountForm(None, 'Websites')
    assert form._inputs['password'].maxLength() == accounts_service.MAX_LENGTHS['password']
    assert form._inputs['name'].maxLength() == accounts_service.MAX_LENGTHS['name']


def test_an_import_row_with_an_over_long_value_is_skipped_and_reported():
    result = account_import.prepare(
        'name,password\nFine,short\nLong,' + 'p' * 600 + '\n')
    assert [row['name'] for row in result['rows']] == ['Fine']
    assert result['reasons']['too_long'] == 1


# -- passwords are stored exactly as typed -------------------------------------

def test_the_form_keeps_spaces_around_a_password(vault, qt_app):
    from ui.account_form import AccountForm

    form = AccountForm(None, 'Websites')
    form._inputs['name'].setText('  Site  ')
    form._inputs['password'].setText('  pass with spaces  ')
    values = form.values()
    assert values['password'] == '  pass with spaces  '
    assert values['name'] == 'Site'


def test_an_import_keeps_spaces_around_a_password_and_trims_the_rest():
    result = account_import.prepare('name,username,password\n Site , user ,  pw  \n')
    row = result['rows'][0]
    assert row['name'] == 'Site'
    assert row['username'] == 'user'
    assert row['password'] == '  pw  '


# -- a number that is not finite does not crash ---------------------------------

def test_infinity_in_a_number_field_is_kept_as_typed(vault, qt_app):
    from ui.account_form import AccountForm

    form = AccountForm(None, 'Credit Cards')
    form._inputs['name'].setText('Visa')
    form._inputs['interest_rate'].setText('inf')
    assert form.values()['interest_rate'] == 'inf'


def test_a_stored_infinite_percent_renders_without_raising():
    from core.categories import format_value, trim_number

    assert format_value('inf', {'percent': True}) == 'inf%'
    assert math.isinf(trim_number(float('inf')))


# -- unexpected errors are logged without their message ---------------------------

def test_the_error_log_records_where_but_not_what(tmp_path, monkeypatch):
    import main as main_module

    monkeypatch.setattr(main_module, 'error_log_path', lambda: tmp_path / 'errors.log')
    try:
        float('hunter2')
    except ValueError as exc:
        path = main_module.log_error(type(exc), exc.__traceback__)

    text = path.read_text(encoding='utf-8')
    assert 'ValueError' in text and 'test_review_fixes.py' in text
    assert 'hunter2' not in text


# -- icacls runs once, by full path -----------------------------------------------

def test_restricting_a_file_is_one_icacls_call_by_full_path(tmp_path, monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(database, '_icacls', lambda *args: calls.append(args) or Done())
    target = tmp_path / 'vault.vault'
    target.write_bytes(b'')

    assert database._restrict_windows(target)
    assert len(calls) == 1
    assert '/remove:g' in calls[0]
    assert all(f'*{sid}' in calls[0] for sid in database._WIDE_SIDS)
    assert database._icacls_path().lower().endswith(os.path.join('system32', 'icacls.exe'))


# -- a category name can never collide with a view -----------------------------

def test_view_keys_are_shaped_so_no_category_can_take_them():
    from ui.account_list import AGGREGATE_TABS

    assert all(accounts_service.is_reserved_category(key) for key in AGGREGATE_TABS)


def test_a_reserved_category_name_is_refused(vault):
    with pytest.raises(ValueError):
        make_account(category='__all__')
    account = make_account(category='Banking')
    with pytest.raises(ValueError):
        accounts_service.set_category([account['id']], '__closed__')


def test_a_custom_category_called_closed_gets_its_own_tab(window):
    from ui.account_list import ALL

    make_account(name='Archive box', category='closed')
    window.load_category(ALL)
    assert 'closed' in window._tab_order


# -- CSV round trips -------------------------------------------------------------

@pytest.mark.parametrize('value', ["'=abc", "'hello", '=SUM(A1)', '-5', "''x", 'plain'])
def test_defuse_and_undefuse_round_trip(value):
    assert export_io.undefuse(export_io.defuse(value)) == value


def test_an_unreadable_placeholder_never_becomes_a_password(vault):
    result = account_import.prepare(
        f'name,password\nDamaged,{encryption.UNREADABLE}\n')
    assert 'password' not in result['rows'][0]
    assert result['unreadable'] == 1

    account = accounts_service.create_account(
        {'name': 'Copied', 'category': 'Banking', 'password': encryption.UNREADABLE})
    assert not account.get('password')


# -- quitting asks about unsaved edits -------------------------------------------

def test_quitting_with_unsaved_edits_asks_first(window, monkeypatch):
    prompted = []
    window._start_add()
    window.form._mark_dirty()
    monkeypatch.setattr(window, 'isVisible', lambda: True)
    monkeypatch.setattr(window, '_show_dirty_prompt', lambda: prompted.append(True))

    assert window.close() is False
    assert prompted == [True]
    assert window._pending_navigation == ('quit', None, None)
    assert auth.is_unlocked()


# -- a dead share cannot freeze startup --------------------------------------------

def test_a_status_check_gives_up_on_a_path_that_hangs(monkeypatch, tmp_path):
    def slow(path):
        if 'dead' in str(path):
            time.sleep(2)
        return 'ok'

    monkeypatch.setattr(database, 'inspect_vault', slow)
    started = time.monotonic()
    statuses = vaultfile.probe_all([tmp_path / 'dead.vault', tmp_path / 'live.vault'],
                                   timeout=0.3)
    assert statuses == ['unavailable', 'ok']
    assert time.monotonic() - started < 1.5


# -- locking Windows locks the vault ------------------------------------------------

@pytest.mark.skipif(os.name != 'nt', reason='Windows session notifications')
def test_the_session_lock_message_is_recognised():
    import ctypes
    import ctypes.wintypes

    from ui.vault_session import is_session_lock

    msg = ctypes.wintypes.MSG()
    msg.message = 0x02B1
    msg.wParam = 0x7
    assert is_session_lock(b'windows_generic_MSG', ctypes.addressof(msg))

    msg.wParam = 0x8   # unlock
    assert not is_session_lock(b'windows_generic_MSG', ctypes.addressof(msg))


def test_a_windows_lock_locks_the_vault(window):
    window._on_session_locked()

    assert not auth.is_unlocked()
    assert window.screens.currentWidget() is window.login_screen
    assert 'Windows was locked' in window.login_screen.error.text()


# -- notes are not searched ------------------------------------------------------------

def test_notes_are_not_searchable_in_any_category():
    from core.categories import CATEGORY_CONFIG, searchable_fields

    for category in [*CATEGORY_CONFIG, 'Some Custom Category']:
        assert 'notes' not in {f['key'] for f in searchable_fields(category)}


def test_searching_for_note_text_finds_nothing(window):
    make_account(name='Bank', notes='PIN is 4711')
    window.search_query = '4711'
    window._run_search()
    assert window.accounts == []


# -- the window asks which screen is up rather than counting ------------------------

def test_shortcuts_are_live_only_on_the_main_screen(window):
    assert window._on_main_screen()
    window.lock_vault()
    assert not window._on_main_screen()

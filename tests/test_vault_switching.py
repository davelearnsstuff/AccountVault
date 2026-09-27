# -----------------------------------------------------------------------------
# tests/test_vault_switching.py
# The window half of choosing a vault: which screen comes up, and what is left
# of the previous vault once a different one is open.
#
# The teardown tests are the point of the file. Locking is what does most of
# the work — it releases the key, which fires the listeners that drop the
# decrypted caches — but everything per-vault that had no reason to be reset
# while the vault could never change is now a way for one vault's state to show
# up inside another.
# -----------------------------------------------------------------------------

import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

import auth
import database
import encryption
import preferences
import vaultfile
from conftest import MASTER, _destroy, make_account
from services import accounts as accounts_service

PASSPHRASE = 'a sufficiently long passphrase'


@pytest.fixture
def window(vault, qt_app, tmp_path, monkeypatch):
    """An unlocked MainWindow with the vault registry redirected to tmp_path."""
    from ui.main_window import MainWindow

    monkeypatch.setattr(database, 'app_data_dir', lambda: tmp_path / 'appdata')
    monkeypatch.delenv(vaultfile.ENV_OVERRIDE, raising=False)

    window = MainWindow()
    window._enter_vault('test')
    yield window
    _destroy(window, qt_app)


def pick(monkeypatch, path):
    """Makes both file dialogs answer with one path."""
    monkeypatch.setattr(QFileDialog, 'getSaveFileName',
                        staticmethod(lambda *a, **k: (str(path), '')))
    monkeypatch.setattr(QFileDialog, 'getOpenFileName',
                        staticmethod(lambda *a, **k: (str(path), '')))


def answer(monkeypatch, button):
    monkeypatch.setattr(QMessageBox, 'question', staticmethod(lambda *a, **k: button))
    monkeypatch.setattr(QMessageBox, 'information', staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, 'warning', staticmethod(lambda *a, **k: None))


# -- which screen comes up ----------------------------------------------------

def test_no_vault_open_lands_on_the_chooser(window):
    database.close_vault()

    window.start('the vault has moved')

    assert window.screens.currentWidget() is window.chooser_screen


def test_a_vault_with_no_master_password_lands_on_setup(window, tmp_path):
    database.open_vault(tmp_path / 'fresh.vault', create=True)

    window.start()

    assert window.screens.currentWidget() is window.setup_screen


def test_an_existing_vault_lands_on_login_and_names_itself(window):
    window.start()

    assert window.screens.currentWidget() is window.login_screen
    assert window.login_screen.vault_caption.text() == 'vault.vault'


# -- creating -----------------------------------------------------------------

def test_creating_a_vault_opens_it_at_setup(window, tmp_path, monkeypatch):
    fresh = tmp_path / 'mymoney.vault'
    pick(monkeypatch, fresh)

    window._create_vault_dialog()

    assert database.DB_PATH == fresh
    assert window.screens.currentWidget() is window.setup_screen


def test_creating_over_an_existing_vault_never_truncates_it(window, tmp_path, monkeypatch):
    """getSaveFileName treats overwrite as an ordinary confirmation, and the
    file behind that click can be somebody's only copy of everything."""
    make_account(name='Irreplaceable')
    existing = database.DB_PATH
    before = existing.read_bytes()
    pick(monkeypatch, existing)
    answer(monkeypatch, QMessageBox.No)

    window._create_vault_dialog()

    assert existing.read_bytes() == before


def test_creating_over_an_existing_vault_offers_to_open_it(window, tmp_path, monkeypatch):
    existing = database.DB_PATH
    pick(monkeypatch, existing)
    answer(monkeypatch, QMessageBox.Yes)

    window._create_vault_dialog()

    assert database.DB_PATH == existing
    assert window.screens.currentWidget() is window.login_screen


def test_a_missing_extension_is_added(window, tmp_path, monkeypatch):
    pick(monkeypatch, tmp_path / 'noextension')

    window._create_vault_dialog()

    assert database.DB_PATH.suffix == database.VAULT_SUFFIX


# -- opening ------------------------------------------------------------------

def test_a_file_that_is_not_a_vault_never_reaches_setup(window, tmp_path):
    """is_first_launch means 'no master hash in this database'. Without the
    check in front of it, a file picked by mistake goes to the setup screen and
    the next thing the user does is put a master password on it."""
    junk = tmp_path / 'notes.vault'
    junk.write_text('definitely not a database', encoding='utf-8')

    window._open_vault_path(junk)

    assert database.DB_PATH != junk
    assert window.screens.currentWidget() is window.chooser_screen


def test_a_vault_from_a_newer_build_is_refused_with_a_reason(window, monkeypatch):
    monkeypatch.setattr(database, 'format_of', lambda path: auth.VAULT_FORMAT + 1)
    target = database.DB_PATH

    window._open_vault_path(target)

    assert window.screens.currentWidget() is window.chooser_screen
    assert 'newer version' in window.chooser_screen.error.text()


def test_opening_the_vault_already_open_just_locks_it(window):
    window._open_vault_path(database.DB_PATH)

    assert window.screens.currentWidget() is window.login_screen


def test_an_opened_vault_is_remembered_only_once_it_opens(window, tmp_path):
    junk = tmp_path / 'junk.vault'
    junk.write_text('not a database', encoding='utf-8')

    window._open_vault_path(junk)

    assert [p.name for p in vaultfile.recent()] == []


# -- what a switch has to leave behind ----------------------------------------

def test_switching_releases_the_key(window, tmp_path):
    second = _second_vault(tmp_path)

    window._open_vault_path(second)

    assert not encryption.is_unlocked()


def test_switching_drops_the_decrypted_account_cache(window, tmp_path):
    make_account(name='First Vault Account')
    accounts_service.list_accounts()
    assert accounts_service.cache_is_warm()
    second = _second_vault(tmp_path)

    window._open_vault_path(second)

    assert not accounts_service.cache_is_warm()


def test_switching_clears_the_account_list_on_screen(window, tmp_path):
    make_account(name='First Vault Account')
    window.load_category('all')
    assert window.account_list.count() > 0
    second = _second_vault(tmp_path)

    window._open_vault_path(second)

    assert window.account_list.count() == 0


def test_a_lockout_in_one_vault_does_not_follow_into_another(window, tmp_path):
    """Vault A's failed logins locking somebody out of vault B is a vault they
    have not touched refusing a password they have not got wrong."""
    auth._lockout_deadline_monotonic = 1e12
    auth._lockout_count = 5
    second = _second_vault(tmp_path)

    window._open_vault_path(second)

    allowed, _ = auth.check_login_rate_limit()
    assert allowed is True


def test_a_switch_adopts_the_new_vaults_appearance(window, tmp_path):
    """Theme and font size live in each vault's own app_config, so a switch
    that skipped this would leave the incoming vault wearing the outgoing
    one's."""
    preferences.save_settings(15, 30, 60, 'light', 'xl', 'compact')
    second = _second_vault(tmp_path, theme='dark', font_size='small')

    window._open_vault_path(second)

    assert window.settings['theme'] == 'dark'
    assert window.settings['font_size'] == 'small'


def test_a_failed_switch_leaves_the_app_at_the_chooser_not_in_limbo(window, tmp_path):
    missing = tmp_path / 'not-there.vault'

    window._open_vault_path(missing)

    assert window.screens.currentWidget() is window.chooser_screen
    assert database.DB_PATH != missing


# -- moving -------------------------------------------------------------------

def test_moving_a_vault_leaves_the_original_in_place(window, tmp_path, monkeypatch):
    """The only vault the user has until the copy is proved good. An app that
    deletes the old one on their behalf is one that can be wrong about it once."""
    make_account(name='Bank')
    origin = database.DB_PATH
    destination = tmp_path / 'moved' / 'vault.vault'
    pick(monkeypatch, destination)
    answer(monkeypatch, QMessageBox.Yes)

    window._move_vault()

    assert database.DB_PATH == destination
    assert origin.exists(), 'the original was deleted'


def test_moving_replaces_the_recent_entry_rather_than_adding_one(
        window, tmp_path, monkeypatch):
    """Otherwise the list offers both, and the abandoned file is a valid vault
    holding a snapshot that stops the moment it was left."""
    origin = database.DB_PATH
    vaultfile.remember(origin)
    destination = tmp_path / 'moved' / 'vault.vault'
    pick(monkeypatch, destination)
    answer(monkeypatch, QMessageBox.Yes)

    window._move_vault()

    remembered = [str(p) for p in vaultfile.recent()]
    assert str(origin) not in remembered
    assert len(remembered) == 1


def test_a_move_onto_an_existing_file_is_refused(window, tmp_path, monkeypatch):
    blocker = tmp_path / 'taken.vault'
    blocker.write_bytes(b'somebody elses file')
    origin = database.DB_PATH
    pick(monkeypatch, blocker)
    answer(monkeypatch, QMessageBox.Yes)

    window._move_vault()

    assert blocker.read_bytes() == b'somebody elses file'
    assert database.DB_PATH == origin


# -- helpers ------------------------------------------------------------------

def _second_vault(tmp_path, **settings):
    """A complete second vault, with the first one restored as the open one."""
    first = database.DB_PATH
    path = tmp_path / 'second.vault'
    database.open_vault(path, create=True)
    auth.setup_master_password(PASSPHRASE)
    if settings:
        preferences.save_settings(15, 30, 60, settings.get('theme', 'dark'),
                                  settings.get('font_size', 'default'), 'compact')
    encryption.release_key()
    database.DB_PATH = first
    auth.verify_master_password(MASTER)
    return path

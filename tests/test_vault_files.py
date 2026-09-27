# -----------------------------------------------------------------------------
# tests/test_vault_files.py
# Choosing which file is the vault.
#
# Weighted towards the two ways this loses data outright: creating a vault over
# a file that already holds one, and opening something that is not a vault and
# then setting a master password on it. Both are one confirmation click away in
# a file dialog, so neither is allowed to depend on the click.
# -----------------------------------------------------------------------------

import pathlib
import sqlite3

import pytest

import auth
import database
import vaultfile
from conftest import MASTER, make_account


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Points the vault registry at a throwaway directory.

    Through database.app_data_dir, which is where vaultfile reads it from on
    every call — a name bound at import would leave the real registry in play.
    """
    home = tmp_path / 'appdata'
    monkeypatch.setattr(database, 'app_data_dir', lambda: home)
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: None)
    monkeypatch.delenv(vaultfile.ENV_OVERRIDE, raising=False)
    return home


# -- what a file turns out to be ----------------------------------------------

def test_a_real_vault_inspects_as_ok(vault):
    assert database.inspect_vault(database.DB_PATH) == 'ok'


def test_a_path_that_is_not_there_is_missing_not_unavailable(tmp_path):
    assert database.inspect_vault(tmp_path / 'nope.vault') == 'missing'


def test_an_empty_file_is_not_mistaken_for_a_vault(tmp_path):
    """What an interrupted create leaves behind. Reported as empty rather than
    opened, because a zero-byte file is where a new vault would go."""
    path = tmp_path / 'half.vault'
    path.touch()

    assert database.inspect_vault(path) == 'empty'


def test_a_text_file_is_not_a_vault(tmp_path):
    path = tmp_path / 'notes.vault'
    path.write_text('this is not a database', encoding='utf-8')

    assert database.inspect_vault(path) == 'not-sqlite'


def test_a_database_with_no_master_hash_is_not_a_vault(tmp_path):
    """The distinction is the whole reason inspect_vault exists.
    auth.is_first_launch means 'no master hash in this database', so a database
    that passed as a vault here would be handed to the setup screen — and the
    next thing the user did would be to put a master password on somebody
    else's file."""
    path = tmp_path / 'other.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT)')
    db.commit()
    db.close()

    assert database.inspect_vault(path) == 'not-vault'


def test_inspecting_a_path_does_not_create_it(tmp_path):
    """Read-only by URI. sqlite3.connect creates on open, so an inspection that
    connected normally would leave an empty database wherever it looked."""
    path = tmp_path / 'ghost.vault'

    database.inspect_vault(path)

    assert not path.exists()


def test_the_recorded_format_comes_back(vault):
    assert database.format_of(database.DB_PATH) == auth.VAULT_FORMAT


def test_a_vault_named_by_a_relative_path_is_still_a_vault(vault, monkeypatch):
    """A file: URI has no working directory, so a relative path used to become
    one at the root of the drive. SQLite opened that read-only without
    complaint, found no tables, and format_of answered None — so a perfectly
    good vault was reported as "a database, but not an AccountVault vault".

    Every caller that passes a path straight from the command line went through
    here, and the message sent the user looking for corruption that was not
    there."""
    folder = database.DB_PATH.parent
    monkeypatch.chdir(folder)
    relative = pathlib.Path(database.DB_PATH.name)
    assert not relative.is_absolute()

    assert database.inspect_vault(relative) == 'ok'
    assert database.format_of(relative) == auth.VAULT_FORMAT
    assert database.inspect_vault(pathlib.Path('.') / relative) == 'ok'


def test_a_relative_path_that_is_not_there_is_still_missing(vault, monkeypatch):
    """The other half: making the path absolute must not turn a name for
    nothing into a name for something."""
    monkeypatch.chdir(database.DB_PATH.parent)

    assert database.inspect_vault(pathlib.Path('nope.vault')) == 'missing'


# -- opening and creating -----------------------------------------------------

def test_creating_over_an_existing_file_is_refused(vault, tmp_path):
    """A save dialog offers 'overwrite?' as an ordinary confirmation, and the
    file behind it can be the only copy of everything somebody has."""
    existing = tmp_path / 'taken.vault'
    existing.write_bytes(b'important bytes')

    with pytest.raises(database.VaultFileError):
        database.open_vault(existing, create=True)

    assert existing.read_bytes() == b'important bytes'


def test_opening_a_path_that_is_not_there_does_not_create_one(tmp_path, monkeypatch):
    """Reached from a recent-files entry whose file has been moved, a new empty
    vault is indistinguishable from the old one having been emptied."""
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: None)
    missing = tmp_path / 'moved.vault'

    with pytest.raises(database.VaultFileError):
        database.open_vault(missing, create=False)

    assert not missing.exists()


def test_a_failed_open_leaves_the_current_vault_in_place(vault, tmp_path):
    before = database.DB_PATH

    with pytest.raises(database.VaultFileError):
        database.open_vault(tmp_path / 'absent.vault', create=False)

    assert database.DB_PATH == before


def test_a_closed_vault_refuses_to_be_read_rather_than_answering(vault):
    database.close_vault()
    try:
        with pytest.raises(database.NoVaultOpen):
            with database.connect():
                pass
    finally:
        database.DB_PATH = None


def test_no_vault_open_is_a_sqlite_error(vault):
    """Deliberate: preferences.py already treats 'the database would not
    answer' as 'use the default', which is exactly right for a window that has
    to be built and themed before any vault is open."""
    assert issubclass(database.NoVaultOpen, sqlite3.Error)


# -- the directory is not ours to restrict ------------------------------------

def test_a_user_chosen_directory_is_not_restricted(tmp_path, monkeypatch):
    """This used to ACL DB_PATH.parent unconditionally. Pointed at Documents,
    that does not protect the vault — it locks every other application out of
    Documents."""
    chosen = tmp_path / 'Documents'
    chosen.mkdir()
    restricted = []
    monkeypatch.setattr(database, 'restrict_to_owner',
                        lambda path: restricted.append(path) or True)
    monkeypatch.setattr(database, 'DB_PATH', chosen / 'mine.vault')

    database.init_db()

    assert chosen not in restricted, 'a directory the user picked was restricted'
    assert database.DB_PATH in restricted, 'the vault file itself was not restricted'


def test_a_directory_the_app_creates_is_restricted(tmp_path, monkeypatch):
    fresh = tmp_path / 'made-by-us'
    restricted = []
    monkeypatch.setattr(database, 'restrict_to_owner',
                        lambda path: restricted.append(path) or True)
    monkeypatch.setattr(database, 'DB_PATH', fresh / 'mine.vault')

    database.init_db()

    assert fresh in restricted


def test_declining_to_restrict_a_chosen_directory_is_not_reported_as_a_failure(
        tmp_path, monkeypatch):
    """permissions_hardened drives a warning about the vault file. Skipping a
    directory that was never ours must not make that warning fire."""
    chosen = tmp_path / 'Documents'
    chosen.mkdir()
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: True)
    monkeypatch.setattr(database, 'DB_PATH', chosen / 'mine.vault')

    database.init_db()

    assert database.permissions_hardened is True


# -- the registry -------------------------------------------------------------

def test_a_remembered_vault_comes_back(registry, tmp_path):
    path = tmp_path / 'one.vault'
    vaultfile.remember(path)

    assert [p.name for p in vaultfile.recent()] == ['one.vault']


def test_reopening_a_vault_moves_it_to_the_front(registry, tmp_path):
    for name in ('a.vault', 'b.vault', 'c.vault'):
        vaultfile.remember(tmp_path / name)
    vaultfile.remember(tmp_path / 'a.vault')

    assert [p.name for p in vaultfile.recent()] == ['a.vault', 'c.vault', 'b.vault']


def test_the_same_vault_spelled_two_ways_is_one_entry(registry, tmp_path):
    """Windows treats these as one file. A list that disagreed would offer the
    same vault twice, once per spelling."""
    vaultfile.remember(tmp_path / 'Money.vault')
    vaultfile.remember(tmp_path / 'money.VAULT')

    assert len(vaultfile.recent()) == 1


def test_the_list_is_capped(registry, tmp_path):
    for index in range(vaultfile.MAX_RECENT + 5):
        vaultfile.remember(tmp_path / f'v{index}.vault')

    assert len(vaultfile.recent()) == vaultfile.MAX_RECENT


def test_forgetting_a_vault_does_not_touch_the_file(registry, tmp_path):
    path = tmp_path / 'keep.vault'
    path.write_text('still here', encoding='utf-8')
    vaultfile.remember(path)

    vaultfile.forget(path)

    assert vaultfile.recent() == []
    assert path.read_text(encoding='utf-8') == 'still here'


def test_a_corrupt_registry_reads_as_empty_rather_than_raising(registry):
    """It must not be able to stop a launch: the chooser screen this falls back
    to is the same one a first-ever launch sees."""
    registry.mkdir(parents=True, exist_ok=True)
    vaultfile.registry_path().write_text('{not json at all', encoding='utf-8')

    assert vaultfile.recent() == []


def test_turning_the_list_off_clears_what_was_stored(registry, tmp_path):
    """Leaving the paths on disk and merely hiding them would keep the file
    naming them, which is the thing somebody turning this off wants gone."""
    vaultfile.remember(tmp_path / 'private.vault')

    vaultfile.set_remembering(False)

    assert vaultfile.recent() == []
    assert 'private.vault' not in vaultfile.registry_path().read_text(encoding='utf-8')


def test_the_registry_records_paths_and_nothing_else(registry, vault, tmp_path):
    """Metadata in the clear is the cost of the recent list. It stays the paths
    only — no counts, no sizes, nothing summarising what a vault holds."""
    make_account(name='Very Secret Bank')
    vaultfile.remember(database.DB_PATH)

    stored = vaultfile.registry_path().read_text(encoding='utf-8')

    assert 'Very Secret Bank' not in stored
    assert set(__import__('json').loads(stored)) <= {'version', 'recent',
                                                     'remember_recent'}


# -- startup resolution -------------------------------------------------------

def test_the_environment_override_wins(registry, tmp_path, monkeypatch):
    vaultfile.remember(tmp_path / 'remembered.vault')
    monkeypatch.setenv(vaultfile.ENV_OVERRIDE, str(tmp_path / 'forced.vault'))

    assert vaultfile.startup_path().name == 'forced.vault'


def test_a_path_on_the_command_line_wins_over_everything(registry, tmp_path, monkeypatch):
    monkeypatch.setenv(vaultfile.ENV_OVERRIDE, str(tmp_path / 'forced.vault'))

    assert vaultfile.startup_path(str(tmp_path / 'typed.vault')).name == 'typed.vault'


def test_the_last_vault_is_reopened(registry, tmp_path):
    vaultfile.remember(tmp_path / 'older.vault')
    vaultfile.remember(tmp_path / 'newer.vault')

    assert vaultfile.startup_path().name == 'newer.vault'


def test_a_moved_vault_does_not_fall_through_to_another_one(registry, tmp_path, vault):
    """It is returned so the app can say which vault is missing. Quietly opening
    the next candidate instead is how somebody concludes their data is gone."""
    vaultfile.remember(tmp_path / 'gone.vault')

    resolved = vaultfile.startup_path()

    assert resolved.name == 'gone.vault'
    assert database.inspect_vault(resolved) == 'missing'


def test_an_existing_vault_at_the_old_location_is_adopted_without_asking(
        registry, tmp_path, monkeypatch):
    """Nobody should have to go and find their own vault because the app
    changed how it records the location."""
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: None)
    legacy = registry / database.LEGACY_DB_NAME
    monkeypatch.setattr(database, 'DB_PATH', legacy)
    database.init_db()
    auth.setup_master_password(MASTER)

    assert vaultfile.startup_path() == legacy


# -- the off switch has to be reachable from the screen that carries it -------

@pytest.fixture
def chooser(qt_app, registry):
    """A chooser screen pointed at the throwaway registry."""
    from ui.auth_screens import ChooserScreen
    screen = ChooserScreen()
    yield screen
    screen.deleteLater()


def test_the_chooser_offers_the_switch_in_both_positions(chooser, tmp_path):
    """Checked when the list is being kept, unchecked when it is not. The menu
    is built from vaultfile.remembering() rather than from a widget's own idea
    of the state, so it cannot drift from what is stored."""
    vaultfile.remember(tmp_path / 'a.vault')
    chooser.reset()
    assert _remember_action(chooser).isChecked()

    vaultfile.set_remembering(False)
    chooser.reset()
    assert not _remember_action(chooser).isChecked()


def test_turning_the_list_off_leaves_the_way_back_on_screen(chooser, tmp_path):
    """The regression this guards: the list hid itself the moment it emptied,
    and the switch lives in its context menu — so turning remembering off took
    the only way to turn it back on off the screen with it."""
    vaultfile.remember(tmp_path / 'a.vault')
    chooser.reset()

    chooser.remember_toggled.emit(False)      # what the menu action emits
    vaultfile.set_remembering(False)
    chooser.reset()

    assert vaultfile.recent() == []
    assert not chooser.recent.isHidden(), (
        'the recent list hid itself, taking its context menu — and the only '
        'way back to remembering — with it')
    assert _remember_action(chooser) is not None


def test_the_placeholder_row_cannot_be_opened_as_a_vault(chooser):
    """It is a sentence, not a path. Activating it must not ask the window to
    open a vault called 'Not remembering recent vaults'."""
    vaultfile.set_remembering(False)
    chooser.reset()

    opened = []
    chooser.recent_activated.connect(opened.append)
    chooser._activate(chooser.recent.item(0))

    assert opened == []


def _remember_action(chooser, point=None):
    """The 'Remember recent vaults' action from the list's context menu.

    Built rather than shown: exec() is a modal loop with nothing to close it
    under the offscreen platform."""
    from PySide6.QtCore import QPoint

    menu = chooser._build_context_menu(point or QPoint(0, 0))
    for action in menu.actions():
        if action.text() == 'Remember recent vaults':
            return action
    return None

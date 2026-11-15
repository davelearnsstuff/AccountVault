# -----------------------------------------------------------------------------
# tests/test_folder_and_journal.py
# The vault's folder, and the rollback journal beside the vault.
#
# Restricting the vault file is undone by anyone who can write its folder, so
# the app finds out and warns. The journal is kept as one owner-only file
# rather than recreated for every write under the folder's permissions.
#
# The Windows folder tests change a real ACL with icacls, because the check
# reads the real ACL through the Windows API: a stubbed one would test nothing.
# -----------------------------------------------------------------------------

import os

import pytest

from core import database
from conftest import make_account

windows_only = pytest.mark.skipif(os.name != 'nt', reason='Windows ACLs')
posix_only = pytest.mark.skipif(os.name == 'nt', reason='POSIX permissions')

EVERYONE = '*S-1-1-0'


def grant(folder, rights: str):
    result = database._icacls(str(folder), '/grant', f'{EVERYONE}:{rights}')
    assert result.returncode == 0, result.stdout + result.stderr


# -- the journal ------------------------------------------------------------------

def test_the_journal_is_created_with_the_vault(vault):
    journal = database.journal_path()
    assert journal.exists()
    assert journal.stat().st_size == 0


def test_every_connection_keeps_the_journal(vault):
    with database.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == 'truncate'


def test_the_journal_survives_a_write_and_is_left_empty(vault):
    """Kept, so its permissions are the ones the app set; emptied, so nothing
    of the last transaction is left in it."""
    make_account(name='Bank', notes='a note')
    journal = database.journal_path()
    assert journal.exists()
    assert journal.stat().st_size == 0


def test_the_journal_survives_a_vacuum(vault):
    make_account(name='Bank')
    assert database.vacuum() is True
    assert database.journal_path().exists()


def test_an_existing_journal_is_left_exactly_as_it_was(vault):
    """It may be what a crash left, which SQLite has to replay."""
    journal = database.journal_path()
    journal.write_bytes(b'what a crash left')
    database._create_journal_file()
    assert journal.read_bytes() == b'what a crash left'
    journal.write_bytes(b'')


def test_a_journal_that_cannot_be_restricted_is_reported(vault, monkeypatch):
    monkeypatch.setattr(database, 'restrict_to_owner',
                        lambda path: not str(path).endswith(database.JOURNAL_SUFFIX))
    database.init_db()
    assert database.permissions_hardened is False


def test_the_journal_is_restricted_like_the_vault(vault, monkeypatch):
    seen = []
    monkeypatch.setattr(database, 'restrict_to_owner',
                        lambda path: seen.append(path) or True)
    database.init_db()
    assert database.journal_path() in seen
    assert database.permissions_hardened is True


# -- the folder -------------------------------------------------------------------

def test_a_folder_that_cannot_be_read_is_unknown(tmp_path):
    assert database.folder_open_to_others(tmp_path / 'not there') is None


def test_a_private_folder_is_closed(tmp_path):
    folder = tmp_path / 'private'
    folder.mkdir()
    database.restrict_to_owner(folder)
    assert database.folder_open_to_others(folder) is False


@windows_only
def test_a_folder_everyone_can_write_is_open(tmp_path):
    folder = tmp_path / 'shared'
    folder.mkdir()
    grant(folder, '(W)')
    assert database.folder_open_to_others(folder) is True


@windows_only
def test_a_grant_that_only_reaches_files_inside_is_not_counted(tmp_path):
    """Inherit-only: it applies to new files in the folder, not to the folder,
    so it gives nobody the right to create or remove files there."""
    folder = tmp_path / 'inherit-only'
    folder.mkdir()
    database.restrict_to_owner(folder)
    grant(folder, '(OI)(IO)(W)')
    assert database.folder_open_to_others(folder) is False


@windows_only
def test_creating_subfolders_alone_is_not_counted(tmp_path):
    """What C:\\ grants every user. A subfolder cannot stand in for the vault."""
    folder = tmp_path / 'subfolders'
    folder.mkdir()
    database.restrict_to_owner(folder)
    grant(folder, '(AD)')
    assert database.folder_open_to_others(folder) is False


@posix_only
@pytest.mark.parametrize('mode, expected', [(0o700, False), (0o755, False),
                                            (0o777, True), (0o703, True)])
def test_posix_modes(tmp_path, mode, expected):
    folder = tmp_path / 'folder'
    folder.mkdir()
    folder.chmod(mode)
    assert database.folder_open_to_others(folder) is expected


@windows_only
def test_opening_a_vault_in_an_open_folder_records_it(tmp_path, monkeypatch, cheap_argon2):
    folder = tmp_path / 'shared'
    folder.mkdir()
    grant(folder, '(OI)(CI)(M)')
    monkeypatch.setattr(database, 'DB_PATH', None)
    database.open_vault(folder / 'shared.vault', create=True)
    try:
        assert database.folder_open is True
    finally:
        database.close_vault()
    assert database.folder_open is False


# -- the warning ------------------------------------------------------------------

@pytest.fixture
def warnings(monkeypatch, qt_app):
    from PySide6.QtWidgets import QMessageBox
    shown = []
    monkeypatch.setattr(QMessageBox, 'warning',
                        staticmethod(lambda parent, title, text: shown.append(text)))
    return shown


def test_a_protected_vault_says_nothing(vault, warnings, monkeypatch):
    from ui.vault_session import warn_if_unprotected
    monkeypatch.setattr(database, 'permissions_hardened', True)
    monkeypatch.setattr(database, 'folder_open', False)
    warn_if_unprotected()
    assert warnings == []


def test_an_open_folder_is_warned_about(vault, warnings, monkeypatch):
    from ui.vault_session import warn_if_unprotected
    monkeypatch.setattr(database, 'permissions_hardened', True)
    monkeypatch.setattr(database, 'folder_open', True)
    warn_if_unprotected()
    (text,) = warnings
    assert 'Other accounts' in text and 'Move Vault' in text
    assert "could not be restricted" not in text


def test_an_unknown_folder_is_not_warned_about(vault, warnings, monkeypatch):
    """None is "could not tell", which is not evidence of anything."""
    from ui.vault_session import warn_if_unprotected
    monkeypatch.setattr(database, 'permissions_hardened', True)
    monkeypatch.setattr(database, 'folder_open', None)
    warn_if_unprotected()
    assert warnings == []


def test_both_problems_share_one_warning(vault, warnings, monkeypatch):
    from ui.vault_session import warn_if_unprotected
    monkeypatch.setattr(database, 'permissions_hardened', False)
    monkeypatch.setattr(database, 'folder_open', True)
    warn_if_unprotected()
    (text,) = warnings
    assert 'could not be restricted' in text and 'Other accounts' in text

# -----------------------------------------------------------------------------
# tests/test_backups.py
# Copying the vault, and the schedule that does it unattended.
#
# The three invariants this file exists to hold, all of them about not losing
# what the user already has:
#
#   * a partial write never becomes a backup;
#   * deletion only ever follows a successful write;
#   * nothing is deleted that this app did not create.
#
# Dates are passed in rather than taken from the clock. A retention test that
# waited for a month to pass would not be a test.
# -----------------------------------------------------------------------------

import datetime
import json
import sqlite3

import pytest

from core import auth
from core import database
from core import preferences
from conftest import MASTER, NEW_MASTER, make_account
from services import accounts as accounts_service
from services import backups

TODAY = datetime.date(2026, 9, 17)


def settings(**overrides) -> dict:
    base = {'auto_backup_enabled': 1, 'backup_frequency': 'daily',
            'backup_retention_months': 12,
            'backup_location': preferences.BACKUP_LOCATION_DEFAULT}
    base.update(overrides)
    return base


# -- naming and location ------------------------------------------------------

def test_a_backup_is_named_for_the_vault_and_the_day(vault):
    assert backups.backup_name(when=TODAY) == 'vault_backup_20260917.vault'


def test_the_default_folder_sits_beside_the_vault(vault):
    assert backups.default_dir() == database.DB_PATH.parent / 'backups'


def test_the_default_location_follows_the_vault(vault, tmp_path, monkeypatch):
    """Stored as a sentinel rather than a resolved path, so moving the vault
    does not leave its backups going to where it used to live."""
    elsewhere = tmp_path / 'moved' / 'vault.vault'
    monkeypatch.setattr(database, 'DB_PATH', elsewhere)

    assert backups.resolve_dir(settings()) == elsewhere.parent / 'backups'


def test_a_chosen_location_is_used_as_given(vault, tmp_path):
    chosen = tmp_path / 'somewhere else'

    assert backups.resolve_dir(settings(backup_location=str(chosen))) == chosen


# -- copying ------------------------------------------------------------------

def test_a_copy_is_a_vault_holding_the_same_accounts(vault, tmp_path):
    make_account(name='Bank', password='s3cret')
    destination = tmp_path / 'copy.vault'

    backups.copy_vault(destination)

    assert database.inspect_vault(destination) == 'ok'
    copied = sqlite3.connect(destination)
    try:
        assert copied.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 1
    finally:
        copied.close()


def test_a_copy_opens_with_the_same_master_password(vault, tmp_path, monkeypatch):
    """A backup is the vault, so it takes the master password and no other.
    This is also the property that makes a copy taken before a password change
    need the old one afterwards."""
    from core import auth
    from core import encryption
    make_account(name='Bank', password='s3cret')
    destination = tmp_path / 'copy.vault'
    backups.copy_vault(destination)

    encryption.release_key()
    monkeypatch.setattr(database, 'DB_PATH', destination)

    assert auth.verify_master_password(MASTER) is True
    assert accounts_service.list_accounts()[0]['password'] == 's3cret'


def test_a_failed_copy_leaves_nothing_behind(vault, tmp_path, monkeypatch):
    """Half a vault in a backups folder is worse than no vault: it is
    indistinguishable from one that worked until somebody tries to open it."""
    def explode(target):
        target.write_bytes(b'partial')
        raise OSError('the drive went away')
    monkeypatch.setattr(backups, '_copy_database', explode)
    destination = tmp_path / 'backups' / 'copy.vault'

    with pytest.raises(backups.BackupError):
        backups.copy_vault(destination)

    assert not destination.exists()
    assert list(destination.parent.glob('*')) == []


def test_a_copy_that_is_not_a_readable_vault_is_not_renamed_into_place(
        vault, tmp_path, monkeypatch):
    monkeypatch.setattr(backups, '_copy_database',
                        lambda target: target.write_bytes(b'not a database'))
    destination = tmp_path / 'backups' / 'copy.vault'

    with pytest.raises(backups.BackupError):
        backups.copy_vault(destination)

    assert not destination.exists()


def test_copying_with_no_vault_open_is_refused(vault, tmp_path):
    database.close_vault()

    with pytest.raises(backups.BackupError):
        backups.copy_vault(tmp_path / 'copy.vault')


# -- when a backup is owed ----------------------------------------------------

def test_nothing_is_due_when_automatic_backups_are_off(vault):
    assert backups.is_due(settings(auto_backup_enabled=0), {}, TODAY) is False


def test_the_first_backup_is_always_due(vault):
    assert backups.is_due(settings(), {'last_backup_date': ''}, TODAY) is True


@pytest.mark.parametrize('frequency,days,expected', [
    ('daily', 0, False), ('daily', 1, True),
    ('weekly', 6, False), ('weekly', 7, True),
    ('monthly', 29, False), ('monthly', 30, True),
])
def test_the_interval_is_respected(vault, frequency, days, expected):
    state = {'last_backup_date': (TODAY - datetime.timedelta(days=days)).isoformat()}

    assert backups.is_due(settings(backup_frequency=frequency), state, TODAY) is expected


def test_a_recorded_date_in_the_future_does_not_wait_itself_out(vault):
    """A clock that has been moved, not a backup from next week. Waiting it out
    would mean no backups until the calendar caught up."""
    state = {'last_backup_date': (TODAY + datetime.timedelta(days=400)).isoformat()}

    assert backups.is_due(settings(), state, TODAY) is True


def test_a_long_absence_produces_one_backup_not_one_per_interval(vault):
    """The vault has one state to copy. Filling the window with copies of the
    same moment is how the genuinely older ones get pushed out."""
    preferences.save_backup_date('2025-01-01')

    backups.run_if_due(settings(), TODAY)

    assert len(list(backups.default_dir().glob('*.vault'))) == 1


# -- running the schedule -----------------------------------------------------

def test_a_scheduled_backup_writes_a_file_and_records_it(vault):
    make_account(name='Bank')

    result = backups.run_if_due(settings(), TODAY)

    assert result['ok'] is True
    assert (backups.default_dir() / 'vault_backup_20260917.vault').exists()
    state = preferences.get_backup_state()
    assert [e['name'] for e in state['files']] == ['vault_backup_20260917.vault']
    assert state['last_backup_date'] == TODAY.isoformat()


def test_a_second_run_on_the_same_day_does_not_rewrite_the_backup(vault):
    """Without this, an evening unlock replaces the morning's copy — which is
    how a good backup gets overwritten by one taken after something went
    wrong."""
    backups.run_if_due(settings(), TODAY)
    taken = backups.default_dir() / 'vault_backup_20260917.vault'
    taken.write_bytes(b'pretend this is the morning copy')

    backups.run_if_due(settings(), TODAY)

    assert taken.read_bytes() == b'pretend this is the morning copy'


def test_a_manual_copy_sitting_in_the_way_is_not_adopted_for_deletion(vault):
    """Save a Copy offers this folder and this name by default, so the file
    already there can easily be one the user wrote by hand. The schedule steps
    over it without claiming it — a file this app did not create is not one it
    may later delete."""
    folder = backups.default_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / backups.backup_name(when=TODAY)).write_text('mine', encoding='utf-8')

    backups.run_if_due(settings(), TODAY)

    state = preferences.get_backup_state()
    assert state['files'] == []
    assert state['last_backup_date'] == TODAY.isoformat()


def test_a_failed_backup_does_not_advance_the_schedule(vault, monkeypatch):
    """Recording a failure as though it had worked would mean waiting out
    another whole interval before trying again."""
    monkeypatch.setattr(backups, '_copy_database',
                        lambda target: (_ for _ in ()).throw(OSError('disk full')))

    result = backups.run_if_due(settings(), TODAY)

    assert result['ok'] is False
    assert preferences.get_backup_state()['last_backup_date'] == ''


def test_a_failure_is_recorded_so_settings_can_say_so(vault, monkeypatch):
    """Backups that have been failing silently are worse than backups nobody
    turned on, because only one of those two is believed in."""
    monkeypatch.setattr(backups, '_copy_database',
                        lambda target: (_ for _ in ()).throw(OSError('disk full')))

    backups.run_if_due(settings(), TODAY)

    outcome = preferences.get_backup_state()['outcome']
    assert outcome['ok'] is False
    assert 'disk full' in outcome['message']


# -- retention ----------------------------------------------------------------

def _stage(folder, *names) -> list:
    """Writes backup files and returns the record that names them."""
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in names:
        (folder / name).write_text('backup', encoding='utf-8')
        stamp = name.split('_backup_')[1].removesuffix('.vault')
        entries.append({'name': name, 'date': f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}'})
    return entries


def test_backups_past_the_window_are_removed(vault):
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20240101.vault',
                     'vault_backup_20260901.vault')

    removed = backups.prune(settings(backup_retention_months=12), entries, folder, TODAY)

    assert removed == 1
    assert not (folder / 'vault_backup_20240101.vault').exists()
    assert (folder / 'vault_backup_20260901.vault').exists()


def test_the_newest_backup_survives_however_old_it_is(vault):
    """Come back after thirteen months with a twelve-month window and every
    backup is expired. Taken literally that empties the folder at the moment
    somebody is least sure of themselves."""
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20200101.vault',
                     'vault_backup_20200601.vault')

    removed = backups.prune(settings(backup_retention_months=1), entries, folder, TODAY)

    assert removed == 1
    assert (folder / 'vault_backup_20200601.vault').exists()


def test_keep_forever_deletes_nothing(vault):
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20100101.vault',
                     'vault_backup_20260101.vault')

    removed = backups.prune(settings(backup_retention_months=0), entries, folder, TODAY)

    assert removed == 0
    assert len(list(folder.glob('*.vault'))) == 2


def test_a_file_the_app_did_not_create_is_never_deleted(vault):
    """A copy made by hand can sit in this folder under a name of exactly the
    same shape. It is not ours to tidy away, so pruning works from the record
    rather than from what matches the pattern."""
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20200101.vault',
                     'vault_backup_20260901.vault')
    by_hand = folder / 'vault_backup_20200202.vault'
    by_hand.write_text('mine, not yours', encoding='utf-8')

    backups.prune(settings(backup_retention_months=12), entries, folder, TODAY)

    assert by_hand.exists()


def test_an_undated_record_is_kept_rather_than_guessed_at(vault):
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20260901.vault')
    entries.append({'name': 'vault_backup_whenever.vault', 'date': 'not a date'})
    (folder / 'vault_backup_whenever.vault').write_text('x', encoding='utf-8')

    backups.prune(settings(backup_retention_months=1), entries, folder, TODAY)

    assert (folder / 'vault_backup_whenever.vault').exists()


def test_a_failed_backup_prunes_nothing(vault, monkeypatch):
    """The ordering is the whole safety property of the retention setting: a
    destination that has stopped working stops the history growing rather than
    consuming it."""
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20200101.vault')
    preferences.save_backup_files(entries)
    preferences.save_backup_source(str(database.DB_PATH))
    monkeypatch.setattr(backups, '_copy_database',
                        lambda target: (_ for _ in ()).throw(OSError('gone')))

    backups.run_if_due(settings(backup_retention_months=1), TODAY)

    assert (folder / 'vault_backup_20200101.vault').exists()


def test_the_count_settings_shows_matches_what_pruning_would_do(vault):
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20200101.vault',
                     'vault_backup_20200301.vault',
                     'vault_backup_20260901.vault')

    predicted = backups.expired_count(entries, 12, TODAY)
    actual = backups.prune(settings(backup_retention_months=12), entries, folder, TODAY)

    assert predicted == actual == 2


# -- a copy must not act on the original's record ------------------------------

def test_a_copy_opened_as_a_vault_does_not_inherit_the_originals_backups(
        vault, tmp_path, monkeypatch):
    """A backup carries app_config, so opening one brings the original's backup
    settings and its list of files. Left alone, the copy would write into the
    original's folder under the original's name and prune the original's
    history — from a file somebody is probably only looking at because
    something went wrong with the original."""
    preferences.save_backup_files([{'name': 'vault_backup_20200101.vault',
                                    'date': '2020-01-01'}])
    preferences.save_backup_source(str(database.DB_PATH))
    preferences.save_backup_date('2020-01-01')

    copy = tmp_path / 'elsewhere' / 'copy.vault'
    backups.copy_vault(copy)
    monkeypatch.setattr(database, 'DB_PATH', copy)

    backups.run_if_due(settings(), TODAY)

    state = preferences.get_backup_state()
    assert [e['name'] for e in state['files']] == ['copy_backup_20260917.vault']
    assert (copy.parent / 'backups' / 'copy_backup_20260917.vault').exists()


# -- a poisoned record must not reach unlink() --------------------------------
#
# app_config is plaintext and unauthenticated, so the recorded backup list is
# attacker-authorable by anyone who can write the vault *file* — no master
# password needed. That record is handed to unlink() when retention runs, and
# retention runs unattended on the next unlock. These hold the line at both
# ends: the names are refused as they are read, and refused again at deletion.

@pytest.mark.parametrize('hostile', [
    '../../../taxes.pdf',                 # climbs out with forward slashes
    r'..\..\..\taxes.pdf',                # and with backslashes, on any OS
    'C:/Users/victim/Documents/keys.pem',  # a drive discards the folder entirely
    '/etc/passwd',                        # posix absolute
    'subdir/vault_backup_20200101.vault',  # right shape, wrong place
    '..',
    '',
])
def test_a_recorded_name_that_leaves_the_folder_is_refused(vault, hostile):
    """Path('/backups') / 'C:/x' is 'C:/x' — the folder is not a boundary on its
    own, so the name has to be one."""
    folder = backups.default_dir()
    folder.mkdir(parents=True, exist_ok=True)
    entries = [{'name': hostile, 'date': '2000-01-01'},
               *_stage(folder, 'vault_backup_20260917.vault')]

    removed = backups.prune(settings(backup_retention_months=1), entries, folder, TODAY)

    # Read raw, not through get_backup_state, which filters these names out on
    # the way back. Kept rather than merely uncounted is the point: asserting
    # only removed == 0 would pass for the wrong reason, since unlink() on a
    # path that happens not to exist raises FileNotFoundError and counts nothing
    # while still having tried. A refused name is never attempted at all.
    assert removed == 0
    stored = json.loads(database.read_config('backup_files'))
    assert hostile in [entry['name'] for entry in stored]


def test_the_poisoned_record_never_survives_being_read(vault, tmp_path):
    """The first of the two gates. Written straight to app_config, the way
    somebody with the vault file and no password would write it."""
    outside = tmp_path / 'taxes.pdf'
    outside.write_text('the victim files', encoding='utf-8')
    preferences.save_backup_files([
        {'name': str(outside), 'date': '2000-01-01'},
        {'name': '../../taxes.pdf', 'date': '2000-01-01'},
        {'name': 'vault_backup_20260917.vault', 'date': '2026-09-17'},
    ])

    kept = [entry['name'] for entry in preferences.get_backup_state()['files']]

    assert kept == ['vault_backup_20260917.vault']


def test_an_absolute_name_does_not_delete_the_file_it_points_at(vault, tmp_path):
    """The end-to-end shape of the attack, driven through prune() directly so
    it does not depend on the boundary filter above holding."""
    victim = tmp_path / 'taxes.pdf'
    victim.write_text('the victim files', encoding='utf-8')
    folder = backups.default_dir()
    entries = [{'name': str(victim), 'date': '2000-01-01'},
               *_stage(folder, 'vault_backup_20260917.vault')]

    backups.prune(settings(backup_retention_months=1), entries, folder, TODAY)

    assert victim.exists(), 'prune() deleted a file outside the backups folder'


def test_a_real_expired_backup_is_still_deleted(vault):
    """The companion to the four above: refusing hostile names must not stop
    retention doing its job."""
    folder = backups.default_dir()
    entries = _stage(folder, 'vault_backup_20200101.vault',
                     'vault_backup_20260917.vault')

    removed = backups.prune(settings(backup_retention_months=1), entries, folder, TODAY)

    assert removed == 1
    assert not (folder / 'vault_backup_20200101.vault').exists()
    assert (folder / 'vault_backup_20260917.vault').exists()


# -- the seal over the backup configuration -----------------------------------
#
# app_config is plaintext, so these rows are writable by anyone holding the
# vault file, with no master password. They are instructions — where to copy the
# whole vault, which files retention may delete — so they carry a token over
# their own bytes, encrypted under the session key. Writing the rows is easy;
# producing the token needs the key, which is the whole point.

def _tamper(**rows):
    """Writes app_config rows the way somebody with the file and no password
    would: straight in, around every helper that would reseal."""
    for key, value in rows.items():
        database.write_config(key, str(value))


def test_a_fresh_vault_is_sealed_without_complaining(vault):
    """Nothing configured means nothing to discard, so the first run seals
    quietly. Warning here would accuse every new user of being attacked."""
    assert backups.check_seal() == 'sealed'
    assert backups.check_seal() == 'ok'


def test_settings_saved_through_the_app_stay_sealed(vault):
    preferences.save_settings(**{**preferences.get_settings(),
                                 'auto_backup_enabled': 1,
                                 'backup_location': 'C:/somewhere'})

    assert backups.check_seal() == 'ok'
    assert preferences.get_settings()['backup_location'] == 'C:/somewhere'


def test_a_planted_destination_is_discarded_and_backups_turned_off(vault):
    """The attack this exists for: point the copy somewhere you control, turn
    the schedule on, and wait for the victim to unlock."""
    preferences.save_settings(**{**preferences.get_settings(),
                                 'auto_backup_enabled': 1})
    _tamper(backup_location='//attacker/share', auto_backup_enabled=1)

    assert backups.check_seal() == 'reset'

    settings = preferences.get_settings()
    assert settings['backup_location'] == preferences.BACKUP_LOCATION_DEFAULT
    assert settings['auto_backup_enabled'] == 0


def test_a_planted_file_record_is_discarded_too(vault):
    """The seal covers the deletion list as well as the destination, so Vuln 1's
    root cause is closed rather than only its symptom."""
    _tamper(backup_files=json.dumps([{'name': 'x', 'date': '2000-01-01'}]))

    assert backups.check_seal() == 'reset'
    assert preferences.get_backup_state()['files'] == []


def test_deleting_the_seal_fails_closed(vault):
    """Removing the token is no harder than writing the rows it covers, so its
    absence must mean 'not ours', never 'nothing to check'."""
    preferences.save_settings(**{**preferences.get_settings(),
                                 'auto_backup_enabled': 1,
                                 'backup_location': 'C:/chosen'})
    _tamper(backup_auth='')

    assert backups.check_seal() == 'reset'
    assert preferences.get_settings()['auto_backup_enabled'] == 0


def test_a_run_refuses_to_act_on_a_configuration_it_cannot_vouch_for(vault, tmp_path):
    """The gate sits in run_if_due, not only in the window, so no caller can
    forget it."""
    _tamper(backup_location=str(tmp_path / 'exfil'), auto_backup_enabled=1,
            backup_frequency='daily', last_backup_date='')

    result = backups.run_if_due(settings(backup_location=str(tmp_path / 'exfil')), TODAY)

    assert result['reset'] is True
    assert not (tmp_path / 'exfil').exists(), 'the vault was copied anyway'


def test_changing_the_master_password_keeps_the_backups(vault, cheap_argon2):
    """The seal is a token under the session key and a rotation changes it, so
    without a reseal a password change would turn the user's backups off and
    blame them for it."""
    preferences.save_settings(**{**preferences.get_settings(),
                                 'auto_backup_enabled': 1,
                                 'backup_location': 'C:/chosen'})

    assert auth.change_master_password(MASTER, NEW_MASTER) is True

    assert backups.check_seal() == 'ok'
    assert preferences.get_settings()['auto_backup_enabled'] == 1


# -- confirming and describing where backups go -------------------------------

def test_the_default_folder_needs_no_confirming(vault):
    assert backups.is_default_dir(settings())


def test_a_chosen_folder_is_unconfirmed_until_it_is_confirmed(vault, tmp_path):
    chosen = tmp_path / 'chosen'
    assert not backups.destination_confirmed(chosen)

    preferences.save_backup_confirmed_dir(str(chosen))

    assert backups.destination_confirmed(chosen)
    assert not backups.destination_confirmed(tmp_path / 'elsewhere')


def test_a_confirmation_that_was_planted_does_not_count(vault, tmp_path):
    """Otherwise the record saying 'the user agreed' would be written by the
    same hand that chose the folder."""
    _tamper(backup_confirmed_dir=str(tmp_path / 'attacker'))

    assert backups.check_seal() == 'reset'
    assert preferences.get_backup_state()['confirmed_dir'] == ''


def test_a_network_destination_is_described_before_it_is_used(vault):
    """Both spellings. Qt and the file picker hand back the platform's own, and
    a UNC path arrives with backslashes on Windows."""
    unc = '\u005c\u005cnas\u005cbackups'   # \\nas\backups, spelled so no
    assert unc.startswith('\u005c\u005c')  # heredoc or editor can eat a slash
    assert 'network share' in backups.destination_warning('//nas/backups')
    assert 'network share' in backups.destination_warning(unc)


def test_an_ordinary_folder_says_nothing(vault, tmp_path):
    assert backups.destination_warning(tmp_path) == ''

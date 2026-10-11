# -----------------------------------------------------------------------------
# tests/test_warning_settings.py
# The user's warning thresholds: when due dates and last-updated stamps turn
# orange and red, and which categories get either check.
#
# Two halves. The levels must follow whatever is in force, and fall back to the
# built-in values exactly — every existing vault runs on those. And the stored
# copy is vault content (it names custom categories), so it has to stay
# encrypted, survive every re-key, and be gone from memory once the vault locks.
# -----------------------------------------------------------------------------

import datetime

import pytest

from core import auth
from core import categories
from core import database
from core import encryption
from core import preferences
from core.categories import (
    LAPSED_AFTER_DAYS, attention_level, due_date_level, set_warning_settings,
    stale_after, updated_age_level, validate_warning_settings, warning_settings,
)
from conftest import MASTER, NEW_MASTER


@pytest.fixture(autouse=True)
def default_warnings():
    """Whatever a test puts in force is gone before the next one runs."""
    set_warning_settings(None)
    yield
    set_warning_settings(None)


def days_away(days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=days)).isoformat()


def days_ago(days: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def settings(**overrides) -> dict:
    """The built-in settings for the built-in categories, with overrides
    merged into the named categories' entries."""
    base = categories.default_warning_settings(categories.CATEGORY_CONFIG)
    for key in ('due_warn', 'due_danger', 'lapsed_after'):
        if key in overrides:
            base[key] = overrides.pop(key)
    for name, entry in overrides.items():
        base['categories'].setdefault(name, categories.warning_defaults(name))
        base['categories'][name].update(entry)
    return base


# -- defaults are what the app always did -------------------------------------

def test_the_defaults_match_the_built_in_behaviour():
    assert stale_after('Credit Cards') == (21, 28)
    assert stale_after('Loans') == (60, 90)
    assert stale_after('Income') == (180, 365)
    assert stale_after('Websites') is None
    assert stale_after('Secure Notes') is None
    assert stale_after('My Custom Thing') is None
    assert due_date_level(days_away(7), None, 'Utilities') == 'danger'
    assert due_date_level(days_away(14), None, 'Utilities') == 'warn'
    assert due_date_level(days_away(15), None, 'Utilities') == ''


def test_explicitly_saved_defaults_change_nothing():
    names = list(categories.CATEGORY_CONFIG) + ['My Custom Thing']
    before = warning_settings(names)
    set_warning_settings(categories.default_warning_settings(names))
    assert warning_settings(names) == before


# -- overrides are followed ---------------------------------------------------

def test_due_thresholds_follow_the_settings():
    set_warning_settings(settings(due_warn=30, due_danger=3))
    assert due_date_level(days_away(3), None, 'Loans') == 'danger'
    assert due_date_level(days_away(4), None, 'Loans') == 'warn'
    assert due_date_level(days_away(30), None, 'Loans') == 'warn'
    assert due_date_level(days_away(31), None, 'Loans') == ''


def test_the_lapsed_cutoff_follows_the_settings():
    set_warning_settings(settings(lapsed_after=10))
    assert due_date_level(days_away(-11), '', 'Loans') == 'lapsed'
    assert due_date_level(days_away(-10), '', 'Loans') == 'danger'


def test_due_warnings_switched_off_for_one_category_leave_the_others_alone():
    set_warning_settings(settings(Utilities={'due': False}))
    assert due_date_level(days_away(1), None, 'Utilities') == ''
    # Off means off — a long-dead date is not singled out either.
    assert due_date_level(days_away(-400), '', 'Utilities') == ''
    assert due_date_level(days_away(1), None, 'Loans') == 'danger'


def test_staleness_can_be_switched_off_for_a_category_that_has_it():
    set_warning_settings(settings(Banking={'stale': False}))
    assert updated_age_level(days_ago(400), 'Banking') == ''
    assert updated_age_level(days_ago(400), 'Credit Cards') == 'danger'


def test_staleness_can_be_switched_on_for_websites_and_custom_categories():
    set_warning_settings(settings(**{
        'Websites': {'stale': True, 'stale_warn': 90, 'stale_danger': 180},
        'Garden Club': {'stale': True},
    }))
    assert updated_age_level(days_ago(100), 'Websites') == 'warn'
    assert updated_age_level(days_ago(180), 'Websites') == 'danger'
    # Switched on with no numbers of its own, it runs the default clock.
    assert stale_after('Garden Club') == categories.DEFAULT_STALE_AFTER
    assert updated_age_level(days_ago(28), 'Garden Club') == 'danger'


def test_per_category_thresholds_are_independent():
    set_warning_settings(settings(**{'Credit Cards': {'stale_warn': 5, 'stale_danger': 10}}))
    assert updated_age_level(days_ago(6), 'Credit Cards') == 'warn'
    assert updated_age_level(days_ago(6), 'Banking') == ''


def test_a_slug_category_reads_the_settings_of_its_current_name():
    set_warning_settings(settings(Banking={'stale': False}))
    assert stale_after('banking') is None


def test_attention_follows_the_switches():
    account = {'category': 'Utilities', 'due_date': days_away(2),
               'last_updated': days_ago(1)}
    assert attention_level(account) == 'danger'
    set_warning_settings(settings(Utilities={'due': False}))
    assert attention_level(account) == ''


# -- validation ---------------------------------------------------------------

@pytest.mark.parametrize('bad', [
    {'due_warn': 7, 'due_danger': 7},                      # red not stricter
    {'due_warn': 7, 'due_danger': 14},                     # the wrong way round
    {'due_warn': 0},                                        # below the floor
    {'lapsed_after': categories.WARNING_DAYS_MAX + 1},     # above the ceiling
    {'due_warn': True},                                     # a bool is not a day count
    {'due_warn': '14'},
])
def test_global_thresholds_that_make_no_sense_are_refused(bad):
    with pytest.raises(ValueError):
        validate_warning_settings(settings(**bad))


@pytest.mark.parametrize('entry', [
    {'stale_warn': 28, 'stale_danger': 28},
    {'stale_warn': 30, 'stale_danger': 20},
    {'stale_warn': 0},
    {'stale': 'yes'},
])
def test_category_thresholds_that_make_no_sense_are_refused(entry):
    with pytest.raises(ValueError):
        validate_warning_settings(settings(Banking=entry))


def test_an_entry_missing_keys_takes_them_from_its_defaults():
    clean = validate_warning_settings(
        {'due_warn': 14, 'due_danger': 7, 'lapsed_after': 60,
         'categories': {'Loans': {'stale': False}, 'loans': {'due': False}}})
    assert clean['categories']['Loans'] == {
        'due': False, 'stale': True, 'stale_warn': 60, 'stale_danger': 90}


# -- storage ------------------------------------------------------------------

def test_a_fresh_vault_has_no_saved_warning_settings(vault):
    assert preferences.get_warning_settings() is None


def test_saved_settings_round_trip_and_are_put_in_force(vault):
    wanted = settings(due_warn=21, **{'Garden Club': {'stale': True}})
    preferences.save_warning_settings(wanted)
    auth.apply_saved_settings()
    assert preferences.get_warning_settings() == validate_warning_settings(wanted)
    assert categories.due_thresholds() == (21, 7)
    assert stale_after('Garden Club') == (21, 28)


def test_custom_category_names_never_reach_disk_in_the_clear(vault):
    preferences.save_warning_settings(settings(**{'Private Fund': {'stale': True}}))
    raw = database.read_config('warning_settings')
    assert raw and 'Private Fund' not in raw
    assert database.DB_PATH.read_bytes().find(b'Private Fund') == -1


def test_a_tampered_row_reads_as_the_defaults(vault):
    database.write_config('warning_settings', '{"due_warn": 1000}')
    assert preferences.get_warning_settings() is None


def test_saving_while_locked_is_refused(vault):
    encryption.release_key()
    with pytest.raises(encryption.VaultLockedError):
        preferences.save_warning_settings(settings())


def test_locking_puts_the_defaults_back(vault):
    preferences.save_warning_settings(settings(due_warn=30))
    auth.apply_saved_settings()
    assert categories.due_thresholds() == (30, 7)
    encryption.release_key()
    assert categories.due_thresholds() == (categories.DUE_WARN_DAYS,
                                           categories.DUE_DANGER_DAYS)


def test_unlocking_puts_the_saved_settings_back(vault):
    preferences.save_warning_settings(settings(lapsed_after=90))
    encryption.release_key()
    assert auth.verify_master_password(MASTER)
    assert categories.lapsed_after_days() == 90
    assert categories.lapsed_after_days() != LAPSED_AFTER_DAYS


def test_settings_survive_a_master_password_change(vault):
    preferences.save_warning_settings(settings(lapsed_after=90))
    assert auth.change_master_password(MASTER, NEW_MASTER)
    assert preferences.get_warning_settings()['lapsed_after'] == 90
    encryption.release_key()
    assert auth.verify_master_password(NEW_MASTER)
    assert categories.lapsed_after_days() == 90


def test_settings_survive_a_work_factor_upgrade_at_login(vault, monkeypatch):
    preferences.save_warning_settings(settings(lapsed_after=90))
    encryption.release_key()
    # Raising the work factors makes the next login re-key the whole vault.
    monkeypatch.setattr(encryption, 'ARGON2_TIME_COST', 2)
    assert auth.verify_master_password(MASTER)
    assert auth.read_vault_crypto()[1]['t'] == 2
    assert categories.lapsed_after_days() == 90

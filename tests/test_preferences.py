# -----------------------------------------------------------------------------
# tests/test_preferences.py
# Settings and window view state.
#
# Nothing here can lose vault data, which is why it had no tests. What it can do
# is make the app unusable from a single bad row in app_config — a lock timeout
# of zero that locks the vault the instant it opens, a sidebar width that
# collapses the account list, a theme name no combo box matches so the Settings
# dialog cannot be saved back to something valid. Every one of those is a value
# read off disk, so every one of them is checked on the way in.
# -----------------------------------------------------------------------------

import pytest

import database
import preferences


def store(key: str, value: str):
    """Writes a raw app_config row, as a hand-edited or corrupt database would."""
    database.write_config(key, value)


# -- settings come back valid or not at all -----------------------------------

def test_a_fresh_vault_reads_the_documented_defaults(vault):
    settings = preferences.get_settings()
    assert settings['lock_timeout_minutes'] == 15
    assert settings['clipboard_clear_seconds'] == 30
    assert settings['shutdown_after_lock_minutes'] == 60
    assert settings['theme'] == 'dark'
    assert settings['font_size'] == 'default'
    assert settings['list_density'] == 'compact'


def test_saved_settings_read_back(vault):
    preferences.save_settings(30, 60, 120, 'light', 'large', 'comfortable',
                              auto_backup_enabled=1, backup_frequency='daily',
                              backup_retention_months=6,
                              backup_location=r'D:\Vault Backups')
    assert preferences.get_settings() == {
        'lock_timeout_minutes': 30,
        'clipboard_clear_seconds': 60,
        'shutdown_after_lock_minutes': 120,
        'theme': 'light',
        'font_size': 'large',
        'list_density': 'comfortable',
        'auto_backup_enabled': 1,
        'backup_frequency': 'daily',
        'backup_retention_months': 6,
        'backup_location': r'D:\Vault Backups',
    }


def test_backup_settings_default_when_nothing_is_stored(vault):
    settings = preferences.get_settings()

    assert settings['auto_backup_enabled'] == 0, 'backups must be opt-in'
    assert settings['backup_retention_months'] == 12
    assert settings['backup_location'] == preferences.BACKUP_LOCATION_DEFAULT


def test_a_backup_location_stored_empty_falls_back_to_the_sentinel(vault):
    """An empty path would resolve against the working directory, which for a
    shortcut launch is the application folder."""
    database.write_config('backup_location', '   ')

    assert preferences.get_settings()['backup_location'] == \
        preferences.BACKUP_LOCATION_DEFAULT


def test_a_zero_lock_timeout_on_disk_does_not_reach_the_session(vault):
    """The one that matters. A stored 0 would otherwise arrive at
    set_lock_timeout_seconds with only its floor between it and a vault that
    re-locks the moment it is opened."""
    store('lock_timeout_minutes', '0')
    assert preferences.get_settings()['lock_timeout_minutes'] == 15


@pytest.mark.parametrize('key,bad', [
    ('lock_timeout_minutes', '7'),
    ('lock_timeout_minutes', '-30'),
    ('lock_timeout_minutes', '999999'),
    ('clipboard_clear_seconds', '0'),
    ('clipboard_clear_seconds', '3600'),
    ('shutdown_after_lock_minutes', '5'),
    ('theme', 'solarized'),
    ('font_size', 'enormous'),
    ('list_density', 'roomy'),
])
def test_a_value_outside_the_valid_set_falls_back_to_its_default(vault, key, bad):
    """Falling back rather than passing it through is what keeps the Settings
    dialog usable: a value no combo box holds could not be selected, and
    save_settings rejects it, so the dialog could not be saved until it was
    changed — the one thing it could not do."""
    store(key, bad)
    assert preferences.get_settings()[key] == preferences._SETTING_DEFAULTS[key]


@pytest.mark.parametrize('bad', ['', 'fifteen', '15.5', '\x00'])
def test_an_unparseable_integer_setting_does_not_raise(vault, bad):
    store('lock_timeout_minutes', bad)
    assert preferences.get_settings()['lock_timeout_minutes'] == 15


def test_zero_is_a_real_shutdown_timeout_and_survives(vault):
    """0 means "never shut down" and is in the valid set — it must not be swept
    up by the same guard that rejects a zero lock timeout."""
    preferences.save_settings(15, 30, 0, 'dark', 'default')
    assert preferences.get_settings()['shutdown_after_lock_minutes'] == 0


@pytest.mark.parametrize('args', [
    (7, 30, 60, 'dark', 'default'),
    (15, 7, 60, 'dark', 'default'),
    (15, 30, 7, 'dark', 'default'),
    (15, 30, 60, 'solarized', 'default'),
    (15, 30, 60, 'dark', 'enormous'),
])
def test_save_settings_refuses_a_value_outside_the_set(vault, args):
    with pytest.raises(ValueError):
        preferences.save_settings(*args)


def test_a_refused_save_writes_nothing(vault):
    preferences.save_settings(30, 60, 120, 'light', 'large')
    with pytest.raises(ValueError):
        preferences.save_settings(30, 60, 120, 'light', 'enormous')
    assert preferences.get_settings()['theme'] == 'light'


# -- sort order ----------------------------------------------------------------

def test_sort_key_defaults_to_name(vault):
    assert preferences.get_sort_key() == 'name'


def test_an_unknown_stored_sort_key_falls_back(vault):
    store('sort_key', 'category; DROP TABLE accounts')
    assert preferences.get_sort_key() == 'name'


def test_save_sort_key_ignores_a_key_that_is_not_one(vault):
    preferences.save_sort_key('due_date')
    preferences.save_sort_key('nonsense')
    assert preferences.get_sort_key() == 'due_date'


@pytest.mark.parametrize('key,expected', [
    ('name', False),
    ('due_date', False),
    ('last_updated', True),
])
def test_each_sort_starts_in_its_natural_direction(vault, key, expected):
    """"Last Updated" means most recent first; A-Z and the nearest due date read
    the other way."""
    assert preferences.get_sort_descending(key) is expected


def test_an_explicit_direction_overrides_the_natural_one(vault):
    preferences.save_sort_descending(False)
    assert preferences.get_sort_descending('last_updated') is False


def test_a_junk_direction_falls_back_to_the_natural_one(vault):
    store('sort_descending', 'yes')
    assert preferences.get_sort_descending('last_updated') is True


# -- window geometry -----------------------------------------------------------

def test_geometry_round_trips_as_bytes(vault):
    blob = bytes(range(256))
    preferences.save_window_geometry(blob)
    assert preferences.get_window_geometry() == blob


def test_no_stored_geometry_reads_as_none(vault):
    assert preferences.get_window_geometry() is None


@pytest.mark.parametrize('bad', ['not hex', 'abc', 'zz'])
def test_a_geometry_row_that_is_not_hex_is_dropped_not_raised(vault, bad):
    """Cosmetic state must never block a launch — restoreGeometry simply does
    not run, and the window opens at its default size."""
    store('window_geometry', bad)
    assert preferences.get_window_geometry() is None


# -- sidebar width -------------------------------------------------------------

def test_no_stored_sidebar_width_reads_as_none(vault):
    """None is not "use the minimum" — it means the user has never dragged the
    sidebar, and the list fits itself to its widest row instead."""
    assert preferences.get_sidebar_width() is None


@pytest.mark.parametrize('width', [preferences.SIDEBAR_MIN, 400, preferences.SIDEBAR_MAX])
def test_a_width_inside_the_drag_range_round_trips(vault, width):
    preferences.save_sidebar_width(width)
    assert preferences.get_sidebar_width() == width


@pytest.mark.parametrize('width', [0, 1, preferences.SIDEBAR_MIN - 1,
                                   preferences.SIDEBAR_MAX + 1, 100_000, -400])
def test_a_width_outside_the_drag_range_is_never_stored(vault, width):
    preferences.save_sidebar_width(width)
    assert preferences.get_sidebar_width() is None


@pytest.mark.parametrize('bad', ['', 'wide', '300.5'])
def test_a_width_that_is_not_a_number_reads_as_none(vault, bad):
    store('sidebar_width', bad)
    assert preferences.get_sidebar_width() is None


def test_a_stored_width_outside_the_range_is_refused_on_the_way_out_too(vault):
    """save_sidebar_width guards the write, but the row can also arrive by hand.
    Reading is where a collapsed account list would actually happen."""
    store('sidebar_width', '12')
    assert preferences.get_sidebar_width() is None


def test_the_window_and_the_stored_bounds_are_the_same_numbers(vault, qt_app):
    """ui/account_list.py used to carry its own copy under different names, with
    a comment pointing at the wrong module. They lived in ui/main_window.py
    until the window was split; the rule is the same wherever they sit."""
    from ui import account_list
    assert account_list.SIDEBAR_MIN == preferences.SIDEBAR_MIN
    assert account_list.SIDEBAR_MAX == preferences.SIDEBAR_MAX

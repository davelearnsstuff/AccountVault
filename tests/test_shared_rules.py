# -----------------------------------------------------------------------------
# tests/test_shared_rules.py
# The rules more than one module has to agree on.
#
# Each of these was, at some point, written out twice. A round-trip test cannot
# catch that: both copies work, right up until one of them is changed. So these
# tests assert the agreement itself — that the callers share a definition rather
# than happening to hold the same value today.
# -----------------------------------------------------------------------------

import pytest

from core import auth
from core import categories
from core.categories import parse_iso_date
from services import account_export, csv_export as backup_service, export_io, history_io
from services import accounts as accounts_service


# -- one spelling of a stored date --------------------------------------------

@pytest.mark.parametrize('text,expected', [
    ('2024-01-15', '2024-01-15'),
    ('2024-01-15T10:30:00', '2024-01-15'),
    ('2024-01-15 10:30:00', '2024-01-15'),
    ('  2024-01-15  ', '2024-01-15'),
    ('2024-02-29', '2024-02-29'),
])
def test_the_spellings_the_app_itself_writes_are_read(text, expected):
    assert parse_iso_date(text).isoformat() == expected


@pytest.mark.parametrize('text', [
    '2024-W01-1',   # an ISO week date: fromisoformat reads it as 2024-01-01
    '20240115',     # the compact form: fromisoformat reads it as 2024-01-15
    '2024-1-5',
    '01-15-2024',
    '15/01/2024',
    '2024-13-01',
    '2024-02-30',
    'not a date',
    '',
    None,
    0,
])
def test_a_spelling_the_app_never_writes_is_refused_rather_than_guessed_at(text):
    """The two at the top are the reason this function exists. Nobody types
    them, but datetime.date.fromisoformat accepts both, so a bare call read
    '2024-W01-1' as the 1st of January and '20240115' as the 15th — a date the
    user never wrote, landing in balance history where it cannot be undone."""
    assert parse_iso_date(text) is None


@pytest.mark.parametrize('text', [
    '2024-01-15', '2024-01-15T10:30:00', '2024-W01-1', '20240115',
    '2024-02-30', 'not a date', '',
])
def test_the_shared_parser_and_the_save_path_accept_exactly_the_same_dates(vault, text):
    """The restore path that used to be the other half of this is gone with the
    .vault format, but the rule it was holding is not: every date that reaches
    storage goes through categories.parse_iso_date, and the save path must not
    have grown a second opinion about what a date is. history_io still reads
    dates out of a file and is the caller that would notice first."""
    shared = parse_iso_date(text)
    from_shared = shared.isoformat() if shared else None

    try:
        from_save = accounts_service._validate_date(text)
    except ValueError:
        from_save = None

    assert from_shared == from_save


def test_the_history_importer_is_deliberately_more_generous(vault):
    """services/history_io.parse_date is *not* part of that agreement, and must
    not be folded into it. It reads bank statements, where the whole job is
    working out what a file meant; the others read values this app wrote."""
    assert history_io.parse_date('01/15/2024') == '2024-01-15'
    assert parse_iso_date('01/15/2024') is None


# -- one definition of a good password -----------------------------------------

def test_every_rule_has_a_caption_and_an_error():
    for caption, met, error in auth.PASSWORD_RULES:
        assert caption and error
        assert callable(met)


@pytest.mark.parametrize('password,failing', [
    ('short', ['At least 15 characters']),
    (' a sufficiently long passphrase ', ['No spaces at the start or end']),
    (' short ', ['At least 15 characters', 'No spaces at the start or end']),
    ('a sufficiently long passphrase', []),
])
def test_the_checklist_and_the_validator_name_the_same_failures(password, failing):
    """Pinned to the captions rather than derived from the table, so a rule
    quietly losing its test still fails here."""
    unmet = [caption for caption, met, _ in auth.PASSWORD_RULES if not met(password)]
    assert unmet == failing
    assert len(auth.validate_password_strength(password)) == len(failing)


def test_a_good_password_clears_every_rule():
    good = 'a sufficiently long passphrase'
    assert auth.validate_password_strength(good) == []
    assert all(met(good) for _, met, _ in auth.PASSWORD_RULES)


def test_the_checklist_on_screen_shows_one_line_per_rule(qt_app):
    """The setup screen and Change Password each restated the length rule as
    their own lambda and showed nothing for the space rule, so the checklist
    could read fully satisfied while submit refused the password."""
    from PySide6.QtWidgets import QLineEdit
    from ui.widgets import password_requirements

    field = QLineEdit()
    labels = password_requirements(field)
    assert len(labels) == len(auth.PASSWORD_RULES)
    assert [label.text() for label in labels] == [c for c, _, _ in auth.PASSWORD_RULES]


def test_the_checklist_tracks_the_rule_it_stands_for(qt_app):
    from PySide6.QtWidgets import QLineEdit
    from ui.widgets import password_requirements

    field = QLineEdit()
    labels = password_requirements(field)

    field.setText('short')
    assert [label.objectName() for label in labels] == ['Subtle', 'RequirementMet']

    field.setText('a sufficiently long passphrase')
    assert all(label.objectName() == 'RequirementMet' for label in labels)

    field.setText('a sufficiently long passphrase ')
    assert [label.objectName() for label in labels] == ['RequirementMet', 'Subtle']


def test_a_password_the_checklist_calls_good_is_one_the_validator_accepts(qt_app):
    """The property that was actually broken, stated directly."""
    from PySide6.QtWidgets import QLineEdit
    from ui.widgets import password_requirements

    field = QLineEdit()
    labels = password_requirements(field)

    for candidate in ('short', ' short ', 'a sufficiently long passphrase',
                      'a sufficiently long passphrase ', ' leading space is bad too'):
        field.setText(candidate)
        all_green = all(label.objectName() == 'RequirementMet' for label in labels)
        assert all_green == (auth.validate_password_strength(candidate) == [])


# -- one list of keys no export writes -----------------------------------------

def test_both_exports_stand_on_the_same_floor():
    assert export_io.INTERNAL_KEYS <= account_export._INTERNAL
    assert export_io.INTERNAL_KEYS <= backup_service.INTERNAL_KEYS


def test_the_per_account_file_excludes_more_not_less():
    """category and closed are written by name elsewhere in that file, so they
    are excluded from the field loop on top of the shared floor."""
    assert account_export._INTERNAL - export_io.INTERNAL_KEYS == {'category', 'closed'}


# -- one definition of a safe CSV cell -----------------------------------------

def test_every_csv_writer_shares_one_defuse():
    assert history_io.defuse is export_io.defuse
    assert account_export.defuse is export_io.defuse
    assert backup_service.defuse is export_io.defuse


# -- one map of levels to colours ----------------------------------------------

def test_only_levels_categories_can_return_are_mapped():
    from ui.theme import LEVEL_SUFFIX
    assert set(LEVEL_SUFFIX) <= {'warn', 'danger', 'lapsed'}


@pytest.mark.parametrize('base,level,expected', [
    ('RowMeta', 'warn', 'RowMetaWarn'),
    ('RowMeta', 'danger', 'RowMetaDanger'),
    ('RowMeta', 'lapsed', 'RowMeta'),
    ('RowMeta', '', 'RowMeta'),
    ('Level', 'warn', 'LevelWarn'),
    ('Level', 'danger', 'LevelDanger'),
])
def test_a_level_names_the_same_style_wherever_it_is_painted(base, level, expected):
    """The account list and the detail pane each carried their own map. They
    agreed by coincidence, and 'lapsed' was already missing from one of them."""
    from ui.theme import level_object_name
    assert level_object_name(base, level) == expected


def test_every_mapped_name_has_a_rule_in_the_stylesheet():
    """An object name with no matching rule is a silent no-op: the widget keeps
    the default colour and nothing reports it."""
    from ui.theme import LEVEL_SUFFIX, stylesheet
    sheet = stylesheet('dark', 'default')
    for base in ('RowMeta', 'Level'):
        for suffix in LEVEL_SUFFIX.values():
            assert f'#{base}{suffix}' in sheet


def test_a_lapsed_date_is_left_uncoloured_on_purpose():
    """Not an oversight. A date this far past is a record nobody rolled forward,
    not a payment anyone can make today, and painting it the same red as a bill
    due on Tuesday is what taught people to stop reading the colour."""
    from ui.theme import level_object_name
    assert categories.due_date_level('2000-01-01', None, 'Credit Cards') == 'lapsed'
    assert level_object_name('RowMeta', 'lapsed') == 'RowMeta'
    assert level_object_name('Level', 'lapsed') == 'Level'


# -- the password rules are enforced where the password is set, not only on screen

@pytest.mark.parametrize('password', ['short', ' a sufficiently long passphrase '])
def test_setup_refuses_a_password_that_fails_the_rules(tmp_path, monkeypatch,
                                                       cheap_argon2, password):
    """The rules used to live only in SetupScreen._submit, which made the
    password protecting everything the one rule a new caller could omit. Now
    that a backup is a copy of the vault, this is the only password there is."""
    from core import database

    monkeypatch.setattr(database, 'DB_PATH', tmp_path / 'vault.vault')
    monkeypatch.setattr(database, 'restrict_to_owner', lambda path: None)
    database.init_db()
    # The session key is a module global that outlives a database, so the
    # "nothing was installed" assertion below needs a known starting point.
    auth.release_key()

    with pytest.raises(ValueError):
        auth.setup_master_password(password)

    # Refused before anything was written: the vault is still unclaimed, and
    # no key was installed for a password that was never accepted.
    assert auth.is_first_launch() is True
    assert not auth.is_unlocked()


@pytest.mark.parametrize('password', ['short', ' a sufficiently long passphrase '])
def test_rotation_refuses_a_new_password_that_fails_the_rules(vault, password):
    from conftest import MASTER

    with pytest.raises(ValueError):
        auth.change_master_password(MASTER, password)

    # The old password still opens the vault — the rotation never started.
    auth.release_key()
    assert auth.verify_master_password(MASTER) is True


def test_rotation_judges_the_password_as_typed_not_as_stripped(vault):
    """The space rule refuses rather than trims, so it has to be tested against
    what the user typed. Checked after the strip it would never fire, and the
    user would walk away believing in a password that is not the one the vault
    was re-keyed under."""
    from conftest import MASTER

    with pytest.raises(ValueError, match='space'):
        auth.change_master_password(MASTER, ' a sufficiently long passphrase ')

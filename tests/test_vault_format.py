# -----------------------------------------------------------------------------
# tests/test_vault_format.py
# The crypto format: field binding, the key check, and the upgrade that installs
# both.
#
# This is the riskiest code in the app. It runs unattended at login, it rewrites
# every encrypted field in the vault, and the two ways it can be wrong are a
# vault that no password opens and a vault it quietly declines to protect. The
# upgrade is also the one path that cannot be exercised by ordinary use — every
# vault the suite builds elsewhere is already at the current format — so the
# format-0 vault is stood up by hand here.
# -----------------------------------------------------------------------------

import json
import sqlite3

import pytest

import auth
import database
import encryption
from conftest import MASTER, corrupt, make_account, raw_column
from services import accounts as accounts_service

LEGACY_FIELDS = ('name', 'username', 'password', 'notes')


def build_format_0_vault(password: str = MASTER, **fields) -> int:
    """The vault a build from before the format existed would have left behind.

    Fields sealed with no associated data, the work factors implied rather than
    recorded, and none of vault_format, kdf_params or key_check present. Returns
    the account's row id.

    Leaves the vault locked, because the point of every test below is what
    happens on the way in."""
    values = {'name': 'Legacy Bank', 'username': 'dave',
              'password': 's3cret', 'notes': 'a note'}
    values.update(fields)

    salt = encryption.generate_salt()
    encryption.install_key(password, salt, encryption.LEGACY_KDF_PARAMS)
    with database.connect() as db:
        database.write_config('master_hash', auth.ph.hash(password), db)
        database.write_config('salt', salt.hex(), db)
        for key in (auth._FORMAT_KEY, auth._KDF_KEY, auth._CANARY_KEY):
            db.execute("DELETE FROM app_config WHERE key = ?", (key,))
        cursor = db.execute(
            "INSERT INTO accounts (category, name, username, password, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            ('Banking', *(encryption.encrypt(values[f]) for f in LEGACY_FIELDS)),
        )
        row_id = cursor.lastrowid
        db.execute(
            "INSERT INTO balance_history (account_id, balance, recorded_at) "
            "VALUES (?, ?, ?)",
            (row_id, encryption.encrypt('1200'), '2024-01-15'),
        )
    auth.lock_app()
    accounts_service.invalidate_cache()
    return row_id


def stored(key: str):
    return database.read_config(key)


# -- the upgrade ---------------------------------------------------------------

def test_a_format_0_vault_opens_and_is_upgraded_at_login(vault):
    """The upgrade every existing install will take, once."""
    row_id = build_format_0_vault()

    assert auth.verify_master_password(MASTER) is True

    account = accounts_service.get_account(row_id)
    assert account['name'] == 'Legacy Bank'
    assert account['password'] == 's3cret'
    assert account['notes'] == 'a note'
    assert accounts_service.list_balance_history(row_id)[0]['balance'] == '1200'

    assert stored(auth._FORMAT_KEY) == str(auth.VAULT_FORMAT)
    assert json.loads(stored(auth._KDF_KEY)) == encryption.current_kdf_params()
    assert stored(auth._CANARY_KEY) is not None


def test_the_upgrade_binds_the_fields_it_rewrites(vault):
    """Not just readable — readable only as the column it lives in."""
    row_id = build_format_0_vault()
    auth.verify_master_password(MASTER)

    on_disk = raw_column(row_id, 'password')
    assert encryption.decrypt(on_disk, encryption.field_aad('accounts', 'password')) == 's3cret'
    with pytest.raises(Exception):
        encryption.decrypt(on_disk)


def test_a_second_login_rewrites_nothing(vault):
    """The upgrade is done once, not on every login — re-keying a vault every
    time it is opened would burn a second of Argon2 and a full table rewrite for
    nothing, and would make the flag meaningless."""
    build_format_0_vault()
    auth.verify_master_password(MASTER)
    after_upgrade = _snapshot()

    auth.lock_app()
    assert auth.verify_master_password(MASTER) is True

    assert _snapshot() == after_upgrade


def test_a_damaged_field_does_not_shut_the_owner_out(vault):
    """A field that will not decrypt is already lost. Refusing the login over it
    would take the whole vault with it, which is the opposite of the trade
    rotation makes — see test_rotation for the other side."""
    row_id = build_format_0_vault()
    with database.connect() as db:
        db.execute("UPDATE accounts SET notes = ? WHERE id = ?",
                   (corrupt(raw_column(row_id, 'notes')), row_id))

    assert auth.verify_master_password(MASTER) is True

    account = accounts_service.get_account(row_id)
    assert account['password'] == 's3cret'          # the rest came through
    assert encryption.is_unreadable(account['notes'])
    assert stored(auth._FORMAT_KEY) == str(auth.VAULT_FORMAT)


def test_a_vault_whose_key_opens_nothing_is_refused_rather_than_rewritten(vault):
    """The one case where "skip what will not decrypt" would be catastrophic: a
    salt that does not belong to the password decrypts nothing at all, and
    rewriting under it would replace a recoverable vault with an unrecoverable
    one."""
    build_format_0_vault()
    database.write_config('salt', encryption.generate_salt().hex())
    before = _snapshot()

    with pytest.raises(auth.VaultConfigError):
        auth.verify_master_password(MASTER)

    assert _snapshot() == before
    assert encryption.is_unlocked() is False


def test_a_wrong_key_is_caught_even_when_legacy_plaintext_would_re_encrypt(vault):
    """The guard has to ask whether any *ciphertext* opened, not whether any
    field was written. Plaintext left over from an older build re-encrypts
    happily under any key at all — so counting it as a success let a mismatched
    key through, and the upgrade then overwrote the salt, taking every genuinely
    encrypted field with it for good.

    A vault mid-upgrade from a build that encrypted fewer fields holds exactly
    this mixture, so it is not a hypothetical shape."""
    row_id = build_format_0_vault()
    with database.connect() as db:
        db.execute("UPDATE accounts SET notes = ? WHERE id = ?",
                   ('legacy plaintext', row_id))
    database.write_config('salt', encryption.generate_salt().hex())
    before = _snapshot()

    with pytest.raises(auth.VaultConfigError):
        auth.verify_master_password(MASTER)

    assert _snapshot() == before, 'the salt was replaced and the ciphertext orphaned'


def test_raising_the_work_factors_rekeys_at_the_next_login(vault, monkeypatch):
    """What the recorded parameters are for. Before they were stored, changing
    these constants orphaned every existing field."""
    account = make_account(name='Bank', password='s3cret', current_balance=1200)
    auth.lock_app()

    monkeypatch.setattr(encryption, 'ARGON2_TIME_COST', 2)
    assert auth.verify_master_password(MASTER) is True

    assert json.loads(stored(auth._KDF_KEY))['t'] == 2
    assert accounts_service.get_account(account['id'])['password'] == 's3cret'
    assert accounts_service.list_balance_history(account['id'])[0]['balance'] == '1200'

    # And it settles: the next login finds nothing to do.
    auth.lock_app()
    assert auth.verify_master_password(MASTER) is True
    assert json.loads(stored(auth._KDF_KEY))['t'] == 2


def test_the_upgrade_clears_what_earlier_deletes_left_in_the_file(vault):
    """secure_delete scrubs pages as they are freed, but only for connections
    that set it — so every page a vault freed before this release is still
    sitting in its freelist, legible. Deleted accounts end up there, and so does
    the plaintext the legacy migration replaced. Turning the pragma on does
    nothing for that backlog; the upgrade's VACUUM is what reaches it."""
    secret = 'LEAKME-deleted-long-ago'
    build_format_0_vault(notes=secret)

    # Freed the way a build without the pragma would have freed it.
    stale = sqlite3.connect(database.DB_PATH)
    stale.execute("INSERT INTO accounts (category, name) VALUES ('Banking', ?)",
                  (secret,))
    stale.commit()
    stale.execute("DELETE FROM accounts WHERE name = ?", (secret,))
    stale.commit()
    stale.close()
    assert secret.encode() in database.DB_PATH.read_bytes()

    assert auth.verify_master_password(MASTER) is True

    assert secret.encode() not in database.DB_PATH.read_bytes()


# -- the key check -------------------------------------------------------------

def test_a_salt_that_does_not_match_the_password_is_caught_at_the_door(vault):
    """Without the check this login succeeds, every field reads as "⚠ unreadable",
    and the first save writes that placeholder over the real value."""
    make_account(name='Bank', password='s3cret')
    auth.lock_app()
    database.write_config('salt', encryption.generate_salt().hex())

    with pytest.raises(auth.VaultConfigError):
        auth.verify_master_password(MASTER)

    assert encryption.is_unlocked() is False


def test_a_missing_key_check_on_a_current_vault_is_refused(vault):
    """Absent is fine on a format-0 vault, which has not been given one yet.
    On a vault that says it is current it means the row was removed."""
    make_account(name='Bank')
    auth.lock_app()
    with database.connect() as db:
        db.execute("DELETE FROM app_config WHERE key = ?", (auth._CANARY_KEY,))

    with pytest.raises(auth.VaultConfigError):
        auth.verify_master_password(MASTER)


def test_a_rotation_leaves_a_key_check_the_new_password_opens(vault):
    from conftest import NEW_MASTER

    make_account(name='Bank', password='s3cret')
    assert auth.change_master_password(MASTER, NEW_MASTER) is True

    auth.lock_app()
    assert auth.verify_master_password(NEW_MASTER) is True
    assert accounts_service.get_account(
        accounts_service.list_accounts()[0]['id'])['password'] == 's3cret'


# -- field binding -------------------------------------------------------------

def test_a_token_moved_between_columns_does_not_open(vault):
    """The attack the binding exists for. Anyone who can write the database file
    can move an intact token from one column to another without knowing the key;
    unbound, it decrypts perfectly and the vault shows a note as the password."""
    account = make_account(name='Bank', username='dave', password='s3cret',
                           notes='not the password')

    with database.connect() as db:
        db.execute("UPDATE accounts SET password = notes WHERE id = ?", (account['id'],))
    accounts_service.invalidate_cache()

    moved = accounts_service.get_account(account['id'])
    assert encryption.is_unreadable(moved['password'])
    assert moved['password'] != 'not the password'


def test_a_balance_cannot_be_read_as_an_account_field(vault):
    """The binding carries the table name too, so the shuffle does not work
    across tables either."""
    account = make_account(name='Bank', current_balance=1200)
    with database.connect() as db:
        balance = db.execute(
            "SELECT balance FROM balance_history WHERE account_id = ?",
            (account['id'],)).fetchone()['balance']
        db.execute("UPDATE accounts SET notes = ? WHERE id = ?", (balance, account['id']))
    accounts_service.invalidate_cache()

    assert encryption.is_unreadable(accounts_service.get_account(account['id'])['notes'])


def _snapshot() -> list:
    """Every stored byte of the vault, including the config rows the format
    upgrade writes. conftest.snapshot_rows excludes the rate-limit counters;
    this needs the crypto rows, which those tests do not."""
    with database.connect() as db:
        return [
            [tuple(r) for r in db.execute("SELECT * FROM accounts ORDER BY id")],
            [tuple(r) for r in db.execute("SELECT * FROM balance_history ORDER BY id")],
            [tuple(r) for r in db.execute(
                "SELECT * FROM app_config WHERE key IN (?, ?, ?, 'salt') ORDER BY key",
                (auth._FORMAT_KEY, auth._KDF_KEY, auth._CANARY_KEY))],
        ]
